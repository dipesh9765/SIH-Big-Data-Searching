"""End-to-end benchmark of the PostgreSQL index on a synthetic Zipf corpus.

    docker compose -f deploy/docker-compose.yml up -d postgres
    python benchmarks/pg_bench.py --docs 100000 --fts

Measures ingest throughput, compaction time, on-disk size and query latency (p50/p95/p99) per
query class, optionally against PostgreSQL's built-in full-text search (GIN over a tsvector) built
from the same documents, and checks how much the two top-10 lists overlap. Uses a throwaway schema.
"""

from __future__ import annotations

import argparse
import gc
import itertools
import os
import random
import statistics
import time
import uuid

import psycopg

from docsearch.engine.pg import NewDocument, PgIndex

DSN = os.environ.get(
    "DOCSEARCH_TEST_DSN", "postgresql://docsearch:docsearch_dev@127.0.0.1:5433/docsearch"
)

QUERIES: dict[str, tuple[list[int], str]] = {
    # ranks into the Zipf vocabulary: 0 is the most common word
    "rare AND rare": ([30000, 40000], "all"),
    "common AND rare": ([0, 30000], "all"),
    "common AND common": ([0, 1], "all"),
    "top-3 common OR": ([0, 1, 2], "any"),
    "common OR rare": ([0, 5000, 30000], "any"),
    "6-term mixed OR": ([3, 17, 200, 1500, 9000, 30000], "any"),
}


def percentile(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def make_documents(count: int, tokens: int, vocabulary: int, seed: int) -> list[NewDocument]:
    rng = random.Random(seed)
    words = [f"t{n}" for n in range(vocabulary)]
    cumulative = list(itertools.accumulate(1.0 / (rank + 1) for rank in range(vocabulary)))
    docs = []
    for number in range(count):
        length = max(5, int(rng.gauss(tokens, tokens / 4)))
        text = " ".join(rng.choices(words, cum_weights=cumulative, k=length)) + f" uid{number}"
        docs.append(NewDocument(f"doc {number}", f"{number}.txt", "text/plain", text))
    return docs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=100_000)
    parser.add_argument("--tokens", type=int, default=80)
    parser.add_argument("--vocabulary", type=int, default=50_000)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--compact-every", type=int, default=0, help="compact after every N documents (0: once)"
    )
    parser.add_argument("--fts", action="store_true", help="also build and query Postgres FTS")
    parser.add_argument("--keep", action="store_true", help="keep the schema afterwards")
    args = parser.parse_args()

    schema = f"bench_{uuid.uuid4().hex[:8]}"
    index = PgIndex(DSN, schema=schema)
    print(f"schema {schema}; generating {args.docs:,} documents ...", flush=True)
    t0 = time.perf_counter()
    docs = make_documents(args.docs, args.tokens, args.vocabulary, args.seed)
    print(f"  generated in {time.perf_counter() - t0:.1f}s", flush=True)
    gc.collect()
    gc.freeze()

    t0 = time.perf_counter()
    compact_time = 0.0
    compacted_postings = 0
    for offset in range(0, len(docs), args.batch):
        index.add_many(docs[offset : offset + args.batch])
        if args.compact_every and (offset + args.batch) % args.compact_every == 0:
            began = time.perf_counter()
            compacted_postings += index.compact().postings
            compact_time += time.perf_counter() - began
        if (offset // args.batch) % 20 == 19:
            done = offset + args.batch
            print(
                f"  ingested {done:,} ({done / (time.perf_counter() - t0):,.0f} docs/s)", flush=True
            )
    ingest = time.perf_counter() - t0 - compact_time
    print(f"ingest: {args.docs / ingest:,.0f} docs/s ({ingest:.0f}s, batch {args.batch})")

    t0 = time.perf_counter()
    moved = index.compact()
    compact_time += time.perf_counter() - t0
    compacted_postings += moved.postings
    print(
        f"compaction total: {compacted_postings:,} postings in {compact_time:.0f}s "
        f"({compacted_postings / compact_time:,.0f}/s)"
    )
    print(
        f"compaction: {moved.postings:,} postings, {moved.terms:,} terms "
        f"in {time.perf_counter() - t0:.0f}s"
    )

    with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
        size = conn.execute(
            "SELECT pg_total_relation_size('postings_blocks'), pg_total_relation_size('terms'),"
            " pg_total_relation_size('documents')"
        ).fetchone()
        assert size is not None
        print(
            f"on disk: blocks {size[0] / 1e6:.0f} MB, terms {size[1] / 1e6:.0f} MB, "
            f"documents {size[2] / 1e6:.0f} MB"
        )
        if args.fts:
            t0 = time.perf_counter()
            conn.execute(
                "CREATE TABLE fts AS"
                " SELECT id, to_tsvector('simple', content) AS tsv FROM documents"
            )
            conn.execute("CREATE INDEX fts_gin ON fts USING gin (tsv)")
            conn.execute("ANALYZE fts")
            conn.commit()
            fts_size = conn.execute("SELECT pg_total_relation_size('fts')").fetchone()
            assert fts_size is not None
            print(f"fts build: {time.perf_counter() - t0:.0f}s, {fts_size[0] / 1e6:.0f} MB")

        header = f"{'query':<20}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}  strategy"
        print(header + ("   fts p50 ms  overlap@10" if args.fts else ""))
        for name, (ranks, mode) in QUERIES.items():
            query = " ".join(f"t{r}" for r in ranks)
            samples = []
            result = index.search(query, mode=mode, limit=10)  # type: ignore[arg-type]
            for _ in range(args.repeats):
                began = time.perf_counter()
                index.search(query, mode=mode, limit=10)  # type: ignore[arg-type]
                samples.append((time.perf_counter() - began) * 1000)
            line = (
                f"{name:<20}{statistics.median(samples):>9.1f}{percentile(samples, 0.95):>9.1f}"
                f"{percentile(samples, 0.99):>9.1f}  {result.strategy}"
            )
            if args.fts:
                joiner = " & " if mode == "all" else " | "
                tsquery = joiner.join(f"t{r}" for r in ranks)
                sql = (
                    "SELECT id FROM fts WHERE tsv @@ to_tsquery('simple', %s)"
                    " ORDER BY ts_rank_cd(tsv, to_tsquery('simple', %s)) DESC, id LIMIT 10"
                )
                fts_samples = []
                top: list[int] = []
                for _ in range(max(3, args.repeats // 3)):
                    began = time.perf_counter()
                    top = [r[0] for r in conn.execute(sql, (tsquery, tsquery))]
                    fts_samples.append((time.perf_counter() - began) * 1000)
                mine = {h.document.id for h in result.hits}
                overlap = f"{len(mine & set(top)) / len(mine):.0%}" if mine else "n/a"
                line += f"   {statistics.median(fts_samples):>10.1f}  {overlap:>9}"
            print(line, flush=True)

    stats = index.stats()
    print(
        f"index: {stats.documents:,} docs, {stats.terms:,} terms, {stats.block_postings:,} postings"
    )
    index.close()
    if not args.keep:
        index.drop_schema()


if __name__ == "__main__":
    main()
