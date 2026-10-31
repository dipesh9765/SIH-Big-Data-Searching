"""Exact evaluation of boolean queries: AND / OR / NOT, grouping, exact phrases.

Unlike `search.py` (top-k with pruning, for the common "these words" query), this evaluates the
query tree to the full set of matching documents with BM25 scores, using sorted id arrays and set
operations on them. That makes it exact and simple to reason about, at the cost of decoding every
list the query touches, so it is used only when the query needs operators.

Semantics:
* a term matches the documents containing it; its score is the BM25 term score;
* AND intersects (scores add), OR unions (scores add), `NOT x` removes the documents containing x
  from the AND it sits in. A query with no positive clause (only `NOT x`) matches nothing: the
  engine does not enumerate "every document except";
* a `NOT` directly under an OR has no positive meaning and is ignored;
* a phrase is a conjunction of its words that is then *verified* against the document text by the
  caller (`verify`): there are no stored word positions (ADR 0014). Stop words are not part of the
  analysed phrase, so "x the y" and "x y" are the same phrase.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from docsearch.engine.scoring import BM25_B, BM25_K1, bm25_idf
from docsearch.engine.search import PostingsSource
from docsearch.query import AAnd, ANode, ANot, AOr, APhrase, ATerm

IdArray = npt.NDArray[np.int64]
ScoreArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class Matches:
    """Matching document ids (sorted, unique) and their scores."""

    ids: IdArray
    scores: ScoreArray

    @staticmethod
    def empty() -> Matches:
        return Matches(np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64))


# Given candidate ids (best score first) and the analysed phrase, the ids whose text contains it.
PhraseVerifier = Callable[[IdArray, tuple[str, ...]], IdArray]


def term_matches(source: PostingsSource, term: str) -> Matches:
    df = source.doc_freq(term)
    if df == 0:
        return Matches.empty()
    ids_parts: list[list[int]] = []
    tf_parts: list[list[int]] = []
    for block in source.blocks(term):
        block_ids, block_tfs = block.decode()
        ids_parts.append(block_ids)
        tf_parts.append(block_tfs)
    ids = np.fromiter((i for part in ids_parts for i in part), dtype=np.int64)
    tfs = np.fromiter((t for part in tf_parts for t in part), dtype=np.float64)
    if not ids.size:
        return Matches.empty()
    idf = bm25_idf(source.doc_count, df)
    lengths = source.length_table()[ids]
    norm = BM25_K1 * (1.0 - BM25_B + BM25_B * lengths / source.average_length)
    return Matches(ids, idf * tfs * (BM25_K1 + 1.0) / (tfs + norm))


def _intersect(a: Matches, b: Matches) -> Matches:
    ids, in_a, in_b = np.intersect1d(a.ids, b.ids, assume_unique=True, return_indices=True)
    return Matches(ids, a.scores[in_a] + b.scores[in_b])


def _union(a: Matches, b: Matches) -> Matches:
    ids = np.union1d(a.ids, b.ids)
    scores = np.zeros(ids.size, dtype=np.float64)
    scores[np.searchsorted(ids, a.ids)] += a.scores
    scores[np.searchsorted(ids, b.ids)] += b.scores
    return Matches(ids, scores)


def _without(a: Matches, drop: IdArray) -> Matches:
    keep = ~np.isin(a.ids, drop, assume_unique=True)
    return Matches(a.ids[keep], a.scores[keep])


@dataclass(slots=True)
class Evaluator:
    source: PostingsSource
    verify: PhraseVerifier
    phrase_candidates: int = (
        1000  # at most this many best-scoring candidates are checked per phrase
    )
    approximate: bool = False  # set when a phrase had more candidates than the cap

    def evaluate(self, node: ANode) -> Matches:
        """All documents matching `node`, before the deletion filter."""
        if isinstance(node, ATerm):
            return term_matches(self.source, node.term)
        if isinstance(node, APhrase):
            return self._phrase(node, None)
        if isinstance(node, AAnd):
            return self._and(node)
        if isinstance(node, AOr):
            result = Matches.empty()
            for child in node.children:
                if not isinstance(child, ANot):
                    result = _union(result, self.evaluate(child))
            return result
        return Matches.empty()  # a bare NOT, or a filter reaching here: nothing to enumerate

    def _and(self, node: AAnd) -> Matches:
        positives = [c for c in node.children if not isinstance(c, ANot)]
        negatives = [c.child for c in node.children if isinstance(c, ANot)]
        plain = [c for c in positives if not isinstance(c, APhrase)]
        phrases = [c for c in positives if isinstance(c, APhrase)]
        if not plain and not phrases:
            return Matches.empty()
        result: Matches | None = None
        for child in sorted(plain, key=self._cost):
            found = self.evaluate(child)
            result = found if result is None else _intersect(result, found)
            if not result.ids.size:
                return result
        for phrase in phrases:  # verified last, only among documents that already match the rest
            result = self._phrase(phrase, result)
            if not result.ids.size:
                return result
        assert result is not None
        for negative in negatives:
            result = _without(result, self.evaluate(negative).ids)
        return result

    def _cost(self, node: ANode) -> int:
        """Cheapest first: a rare term shrinks the working set before common ones are decoded."""
        if isinstance(node, ATerm):
            return self.source.doc_freq(node.term)
        return 1 << 40

    def _phrase(self, node: APhrase, within: Matches | None) -> Matches:
        candidates: Matches | None = within
        for term in node.terms:
            found = term_matches(self.source, term)
            candidates = found if candidates is None else _intersect(candidates, found)
            if not candidates.ids.size:
                return Matches.empty()
        assert candidates is not None
        order = np.argsort(-candidates.scores, kind="stable")
        if order.size > self.phrase_candidates:
            self.approximate = True
            order = order[: self.phrase_candidates]
        ranked_ids = candidates.ids[order]
        verified = self.verify(ranked_ids, node.terms)
        keep = np.isin(candidates.ids, verified, assume_unique=True)
        return Matches(candidates.ids[keep], candidates.scores[keep])


def top_k(
    matches: Matches, excluded: frozenset[int], k: int
) -> tuple[list[tuple[int, float]], int]:
    """The `k` best (id, score) pairs, ties by ascending id, and the number of live matches."""
    ids, scores = matches.ids, matches.scores
    if excluded:
        gone = np.fromiter(excluded, dtype=np.int64, count=len(excluded))
        keep = ~np.isin(ids, gone)
        ids, scores = ids[keep], scores[keep]
    total = int(ids.size)
    order = np.lexsort((ids, -scores))[:k]
    return [(int(ids[i]), float(scores[i])) for i in order], total
