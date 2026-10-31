"""Query language: free text, "exact phrases", AND / OR / NOT, -exclusion, (grouping), filters.

    climate change                      both words (implicit AND)
    "climate change" policy             the exact phrase, and policy
    solar OR wind -coal                 (solar or wind) and not coal
    (solar OR wind) AND NOT nuclear     explicit operators and grouping
    report type:pdf lang:hi             filters; see `FILTER_FIELDS`

The parser is *total*: a search box can receive anything, so it never raises. An unclosed quote
runs to the end of the input, stray or unbalanced parentheses are ignored, a dangling operator
is dropped, and an unknown `field:value` is searched as plain words.

Precedence, loosest first: OR, AND (explicit or implicit), NOT / `-`. Operators are matched only
in upper case, so lower-case "and", "or", "not" stay ordinary words (and are stop words anyway).
Parsing yields an AST of raw words; analysis (stemming, stop words) is applied later, with the
same analyzer the index used, by `analyse`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from docsearch.analysis import query_terms, tokenize


def text_contains_phrase(text: str, terms: tuple[str, ...]) -> bool:
    """True if the analysed `terms` occur consecutively in `text` (stop words are not analysed
    tokens, so they neither help nor break a match)."""
    tokens = [t.term for t in tokenize(text)]
    width = len(terms)
    if width == 0 or len(tokens) < width:
        return False
    first = terms[0]
    return any(
        tokens[i] == first and tuple(tokens[i : i + width]) == terms
        for i in range(len(tokens) - width + 1)
    )


FILTER_FIELDS = frozenset({"type", "lang", "ext", "before", "after"})
MAX_QUERY_CHARS = 1000  # longer input is cut: nothing useful is 1000+ characters of query
MAX_NODES = 64  # bounds work for a hostile query such as thousands of terms or nested groups
MAX_DEPTH = 8


@dataclass(frozen=True, slots=True)
class Word:
    text: str


@dataclass(frozen=True, slots=True)
class Phrase:
    words: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Filter:
    field: str
    value: str


@dataclass(frozen=True, slots=True)
class Not:
    child: Node


@dataclass(frozen=True, slots=True)
class And:
    children: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Or:
    children: tuple[Node, ...]


Node = Word | Phrase | Filter | Not | And | Or

_TOKEN = re.compile(
    r"""
    (?P<phrase>"[^"]*"?)                      # "exact phrase", closing quote optional
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<minus>-(?=[^\s)-]))                   # -word: only when attached to something
  | (?P<filter>[A-Za-z]+:(?:"[^"]*"|[^\s()"]+))
  | (?P<word>[^\s()"]+)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str  # phrase | lparen | rparen | minus | filter | word | and | or | not
    value: str


def _lex(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    for match in _TOKEN.finditer(text[:MAX_QUERY_CHARS]):
        kind = match.lastgroup or "word"
        value = match.group()
        if kind == "word" and value in ("AND", "OR", "NOT"):
            kind = value.lower()
        tokens.append(_Tok(kind, value))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Tok]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._nodes = 0

    def _peek(self) -> _Tok | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def parse(self) -> Node | None:
        node = self._or(0)
        # Anything left is a stray ")" or text after one: skip it and keep parsing.
        while self._pos < len(self._tokens):
            self._pos += 1
            rest = self._or(0)
            if rest is not None:
                node = rest if node is None else And((node, rest))
        return node

    def _or(self, depth: int) -> Node | None:
        parts = [p for p in [self._and(depth)] if p is not None]
        while (tok := self._peek()) is not None and tok.kind == "or":
            self._pos += 1
            part = self._and(depth)
            if part is not None:
                parts.append(part)
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else Or(tuple(parts))

    def _and(self, depth: int) -> Node | None:
        parts: list[Node] = []
        while (tok := self._peek()) is not None and tok.kind not in ("or", "rparen"):
            if tok.kind == "and":
                self._pos += 1
                continue
            node = self._unary(depth)
            if node is not None:
                parts.append(node)
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else And(tuple(parts))

    def _unary(self, depth: int) -> Node | None:  # noqa: PLR0911 - one return per token kind
        tok = self._peek()
        assert tok is not None
        if tok.kind in ("not", "minus"):
            self._pos += 1
            nxt = self._peek()
            if nxt is None or nxt.kind in ("or", "rparen"):
                return None  # dangling NOT
            child = self._unary(depth)
            return None if child is None else Not(child)
        self._pos += 1
        if self._nodes >= MAX_NODES:
            return None
        self._nodes += 1
        if tok.kind == "lparen":
            if depth >= MAX_DEPTH:
                return None
            inner = self._or(depth + 1)
            closing = self._peek()
            if closing is not None and closing.kind == "rparen":
                self._pos += 1
            return inner
        if tok.kind == "phrase":
            words = tuple(tok.value.strip('"').split())
            if not words:
                return None
            return Word(words[0]) if len(words) == 1 else Phrase(words)
        if tok.kind == "filter":
            field, _, value = tok.value.partition(":")
            value = value.strip('"')
            if field.lower() in FILTER_FIELDS and value:
                return Filter(field.lower(), value)
            return And(tuple(Word(w) for w in re.split(r"[:\s]+", tok.value.strip('"')) if w))
        return Word(tok.value)


def parse(text: str) -> Node | None:
    """Parse `text` into an AST, or None if it contains nothing searchable."""
    return _Parser(_lex(text)).parse()


# ------------------------------------------------------------------------------------ analysis


@dataclass(frozen=True, slots=True)
class ATerm:
    term: str


@dataclass(frozen=True, slots=True)
class APhrase:
    terms: tuple[str, ...]  # analysed, in order; stop words are gone (see ADR on positions)


@dataclass(frozen=True, slots=True)
class ANot:
    child: ANode


@dataclass(frozen=True, slots=True)
class AAnd:
    children: tuple[ANode, ...]


@dataclass(frozen=True, slots=True)
class AOr:
    children: tuple[ANode, ...]


@dataclass(frozen=True, slots=True)
class AFilter:
    field: str
    value: str


ANode = ATerm | APhrase | ANot | AAnd | AOr | AFilter


def analyse(node: Node | None) -> ANode | None:  # noqa: PLR0911 - one return per node type
    """Run the index analyzer over every word, dropping nodes that analyse to nothing.

    "the" analyses to no terms and vanishes; a phrase left with one term becomes that term. A NOT
    whose operand vanished is dropped rather than excluding everything.
    """
    if node is None:
        return None
    if isinstance(node, Filter):
        return AFilter(node.field, node.value)
    if isinstance(node, Word):
        word_terms = query_terms(node.text)
        if not word_terms:
            return None
        if len(word_terms) == 1:
            return ATerm(word_terms[0])
        return AAnd(tuple(ATerm(t) for t in word_terms))
    if isinstance(node, Phrase):
        terms = tuple(t for w in node.words for t in query_terms(w))
        if not terms:
            return None
        return ATerm(terms[0]) if len(terms) == 1 else APhrase(terms)
    if isinstance(node, Not):
        child = analyse(node.child)
        return None if child is None else ANot(child)
    kids = [k for c in node.children if (k := analyse(c)) is not None]
    if not kids:
        return None
    if len(kids) == 1:
        return kids[0]
    return AAnd(tuple(kids)) if isinstance(node, And) else AOr(tuple(kids))
