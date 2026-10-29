"""In-memory block index: the smallest possible `PostingsSource`.

Used by tests (to check the search algorithm against the SQLite reference without any database)
and by the micro-benchmark (to separate the algorithm's cost from storage cost).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

import numpy as np
import numpy.typing as npt

from docsearch.analysis import term_frequencies
from docsearch.engine.codec import Block, build_blocks


class MemoryIndex:
    def __init__(self) -> None:
        self._lengths: dict[int, int] = {}
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._blocks: dict[str, list[Block]] = {}
        self.doc_count = 0
        self.average_length = 0.0
        self._total_length = 0
        self._table: npt.NDArray[np.int64] | None = None

    def add(self, doc_id: int, text: str) -> None:
        """Add a document. Ids must arrive in ascending order (as an auto-increment key does)."""
        counts, length = term_frequencies(text)
        if length == 0:
            return
        self.add_counts(doc_id, counts, length)

    def add_counts(self, doc_id: int, counts: dict[str, int], length: int) -> None:
        if doc_id in self._lengths:
            raise ValueError(f"document {doc_id} already indexed")
        self._lengths[doc_id] = length
        for term, tf in counts.items():
            self._postings[term].append((doc_id, tf))
        self.doc_count += 1
        self._total_length += length
        self._blocks.clear()  # invalidate; rebuilt on demand by `finish`
        self._table = None

    def finish(self) -> None:
        """Compress every postings list into blocks. Call after the last `add`."""
        self.average_length = self._total_length / self.doc_count if self.doc_count else 0.0
        self._blocks = {
            term: build_blocks(sorted(postings), self._lengths)
            for term, postings in self._postings.items()
        }

    def extend(self, documents: Iterable[tuple[int, str]]) -> None:
        for doc_id, text in documents:
            self.add(doc_id, text)
        self.finish()

    # -- PostingsSource -------------------------------------------------------------------

    def doc_freq(self, term: str) -> int:
        return len(self._postings.get(term, ()))

    def blocks(self, term: str) -> Sequence[Block]:
        return self._blocks.get(term, [])

    def length(self, doc_id: int) -> int:
        return self._lengths[doc_id]

    def length_table(self) -> npt.NDArray[np.int64]:
        if self._table is None:
            table = np.zeros(max(self._lengths, default=0) + 1, dtype=np.int64)
            for doc_id, length in self._lengths.items():
                table[doc_id] = length
            self._table = table
        return self._table

    # -- inspection -----------------------------------------------------------------------

    def payload_bytes(self) -> int:
        return sum(len(b.payload) for blocks in self._blocks.values() for b in blocks)

    def posting_count(self) -> int:
        return sum(len(p) for p in self._postings.values())
