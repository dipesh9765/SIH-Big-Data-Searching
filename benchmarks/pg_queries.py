"""Query latency against an existing benchmark schema (run pg_bench.py with --keep first).

python benchmarks/pg_queries.py bench_ab12cd34 --repeats 15
"""

from __future__ import annotations

import argparse
import statistics
import time

from benchmarks.pg_bench import DSN, QUERIES, percentile
from docsearch.engine.pg import PgIndex


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("schema")
    parser.add_argument("--repeats", type=int, default=15)
    args = parser.parse_args()
    with PgIndex(DSN, schema=args.schema) as index:
        print(f"{'query':<20}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}  strategy")
        for name, (ranks, mode) in QUERIES.items():
            query = " ".join(f"t{r}" for r in ranks)
            result = index.search(query, mode=mode, limit=10)  # type: ignore[arg-type]
            samples = []
            for _ in range(args.repeats):
                began = time.perf_counter()
                index.search(query, mode=mode, limit=10)  # type: ignore[arg-type]
                samples.append((time.perf_counter() - began) * 1000)
            print(
                f"{name:<20}{statistics.median(samples):>9.1f}{percentile(samples, 0.95):>9.1f}"
                f"{percentile(samples, 0.99):>9.1f}  {result.strategy}"
            )


if __name__ == "__main__":
    main()
