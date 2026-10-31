# 13. Deletion: tombstone first, purge later, never trust the purge for correctness

Status: accepted (scale-out work, `feature/scale-v2`)

## Decision
- **Delete = tombstone.** `PgIndex.delete_documents` sets `documents.deleted_at`, replaces the content hash (so identical text can be indexed again as a new document), and adjusts the corpus counters (`doc_count`, `total_length`) in the same transaction, so BM25 statistics stay right. Every search reads the set of tombstoned ids in its snapshot (cached by a `deletions` counter in `meta`) and the engine skips them: the scan masks them out, WAND and the AND intersection do not offer them to the top-k. A deleted document therefore disappears from results on the next query, regardless of what is still in the postings.
- **Purge = space and accuracy, not correctness.** `purge_deleted` (under the compaction lock) re-analyses the stored text to find the document's terms, removes its postings from the uncompacted delta and from the compressed blocks it sits in (re-encoding only those blocks), decrements document frequencies, and clears the text (`content=''`, `purged_at`). The tombstone is **never** removed. If the analyzer has changed since indexing and a posting is missed, the cost is a little space and a slightly high document frequency; the tombstone still keeps it out of results.
- **Uploads.** `delete_asset` tombstones the asset's documents first (idempotent), then deletes its jobs and row, then the stored original. A text shared with another upload (identical content) is kept until the last owner is deleted. If an upload is deleted while a worker is still indexing it, the worker notices (its asset update touches no row) and takes its own documents back.
- **HTTP.** `DELETE /v1/assets/{id}` and `DELETE /v1/documents/{id}`.
- **Auth.** Because deletion is destructive and the service had no authentication, `DOCSEARCH_API_KEY` now protects every `/v1` route (Bearer or `X-API-Key`, constant-time compare). Probes, `/metrics` and the docs page stay open; expose `/metrics` internally only.

## Verified
- A deleted document never appears, for scan, WAND and AND-skip strategies, with data compacted or not, and with ingest and compaction after the deletion.
- After deleting 90 of 400 documents, purging and compacting, rankings and scores equal an index built from the surviving 310 documents alone (compared with the SQLite reference to 1e-9).
- Purge: block postings drop by exactly the number removed, document frequency is decremented, text is cleared, a second purge does nothing, delta postings of deleted documents go too.
- Asset deletion: text, jobs, row and stored object are gone; shared text survives until its last owner is deleted; all transcript units of a recording are removed; an upload deleted mid-processing leaves nothing searchable.

## Limits
- The exclusion set is held in memory per process and passed to each query; it grows with the number of deleted documents ever (tombstones are permanent). At millions of deletions this needs a different structure (a compacted bitmap, or rebuilding blocks and dropping old tombstones with an analyzer-version check).
- Between a delete and the purge, the deleted text still sits in the database (privacy-sensitive deployments should run `purge_deleted` right after deleting; the compactor loop does it every cycle).
- Cached search pages in clients are not invalidated.
