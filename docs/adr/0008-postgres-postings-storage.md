# 8. Postings in PostgreSQL: delta table, compacted blocks, snapshot reads

Status: accepted (scale-out work, `feature/scale-v2`)

## Context
The index must hold millions of documents in a database with proper architecture. MongoDB was rejected (no sorted, compressed, range-scannable postings). The choice is how to lay an inverted index out in PostgreSQL.

## Decision
- `documents` (text, length, SHA-256 for exact-duplicate detection), `terms` (term to id, document frequency), `postings_blocks` (one row per 128 postings: block bounds and a varint payload, ADR 0006), `postings_delta` (one row per posting, newly ingested), `meta` (corpus counters).
- **Ingest** writes documents and delta rows in one transaction. Writers are serialised with an advisory lock so ids become visible in order; text analysis happens before taking it, so analysis parallelises across worker processes.
- **Compaction** moves delta rows into blocks, a chunk of terms per transaction. If a term's new ids are all above its last block, the partial tail block is topped up and new blocks appended; otherwise the term is rewritten. One compactor at a time (advisory lock).
- **Search** runs in one `REPEATABLE READ` read-only transaction, so a compaction committing mid-query cannot hide or double-count a posting. Corpus counters, term frequencies (compacted + delta), blocks and delta postings are read in that snapshot, merged per term, and passed to the same `engine.search` that is differentially tested against the SQLite reference.
- Document lengths live in a process-local dense numpy array extended incrementally (`id > max cached id`). That is only correct because ingest is serialised (a smaller id can never appear after a larger one); the lock is therefore load-bearing, not an optimisation.
- Document frequency is not updated per ingest (a hot row per term would serialise writers); `terms.doc_freq` counts compacted postings and the delta count is added at query time.

## Verified
`tests/test_pg.py` loads the same random corpora into PostgreSQL and the SQLite reference and compares rankings and scores (AND and OR; k of 1, 10, 40) with the data (a) all in the delta, (b) fully compacted in chunks of 50 terms, and (c) after three rounds of ingest, each followed by compaction, checking before and after each compaction. Both search strategies are compared. Tests need a Postgres (docker compose service, `DOCSEARCH_TEST_DSN`) and are skipped without one.

## Limits (not yet addressed)
- A query reads every block of each of its terms, payload included. A common term over 1M documents is megabytes per query. Fix: fetch block metadata first and payloads lazily for the blocks pruning does not skip.
- No deletion yet (needs tombstones applied at search time and dropped by the compactor).
- Connection per call; a pool is needed for a server.
- The length array is O(documents) memory per process (8 bytes each): 8 MB per million documents, fine.
- Serialised ingest caps write throughput to one transaction at a time; bulk batches amortise it.
