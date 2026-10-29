"""Top-k retrieval over block postings: block-max WAND for OR, skip-driven intersection for AND.

The SQLite reference index scores *every* posting of every query term. That is exact, and its cost
grows with the length of the postings lists, so a query containing a common word touches a large
share of the corpus. Here, lists are cut into blocks that carry a score upper bound, and a document
is only decoded and scored if its optimistic best score could still enter the current top-k:

* a threshold is the k-th best score so far (it only ever rises);
* a document whose best-case score is not above the threshold cannot change the result, so whole
  blocks are skipped without being decompressed.

Results are identical to exhaustive scoring (tested differentially against the SQLite index),
including tie-breaking: score descending, then document id ascending.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from docsearch.engine.codec import Block
from docsearch.engine.scoring import bm25_idf, bm25_term_score

Mode = Literal["all", "any"]
_END = 1 << 62  # larger than any document id


class PostingsSource(Protocol):
    """What the search needs from a storage backend. Memory and Postgres both implement this."""

    doc_count: int
    average_length: float

    def doc_freq(self, term: str) -> int: ...

    def blocks(self, term: str) -> Sequence[Block]: ...

    def length(self, doc_id: int) -> int: ...


@dataclass(frozen=True, slots=True)
class ScoredDoc:
    doc_id: int
    score: float


@dataclass(frozen=True, slots=True)
class SearchStats:
    """Work counters, so a benchmark can show *why* a query was fast or slow."""

    blocks_decoded: int = 0
    postings_scored: int = 0
    blocks_total: int = 0


@dataclass(frozen=True, slots=True)
class Outcome:
    hits: list[ScoredDoc]
    stats: SearchStats
    total: int | None  # exact number of matching documents, or None when pruning made it unknown


class _Cursor:
    """A forward-only position in one term's postings list that decodes blocks lazily."""

    __slots__ = (
        "_average_length",
        "_block_no",
        "_blocks",
        "_bound_now",
        "_ids",
        "_pos",
        "_source",
        "_tfs",
        "decoded",
        "doc",
        "idf",
        "list_max",
    )

    def __init__(self, source: PostingsSource, term: str, idf: float) -> None:
        self._source = source
        self._average_length = source.average_length
        self._blocks = source.blocks(term)
        self.idf = idf
        self._block_no = 0
        self._ids: list[int] = []
        self._tfs: list[int] = []
        self._pos = 0
        self._bound_now = 0.0
        self.decoded = 0
        self.list_max = max(
            (self._bound(b) for b in self._blocks),
            default=0.0,
        )
        self.doc = self._blocks[0].first_doc if self._blocks else _END
        if self._blocks:
            self._load(0)

    def _bound(self, block: Block) -> float:
        return bm25_term_score(self.idf, block.max_tf, block.min_len, self._average_length)

    # -- block-level (cheap, never decompresses) ---------------------------------------------

    def block_bound(self) -> float:
        """Best score any posting in the *current* block can reach."""
        return self._bound_now if self.doc != _END else 0.0

    def block_end(self) -> int:
        """Last document id covered by the current block."""
        return self._blocks[self._block_no].last_doc if self.doc != _END else _END

    # -- posting-level -----------------------------------------------------------------------

    def _load(self, block_no: int) -> None:
        self._block_no = block_no
        self._ids, self._tfs = self._blocks[block_no].decode()
        self._pos = 0
        self._bound_now = self._bound(self._blocks[block_no])
        self.decoded += 1

    def tf(self) -> int:
        return self._tfs[self._pos]

    def advance_to(self, target: int) -> None:
        """Move to the first posting with id >= target (or the end)."""
        if self.doc >= target:
            return
        if self.doc == _END:
            return
        blocks = self._blocks
        if blocks[self._block_no].last_doc < target:
            block_no = self._block_no + 1
            while block_no < len(blocks) and blocks[block_no].last_doc < target:
                block_no += 1  # skipped blocks are never decompressed
            if block_no == len(blocks):
                self.doc = _END
                return
            self._load(block_no)
        ids = self._ids
        pos = self._pos
        while ids[pos] < target:
            pos += 1
        self._pos = pos
        self.doc = ids[pos]

    def next(self) -> None:
        self._pos += 1
        if self._pos < len(self._ids):
            self.doc = self._ids[self._pos]
        elif self._block_no + 1 < len(self._blocks):
            self._load(self._block_no + 1)
            self.doc = self._ids[0]
        else:
            self.doc = _END


class _TopK:
    """Keeps the k best (score, -doc_id) pairs; documents arrive in ascending id order."""

    def __init__(self, k: int) -> None:
        self._k = k
        self._heap: list[tuple[float, int]] = []

    @property
    def threshold(self) -> float:
        """A document must score strictly above this to enter once the heap is full.

        Strictly, because documents are visited in ascending id order: a later document with an
        equal score loses the tie-break, so it can never displace an earlier one.
        """
        return self._heap[0][0] if len(self._heap) >= self._k else float("-inf")

    def offer(self, doc_id: int, score: float) -> None:
        item = (score, -doc_id)
        if len(self._heap) < self._k:
            heapq.heappush(self._heap, item)
        elif score > self._heap[0][0]:
            heapq.heapreplace(self._heap, item)

    def result(self) -> list[ScoredDoc]:
        ordered = sorted(self._heap, key=lambda item: (-item[0], -item[1]))
        return [ScoredDoc(-neg_id, score) for score, neg_id in ordered]


def search(
    source: PostingsSource,
    terms: Sequence[str],
    *,
    mode: Mode = "any",
    k: int = 10,
    exact_total: bool = False,
) -> Outcome:
    """Top-`k` documents for already-analysed `terms`.

    `exact_total=True` disables pruning so the number of matching documents is exact; without it
    `Outcome.total` is only set when the algorithm happened to visit every match.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    if not terms or source.doc_count == 0:
        return Outcome([], SearchStats(), 0)
    distinct = list(dict.fromkeys(terms))
    cursors: list[_Cursor] = []
    for term in distinct:
        df = source.doc_freq(term)
        if df == 0:
            if mode == "all":
                return Outcome([], SearchStats(), 0)
            continue
        cursors.append(_Cursor(source, term, bm25_idf(source.doc_count, df)))
    if not cursors:
        return Outcome([], SearchStats(), 0)
    if mode == "all":
        return _and(source, cursors, k, exact_total)
    return _wand(source, cursors, k, exact_total)


def _score(source: PostingsSource, cursor: _Cursor, doc_id: int) -> float:
    return bm25_term_score(cursor.idf, cursor.tf(), source.length(doc_id), source.average_length)


def _stats(cursors: Sequence[_Cursor], scored: int) -> SearchStats:
    return SearchStats(
        blocks_decoded=sum(c.decoded for c in cursors),
        postings_scored=scored,
        blocks_total=sum(len(c._blocks) for c in cursors),
    )


def _and(source: PostingsSource, cursors: list[_Cursor], k: int, exact_total: bool) -> Outcome:
    """Every term must match. Drive from the rarest list and skip the others forward to it."""
    cursors.sort(key=lambda c: len(c._blocks))
    lead, rest = cursors[0], cursors[1:]
    top = _TopK(k)
    scored = 0
    matches = 0
    bound_total = sum(c.list_max for c in cursors)
    while lead.doc != _END:
        doc = lead.doc
        if not exact_total and bound_total <= top.threshold:
            break  # no document anywhere can beat the current k-th score any more
        aligned = True
        for cursor in rest:
            cursor.advance_to(doc)
            if cursor.doc != doc:
                aligned = False
                doc = cursor.doc
                break
        if not aligned:
            if doc == _END:
                break
            lead.advance_to(doc)
            continue
        if not exact_total and sum(c.block_bound() for c in cursors) <= top.threshold:
            lead.next()
            continue
        length = source.length(doc)
        score = sum(bm25_term_score(c.idf, c.tf(), length, source.average_length) for c in cursors)
        scored += 1
        matches += 1
        top.offer(doc, score)
        lead.next()
    return Outcome(top.result(), _stats(cursors, scored), matches if exact_total else None)


def _wand(source: PostingsSource, cursors: list[_Cursor], k: int, exact_total: bool) -> Outcome:
    """At least one term must match: block-max WAND (Ding & Suel, 2011)."""
    top = _TopK(k)
    scored = 0
    matches = 0
    while True:
        active = [c for c in cursors if c.doc != _END]
        if not active:
            break
        active.sort(key=lambda c: c.doc)
        if exact_total:
            doc = active[0].doc
        else:
            pivot = _pivot(active, top.threshold)
            if pivot is None:
                break
            if pivot == -1:
                continue
            doc = pivot
        total = 0.0
        length = source.length(doc)
        for cursor in active:
            if cursor.doc != doc:
                break
            total += bm25_term_score(cursor.idf, cursor.tf(), length, source.average_length)
            cursor.next()
        scored += 1
        matches += 1
        top.offer(doc, total)
    return Outcome(top.result(), _stats(cursors, scored), matches if exact_total else None)


def _pivot(active: list[_Cursor], threshold: float) -> int | None:
    """Pick the next document worth scoring, or skip ahead; None means nothing can qualify.

    Returns the document id to score, or -1 after moving cursors past a region that cannot
    contain a winner (the caller then re-sorts and tries again).
    """
    # 1. List-level WAND: the smallest prefix of cursors whose list maxima could beat the threshold.
    acc = 0.0
    pivot_index = -1
    for index, cursor in enumerate(active):
        acc += cursor.list_max
        if acc > threshold:
            pivot_index = index
            break
    if pivot_index == -1:
        return None
    pivot_doc = active[pivot_index].doc
    while pivot_index + 1 < len(active) and active[pivot_index + 1].doc == pivot_doc:
        pivot_index += 1
    if active[0].doc != pivot_doc:
        # Documents before the pivot cannot reach the threshold: skip the leaders to it.
        for cursor in active[:pivot_index]:
            if cursor.doc < pivot_doc:
                cursor.advance_to(pivot_doc)
        return -1
    # 2. Block-level check: do the *blocks* covering the pivot still allow a winner?
    block_sum = sum(c.block_bound() for c in active[: pivot_index + 1])
    if block_sum > threshold:
        return pivot_doc
    # Skip to the nearest place where the answer could change: the end of the earliest block
    # in play, or the next document of a cursor that was not part of the pivot prefix.
    next_candidate = min(c.block_end() for c in active[: pivot_index + 1]) + 1
    if pivot_index + 1 < len(active):
        next_candidate = min(next_candidate, active[pivot_index + 1].doc)
    next_candidate = max(next_candidate, pivot_doc + 1)
    for cursor in active[: pivot_index + 1]:
        cursor.advance_to(next_candidate)
    return -1
