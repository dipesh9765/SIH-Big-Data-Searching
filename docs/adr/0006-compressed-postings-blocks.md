# 6. Compressed postings blocks with per-block score bounds

Status: accepted (scale-out work, `feature/scale-v2`)

## Context
The SQLite index stores one row per posting and scores every posting of every query term. That is exact but its cost grows with the length of the postings lists; a query containing a common word touches most of the corpus. The goal now is ~1M documents.

## Decision
Postings lists are cut into blocks of 128 postings (`engine/codec.py`). Each block keeps its metadata outside the payload: first/last document id, count, `max_tf` and `min_len`. The payload is varint-coded id deltas followed by varint term frequencies. From `max_tf` and `min_len` the best BM25 score any posting in the block can reach is computed at query time (the score rises with tf and falls with length), which lets `engine/search.py` skip blocks without decompressing them (block-max WAND for OR, skip-driven intersection for AND).

Varint over bit-packing: about 20 lines, trivially verifiable, ~2.3 bytes/posting measured on a Zipf corpus. Bit-packing/PForDelta would be denser and faster to decode; it is a drop-in change inside `codec.py`.

## Verified
Differential tests against the SQLite index (same ids, same scores, same tie-breaks) on random queries, k in {1,5,10,50}, AND and OR.

## Measured (in memory, 50,000 docs x ~80 tokens, 3.2M postings, 7.5 MB compressed)
| Query | exhaustive p50 | pruned p50 |
|---|---|---|
| common OR rare | 86 ms | 17 ms (5.0x) |
| 6-term mixed OR | 102 ms | 22 ms (4.7x) |
| top-3 common OR | 157 ms | 218 ms (0.7x) |
| common AND common | 114 ms | 159 ms (0.7x) |

## Consequences
Pruning wins when at least one query term is selective (the realistic case). With only very common terms every block's bound is close to the global bound, nothing can be skipped, and the bookkeeping makes pruned search slower than exhaustive. Planned mitigations: reorder document ids by length so block bounds tighten, an adaptive fallback to exhaustive scoring, and stop-word-like terms being dropped by idf. An exact total hit count requires disabling pruning (`exact_total=True`).
