"""BM25 scoring, shared by the SQLite reference index and the block engine."""

from __future__ import annotations

import math

BM25_K1 = 1.2  # term-frequency saturation
BM25_B = 0.75  # document-length normalisation


def bm25_idf(doc_count: int, doc_freq: int) -> float:
    """Robertson-Spärck Jones idf with the +1 inside the log so it is never negative."""
    return math.log(1.0 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))


def bm25_term_score(idf: float, tf: int, length: int, average_length: float) -> float:
    norm = 1.0 - BM25_B + BM25_B * length / average_length
    return idf * tf * (BM25_K1 + 1.0) / (tf + BM25_K1 * norm)
