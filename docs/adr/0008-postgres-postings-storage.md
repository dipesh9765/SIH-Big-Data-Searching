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

## Measured (300,000 synthetic documents, 19.5M postings, 350,000 terms)
Zipf corpus, ~80 tokens per document plus one unique token each, `benchmarks/pg_bench.py`. One Windows laptop (12 cores, 7.7 GB RAM), PostgreSQL 17 in Docker Desktop with 512 MB `shared_buffers` and a 2 GB container limit, synchronous_commit off. Queries are top-10 through a connection pool; median of 9 runs. The baseline is PostgreSQL full-text search (`to_tsvector('simple')` + GIN, `ts_rank_cd`) on the same documents.

| | docsearch | Postgres FTS |
|---|---|---|
| ingest (500-doc batches, includes text analysis in Python) | 1,400 docs/s, flat from 10k to 300k | n/a |
| index size (excluding document text) | 114 MB blocks + 59 MB terms | 386 MB (tsvector + GIN) |
| rare AND rare | 4.4 ms | 0.7 ms |
| common AND rare | 25.6 ms | 2.3 ms |
| common AND common | 162 ms | 508 ms |
| 3 common words, OR | 231 ms | 2,229 ms |
| common OR rare | 92 ms | 2,003 ms |
| 6-term mixed OR | 67 ms | 3,367 ms |

How to read it: the full-text search wins the selective queries (a GIN lookup is a few page reads; our path pays for a pooled connection, six queries and decoding every block of each term). It loses by 3-30x where the query terms are common, because it must rank every match, while ours scans or prunes with block bounds. The two engines rank differently (`ts_rank_cd` has no idf or length normalisation), so overlap of their top-10 lists is not reported as a quality measure. These are synthetic words, not real language; a real corpus has a different frequency shape and relevance judgements this benchmark does not have.

**Compaction finding.** Compacting a 19.5M-posting delta in one go ran at 28k postings/s (701 s, after a keyset-paging fix; 897 s before it); compacting every 50,000 documents ran at 125k postings/s (156 s in total), with the same query latency afterwards and a slightly larger block table (114 MB vs 98 MB, from partial tail blocks). A 100,000-document run, whose delta fits in cache, compacted at 130k/s. The likely cause is that the delta no longer fits in memory (this is a hypothesis supported by the three runs, not profiled I/O). The compactor should therefore run continuously and keep the delta small; it is not a nightly job.

Not measured: 1M documents (this machine's 7.7 GB RAM makes it a poor test; the trend above is linear in postings for scans), concurrent ingest while querying, p99 under load, relevance quality.

## Tried and rejected: lazy per-block payload fetching
Idea: read only block metadata (bounds, counts) per query and fetch a block's payload when it is first decoded, in chunks of 32 blocks, so blocks that pruning skips are never read. It was implemented, passed the differential tests, and was measured against the eager version on the same 300,000-document corpus (median of 15 runs, same machine, one after the other):

| Query | eager (all blocks, one query) | lazy |
|---|---|---|
| rare AND rare | 4.6 ms | 6.0 ms |
| common AND rare | 27.6 ms | 44.9 ms |
| common AND common | 181.8 ms | 173.0 ms |
| 3 common words, OR | 238.7 ms | 246.6 ms |
| common OR rare | 86.2 ms | 96.6 ms |
| 6-term mixed OR | 64.5 ms | 134.3 ms |

It was slower where it was meant to help. Block-max WAND and skip-driven intersection jump around a long list, so each jump into an unfetched region costs a round trip (about 0.5 ms), while reading a whole list is one query returning a few hundred KB. At this size the transfer is cheaper than the round trips. It was reverted before being committed, so the code is not in the history.

What this does and does not show: lazy fetching loses at 300k documents, where the longest list is about 2,300 blocks. It might win for lists of tens of thousands of blocks (millions of documents per term), but that is untested here (7.7 GB RAM), so it stays out. If it is revisited, avoid per-skip round trips: prefetch ranges guided by the metadata, or keep hot terms' blocks in a process-local cache keyed by the `generation` counter.

## Found while doing this
Advisory locks were database-wide, so a compactor running on one schema made `compact()` on another schema return "skipped" (a flaky test, caught while a benchmark was running in a different schema). The locks are now `(class, hashtext(schema))` pairs, with a regression test.
