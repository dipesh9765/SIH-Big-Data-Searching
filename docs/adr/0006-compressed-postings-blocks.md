# 6. Compressed postings blocks with per-block score bounds

Status: accepted (scale-out work, `feature/scale-v2`)

## Context
The SQLite index stores one row per posting and scores every posting of every query term. That is exact but its cost grows with the length of the postings lists; a query containing a common word touches most of the corpus. The goal now is ~1M documents.

## Decision
Postings lists are cut into blocks of 128 postings (`engine/codec.py`). Each block keeps its metadata outside the payload: first/last document id, count, `max_tf` and `min_len`. The payload is varint-coded id deltas followed by varint term frequencies. From `max_tf` and `min_len` the best BM25 score any posting in the block can reach is computed at query time (the score rises with tf and falls with length), which lets `engine/search.py` skip blocks without decompressing them (block-max WAND for OR, skip-driven intersection for AND).

Varint over bit-packing: about 20 lines, trivially verifiable, ~2.3 bytes/posting measured on a Zipf corpus. Bit-packing/PForDelta would be denser and faster to decode; it is a drop-in change inside `codec.py`.

## Verified
Differential tests against the SQLite index (same ids, same scores, same tie-breaks) on random queries, k in {1,5,10,50}, AND and OR.

## Measured
In memory (no database), Zipf-distributed synthetic corpus, 100,000 docs x ~80 tokens, 6.4M postings, 15.1 MB compressed (2.36 bytes/posting), top-10, median of 15 runs, one Windows laptop. "scan" scores every posting, "pruned" forces block-max, "auto" is what `search()` does by default.

| Query | scan | pruned | auto (chose) |
|---|---|---|---|
| rare AND rare | 0.09 ms | 0.04 ms | 0.04 ms (and-skip) |
| common AND rare | 15.0 ms | 0.52 ms | 0.55 ms (and-skip), 27x faster than scan |
| common AND common | 29 ms | 238 ms | 30 ms (scan) |
| 3 very common words, OR | 45 ms | 331 ms | 46 ms (scan) |
| common OR rare | 17 ms | 13 ms | 17 ms (scan) |
| 6-term mixed OR | 20 ms | 15 ms | 20 ms (scan) |

## What the first version got wrong, and the fix
The first version always pruned. On queries made only of common words it was **slower** than scoring everything (e.g. 3 common words, 50k docs: 218 ms vs 157 ms), because no block can be skipped and the cursor bookkeeping is pure overhead. Three changes, each measured:

1. **Single-byte fast path in `Block.decode`.** A block whose payload is `2*count - 1` bytes must consist of one-byte varints, so it is sliced and prefix-summed at C speed instead of decoded byte by byte. About 2x on every query that touches dense lists (the pure-Python varint loop was the dominant cost).
2. **Vectorised scan (numpy).** The scan decodes each list once and scores all its postings in one array operation, with the same formula in the same operation order (scores equal the SQLite reference to 1e-9, tested). Common-only queries went from ~115-155 ms to 17-28 ms at 50k docs.
3. **Cost-based strategy choice.** Prune only if the rarest term is under 5% of the corpus **and** (for OR) a scan would touch more than 300,000 postings. AND queries always drive from the rarest list when it is selective.

## Consequences and limits
- The 300,000-posting budget was set from these measurements: at 100k docs scan is within ~4 ms of pruning on mixed queries, and scan cost is linear in postings, so pruning should win clearly at larger sizes. **That crossover has not been measured at 1M documents**; this machine has 7.7 GB of RAM, and the in-memory benchmark does not fit 1M. The Postgres-backed benchmark (phase 4) is where it will be checked, and the constants may move.
- Queries of only very common words are linear in corpus size (about 45 ms at 100k, so roughly 450 ms at 1M if it scales). Fixes that would change that: impact-ordered or score-sorted postings, or treating terms that appear in most documents as stop words at query time (not exact, so not done).
- An exact total hit count needs a scan (`exact_total=True`); pruned searches return `total=None`.
- numpy is now a runtime dependency of the engine (pinned in `requirements.txt`).
