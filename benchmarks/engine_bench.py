"""Micro-benchmark of the block engine's search algorithm, in memory (no database).

Compares exhaustive scoring (what the SQLite reference does: score every posting) against
block-max WAND on the same data, so the effect of pruning is isolated from storage cost.

    python benchmarks/engine_bench.py --docs 100000 --tokens 80
"""

from __future__ import annotations

import argparse
import random
import statistics
import time
from collections import Counter

from docsearch.engine.memory import MemoryIndex
from docsearch.engine.search import Mode, search


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
    index: MemoryIndex, terms: list[str], mode: Mode, exact: bool, repeats: int
) -> list[float]:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        search(index, terms, mode=mode, k=10, exact_total=exact)
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
    args = parser.parse_args()

    start = time.perf_counter()
    index = build(args.docs, args.tokens, args.vocabulary, args.seed)
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
    print(
        f"{'query':<20}{'exhaustive p50 ms':>20}{'pruned p50 ms':>16}{'speedup':>9}{'scored':>16}"
    )
    for name, (terms, mode) in queries.items():
        slow = timed(index, terms, mode, True, args.repeats)
        fast = timed(index, terms, mode, False, args.repeats)
        a = search(index, terms, mode=mode, k=10, exact_total=True).stats.postings_scored
        b = search(index, terms, mode=mode, k=10).stats.postings_scored
        print(
            f"{name:<20}{statistics.median(slow):>20.2f}{statistics.median(fast):>16.2f}"
            f"{statistics.median(slow) / max(statistics.median(fast), 1e-9):>8.1f}x"
            f"{b:>9,}/{a:<6,}"
        )


if __name__ == "__main__":
    main()
