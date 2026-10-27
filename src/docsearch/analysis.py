"""Text analysis: the same pipeline runs on documents at index time and on queries at search time.

Pipeline: Unicode word tokens -> casefold -> drop stop words -> Snowball (English) stem.
Using one function for both sides is what makes "running" match "runs"; the legacy prototype
stored unstemmed tokens and looked up the raw query, so nothing ever matched.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import snowballstemmer

# Letters and digits, not underscores; an internal apostrophe stays inside the token ("don't").
_CURLY_APOSTROPHE = chr(0x2019)
_TOKEN = re.compile(rf"[^\W_]+(?:['{_CURLY_APOSTROPHE}][^\W_]+)*")
MAX_TOKEN_LENGTH = 40  # longer runs are base64 blobs, hashes or garbage, not words

STOP_WORDS: frozenset[str] = frozenset(
    """a about above after again against all am an and any are aren't as at be because been before
    being below between both but by can can't cannot could couldn't did didn't do does doesn't doing
    don't down during each few for from further had hadn't has hasn't have haven't having he he'd
    he'll he's her here here's hers herself him himself his how how's i i'd i'll i'm i've if in
    into is isn't it it's its itself let's me more most mustn't my myself no nor not of off on once
    only or other ought our ours ourselves out over own same shan't she she'd she'll she's should
    shouldn't so some such than that that's the their theirs them themselves then there there's
    these they they'd they'll they're they've this those through to too under until up very was
    wasn't we we'd we'll we're we've were weren't what what's when when's where where's which while
    who who's whom why why's with won't would wouldn't you you'd you'll you're you've your yours
    yourself yourselves""".split()  # noqa: SIM905 - a list literal here would be 170 lines
)

_stemmer = snowballstemmer.stemmer("english")


@lru_cache(maxsize=100_000)
def stem(word: str) -> str:
    """Stem one lower-case word. Cached: natural-language vocabularies are small and Zipfian."""
    result: str = _stemmer.stemWord(word)
    return result


@dataclass(frozen=True, slots=True)
class Token:
    """An indexed term together with where its source word sits in the original text."""

    term: str
    start: int
    end: int


def tokenize(text: str) -> list[Token]:
    """Return the indexable tokens of `text` in order, with character offsets into `text`."""
    tokens: list[Token] = []
    for match in _TOKEN.finditer(text):
        word = match.group().casefold().replace(_CURLY_APOSTROPHE, "'")
        if len(word) > MAX_TOKEN_LENGTH or word in STOP_WORDS:
            continue
        tokens.append(Token(stem(word), match.start(), match.end()))
    return tokens


def term_frequencies(text: str) -> tuple[Counter[str], int]:
    """Return ({term: count}, total indexed tokens) for `text`."""
    counts = Counter(token.term for token in tokenize(text))
    return counts, sum(counts.values())


def query_terms(query: str) -> list[str]:
    """Distinct analysed terms of a query, in first-appearance order."""
    return list(dict.fromkeys(token.term for token in tokenize(query)))
