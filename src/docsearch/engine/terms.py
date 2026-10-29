"""Term dictionary operations: prefix completion and typo-tolerant lookup.

`TermDictionary` is the in-memory reference for what the storage layer must provide. In Postgres the
same two operations are an index range scan on the term column (prefix) and `pg_trgm` candidate
generation followed by this module's `edit_distance` (fuzzy); see ADR 0007.

Fuzzy lookup uses deletion neighbourhoods (the SymSpell idea): two words are within edit distance
d only if some word reachable by deleting up to d characters from each is shared, so candidates
come from dictionary lookups instead of comparing the query to every term. Candidates are then
verified with a true distance, so false positives from the index never reach the caller.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations

MAX_EDITS = 2


def max_edits_for(term: str) -> int:
    """Short words tolerate fewer typos: one edit changes a 3-letter word into another word."""
    length = len(term)
    if length <= 3:
        return 0
    return 1 if length <= 7 else MAX_EDITS


def edit_distance(a: str, b: str, limit: int) -> int | None:
    """Damerau-Levenshtein (optimal string alignment) distance, or None if it exceeds `limit`.

    Counts insert, delete, substitute and swapping two adjacent characters as one edit each. Rows
    are abandoned as soon as every cell exceeds `limit`.
    """
    if abs(len(a) - len(b)) > limit:
        return None
    previous2: list[int] | None = None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if previous2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                value = min(value, previous2[j - 2] + 1)
            current[j] = value
        if min(current) > limit:
            return None
        previous2, previous = previous, current
    distance = previous[-1]
    return distance if distance <= limit else None


def _deletes(term: str, depth: int) -> set[str]:
    """Every string obtained by deleting up to `depth` characters from `term` (including itself)."""
    results = {term}
    for removed in range(1, min(depth, len(term)) + 1):
        for positions in combinations(range(len(term)), removed):
            drop = set(positions)
            results.add("".join(ch for i, ch in enumerate(term) if i not in drop))
    return results


@dataclass(frozen=True, slots=True)
class Suggestion:
    term: str
    doc_freq: int
    distance: int = 0


class TermDictionary:
    def __init__(self, doc_freqs: Mapping[str, int]) -> None:
        self._freq = dict(doc_freqs)
        self._sorted = sorted(self._freq)
        self._deletes: dict[str, list[str]] | None = None  # built on first fuzzy lookup

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, int]]) -> TermDictionary:
        return cls(dict(pairs))

    def __len__(self) -> int:
        return len(self._freq)

    def doc_freq(self, term: str) -> int:
        return self._freq.get(term, 0)

    def complete(self, prefix: str, limit: int = 10) -> list[Suggestion]:
        """Terms starting with `prefix`, most frequent first (ties alphabetical)."""
        if not prefix or limit < 1:
            return []
        start = bisect.bisect_left(self._sorted, prefix)
        matches: list[Suggestion] = []
        for index in range(start, len(self._sorted)):
            term = self._sorted[index]
            if not term.startswith(prefix):
                break
            matches.append(Suggestion(term, self._freq[term]))
        matches.sort(key=lambda s: (-s.doc_freq, s.term))
        return matches[:limit]

    def fuzzy(self, term: str, limit: int = 5) -> list[Suggestion]:
        """Known terms within the typo budget of `term`, closest then most frequent first.

        An exact match is returned with distance 0 and, when present, wins every tie.
        """
        budget = max_edits_for(term)
        if self._deletes is None:
            self._deletes = self._build_deletes()
        found: dict[str, int] = {}
        if term in self._freq:
            found[term] = 0
        if budget > 0:
            for variant in _deletes(term, budget):
                for candidate in self._deletes.get(variant, ()):
                    if candidate in found:
                        continue
                    distance = edit_distance(term, candidate, budget)
                    if distance is not None:
                        found[candidate] = distance
        ranked = [Suggestion(t, self._freq[t], d) for t, d in found.items()]
        ranked.sort(key=lambda s: (s.distance, -s.doc_freq, s.term))
        return ranked[:limit]

    def _build_deletes(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = defaultdict(list)
        for term in self._freq:
            for variant in _deletes(term, MAX_EDITS):
                index[variant].append(term)
        return dict(index)
