"""Micro-benchmark of the block engine's search algorithm, in memory (no database).

Compares exhaustive scoring (what the SQLite reference does: score every posting) against
block-max WAND on the same data, so the effect of pruning is isolated from storage cost.

    python benchmarks/engine_bench.py --docs 100000 --tokens 80
"""

from __future__ import annotations

import argparse
import gc
import pickle
import random
import statistics
import time
from collections import Counter
from pathlib import Path

from docsearch.engine.memory import MemoryIndex
from docsearch.engine.search import Mode, Pruning, search


def build(docs: int, tokens: int, vocabulary: int, seed: int) -> MemoryIndex:
    rng = random.Random(seed)
    terms = [f"t{n}" for n in range(vocabulary)]
    weights = [1.0 / (rank + 1) for rank in range(vocabulary)]
    index = MemoryIndex()
    for doc_id in range(1, docs + 1):
        length = max(5, int(rng.gauss(tokens, tokens / 4)))
        counts = Counter(rng.choices(terms, weights, k=length))
        index.add_counts(doc_id, dict(counts), length)
    index.finish()
    return index


def timed(
    index: MemoryIndex, terms: list[str], mode: Mode, pruning: Pruning, repeats: int
) -> list[float]:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        search(index, terms, mode=mode, k=10, pruning=pruning)
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def pct(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=100_000)
    parser.add_argument("--tokens", type=int, default=80)
    parser.add_argument("--vocabulary", type=int, default=50_000)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cache", type=Path, help="pickle file: reuse the built corpus")
    args = parser.parse_args()

    start = time.perf_counter()
    if args.cache and args.cache.exists():
        index = pickle.loads(args.cache.read_bytes())
    else:
        index = build(args.docs, args.tokens, args.vocabulary, args.seed)
        if args.cache:
            args.cache.write_bytes(pickle.dumps(index))
    gc.collect()
    gc.freeze()  # millions of long-lived objects: keep collector pauses out of the timings
    print(
        f"corpus: {index.doc_count:,} docs, {index.posting_count():,} postings, "
        f"{index.payload_bytes() / 1e6:.1f} MB compressed "
        f"({index.payload_bytes() / index.posting_count():.2f} bytes/posting), "
        f"built in {time.perf_counter() - start:.1f}s"
    )
    queries: dict[str, tuple[list[str], Mode]] = {
        "rare AND rare": (["t30000", "t40000"], "all"),
        "common AND rare": (["t0", "t30000"], "all"),
        "common AND common": (["t0", "t1"], "all"),
        "top-3 common OR": (["t0", "t1", "t2"], "any"),
        "common OR rare": (["t0", "t5000", "t30000"], "any"),
        "6-term mixed OR": (["t3", "t17", "t200", "t1500", "t9000", "t30000"], "any"),
    }
    header = f"{'query':<20}{'scan ms':>10}{'pruned ms':>11}{'auto ms':>9}"
    print(header + f"{'auto chose':>12}{'vs scan':>9}")
    for name, (terms, mode) in queries.items():
        scan = statistics.median(timed(index, terms, mode, "never", args.repeats))
        pruned = statistics.median(timed(index, terms, mode, "always", args.repeats))
        auto = statistics.median(timed(index, terms, mode, "auto", args.repeats))
        chose = search(index, terms, mode=mode, k=10).stats.strategy
        print(f"{name:<20}{scan:>10.2f}{pruned:>11.2f}{auto:>9.2f}{chose:>12}{scan / auto:>8.1f}x")


if __name__ == "__main__":
    main()
