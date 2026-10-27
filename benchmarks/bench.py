"""Reproducible benchmark on a synthetic corpus.

    python benchmarks/bench.py --docs 5000 --tokens 200

The corpus is generated from a seeded Zipf distribution over a synthetic vocabulary, so word
frequencies look like natural language (a few very common words, a long tail of rare ones) and the
numbers are comparable between runs and machines. Reports:

* indexing throughput (documents/s, one transaction per document, as the API does it),
* query latency (p50 / p95 / p99) for queries made of rare, mixed and very common terms,
* known-item recall: query with a few rare terms taken from a document and check that BM25 puts
  that document at rank 1 (a sanity check on ranking, not a relevance benchmark).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from docsearch.analysis import tokenize
from docsearch.index import Index


def make_vocabulary(size: int) -> list[str]:
    """Pronounceable fake words that survive stemming and are never stop words."""
    rng = random.Random(7)
    consonants, vowels = "bdgklmnprstvz", "aeiou"
    words: set[str] = set()
    while len(words) < size:
        words.add(
            "".join(rng.choice(consonants) + rng.choice(vowels) for _ in range(rng.randint(3, 5)))
        )
    return sorted(words)  # sorted so rank 0 (most frequent) is deterministic


def make_corpus(docs: int, tokens: int, vocab: list[str], seed: int) -> list[str]:
    rng = random.Random(seed)
    weights = [1.0 / (rank + 1) for rank in range(len(vocab))]  # Zipf, exponent 1
    return [" ".join(rng.choices(vocab, weights=weights, k=tokens)) for _ in range(docs)]


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct))]


def timed_queries(index: Index, queries: list[str], mode: str) -> dict[str, float]:
    latencies = []
    for query in queries:
        started = time.perf_counter()
        index.search(query, mode=mode, limit=10)  # type: ignore[arg-type]
        latencies.append((time.perf_counter() - started) * 1000)
    return {
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(percentile(latencies, 0.95), 2),
        "p99_ms": round(percentile(latencies, 0.99), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--docs", type=int, default=5000)
    parser.add_argument("--tokens", type=int, default=200, help="tokens per document")
    parser.add_argument("--vocab", type=int, default=20000)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args()

    vocab = make_vocabulary(args.vocab)
    corpus = make_corpus(args.docs, args.tokens, vocab, args.seed)
    rng = random.Random(args.seed + 1)

    with tempfile.TemporaryDirectory() as tmp:
        index = Index(Path(tmp) / "bench.db")
        started = time.perf_counter()
        ids = [
            index.add(
                title=f"d{i}", source_name=f"d{i}.txt", media_type="text/plain", text=text
            ).document.id
            for i, text in enumerate(corpus)
        ]
        elapsed = time.perf_counter() - started
        stats = index.stats()

        common, rare = vocab[:20], vocab[len(vocab) // 2 :]
        workloads = {
            "rare terms (AND)": (
                [" ".join(rng.sample(rare, 2)) for _ in range(args.queries)],
                "all",
            ),
            "mixed terms (AND)": (
                [f"{rng.choice(common)} {rng.choice(rare)}" for _ in range(args.queries)],
                "all",
            ),
            "common terms (OR)": (
                [" ".join(rng.sample(common, 3)) for _ in range(args.queries)],
                "any",
            ),
        }
        latency = {name: timed_queries(index, qs, mode) for name, (qs, mode) in workloads.items()}

        doc_freq = Counter(t for text in corpus for t in {tok.term for tok in tokenize(text)})
        trials, correct = 0, 0
        for position in rng.sample(range(args.docs), min(args.queries, args.docs)):
            terms = list(dict.fromkeys(t.term for t in tokenize(corpus[position])))
            distinctive = sorted(terms, key=lambda t: (doc_freq[t], t))[:3]  # the 3 rarest terms
            hits = index.search(" ".join(distinctive), mode="all", limit=1).hits
            trials += 1
            correct += bool(hits) and hits[0].document.id == ids[position]

        db_bytes = index.path.stat().st_size
        index.close()

    result: dict[str, Any] = {
        "corpus": {
            "documents": args.docs,
            "tokens_per_document": args.tokens,
            "vocabulary": args.vocab,
            "distinct_terms_indexed": stats.terms,
            "postings": stats.postings,
            "db_megabytes": round(db_bytes / 2**20, 1),
        },
        "indexing": {
            "seconds": round(elapsed, 2),
            "docs_per_second": round(args.docs / elapsed, 1),
        },
        "query_latency": latency,
        "known_item_recall_at_1": round(correct / trials, 3),
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(
        f"corpus   : {args.docs:,} docs x {args.tokens} tokens, {stats.terms:,} terms, "
        f"{stats.postings:,} postings, {result['corpus']['db_megabytes']} MB"
    )
    print(f"indexing : {result['indexing']['docs_per_second']} docs/s ({elapsed:.1f}s)")
    for name, row in latency.items():
        print(
            f"query    : {name:<20} p50 {row['p50_ms']} ms  "
            f"p95 {row['p95_ms']} ms  p99 {row['p99_ms']} ms"
        )
    print(f"known-item recall@1: {result['known_item_recall_at_1']:.1%} of {trials} queries")


if __name__ == "__main__":
    main()
