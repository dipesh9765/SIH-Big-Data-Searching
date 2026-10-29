"""Text analysis: the same pipeline runs on documents at index time and on queries at search time.

Pipeline: Unicode word tokens -> casefold + NFC -> drop stop words -> Snowball stem, chosen per
token by script (English for Latin text, Hindi for Devanagari), so mixed-script text works.
Using one function for both sides is what makes "running" match "runs"; the legacy prototype
stored unstemmed tokens and looked up the raw query, so nothing ever matched.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import snowballstemmer


def _mark_ranges() -> str:
    """Regex character-class body for every combining mark (categories Mn, Mc, Me) in the BMP.

    Python's `\\w` does not match combining marks, so without this a Hindi word is cut into pieces
    at every vowel sign (matra): "किताबें" would become "क", "त", "ब". Marks are part of words in
    every Indic script, so they are allowed inside tokens regardless of script.
    """
    ranges: list[tuple[int, int]] = []
    for code in range(0x300, min(sys.maxunicode, 0xFFFF) + 1):
        if unicodedata.category(chr(code)).startswith("M"):
            if ranges and ranges[-1][1] == code - 1:
                ranges[-1] = (ranges[-1][0], code)
            else:
                ranges.append((code, code))
    return "".join(f"\\u{lo:04x}" if lo == hi else f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in ranges)


# Letters and digits, not underscores, plus combining marks and the zero-width (non-)joiners that
# Indic scripts use inside words; an internal apostrophe stays inside the token ("don't").
_CURLY_APOSTROPHE = chr(0x2019)
_MARKS = _mark_ranges() + "\\u200c\\u200d"
_UNIT = rf"(?:[^\W_]|[{_MARKS}])"
_TOKEN = re.compile(rf"{_UNIT}+(?:['{_CURLY_APOSTROPHE}]{_UNIT}+)*")
MAX_TOKEN_LENGTH = 40  # longer runs are base64 blobs, hashes or garbage, not words
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")  # Hindi, Marathi, Sanskrit, Nepali, ...
_DEVANAGARI_DIGITS = {0x966 + n: str(n) for n in range(10)}

# Hindi and Marathi function words (hand-picked, high frequency). Matched before stemming.
DEVANAGARI_STOP_WORDS: frozenset[str] = frozenset(
    """का के की को में से पर है हैं था थे थी थीं हो होता होती होते हुआ हुई हुए और या कि जो यह वह ये वे
    एक भी तो ही ने इस उस इन उन कोई कुछ सब हम तुम आप मैं नहीं लिए साथ तक क्या जब तब अपने रहा रही रहे
    गया गई गए सकता सकती सकते द्वारा लेकिन अगर बहुत कर करके करना
    आहे आहेत च ची चे चा आणि व या हा हे ही ती ते मी तू आम्ही तुम्ही ला ना नाही पण मध्ये साठी कडे
    कडून होते होता होती तर जर म्हणून काही सर्व""".split()  # noqa: SIM905
)

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
_hindi_stemmer = snowballstemmer.stemmer("hindi")


@lru_cache(maxsize=100_000)
def stem(word: str) -> str:
    """Stem one lower-case word. Cached: natural-language vocabularies are small and Zipfian."""
    result: str = _stemmer.stemWord(word)
    return result


@lru_cache(maxsize=100_000)
def stem_devanagari(word: str) -> str:
    """Hindi light stemmer (Snowball). Also used for Marathi: same script and similar suffixes,
    but Marathi-specific morphology (case endings such as -ाचा) is not handled."""
    result: str = _hindi_stemmer.stemWord(word)
    # The stemmer can strip a short word down to one character ("नई" -> "न"), which would collide
    # with unrelated words; keep the original in that case.
    return result if len(result) >= 2 else word


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
        raw = match.group()
        if not any(ch.isalnum() for ch in raw):
            continue  # only marks or joiners
        # NFC so precomposed and decomposed spellings (e.g. ढ़ as one code point or ढ + nukta)
        # become one term. Done per token: offsets into `text` must stay valid for highlighting.
        word = unicodedata.normalize("NFC", raw.casefold().replace(_CURLY_APOSTROPHE, "'"))
        if len(word) > MAX_TOKEN_LENGTH:
            continue
        if _DEVANAGARI.search(word):
            word = word.translate(_DEVANAGARI_DIGITS)
            if word in DEVANAGARI_STOP_WORDS:
                continue
            term = stem_devanagari(word)
        else:
            if word in STOP_WORDS:
                continue
            term = stem(word)
        tokens.append(Token(term, match.start(), match.end()))
    return tokens


def term_frequencies(text: str) -> tuple[Counter[str], int]:
    """Return ({term: count}, total indexed tokens) for `text`."""
    counts = Counter(token.term for token in tokenize(text))
    return counts, sum(counts.values())


def query_terms(query: str) -> list[str]:
    """Distinct analysed terms of a query, in first-appearance order."""
    return list(dict.fromkeys(token.term for token in tokenize(query)))
