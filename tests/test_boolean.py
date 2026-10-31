"""The query language against a brute-force oracle, in every storage state."""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import Iterator

import numpy as np
import pytest

from docsearch.analysis import script_of, tokenize
from docsearch.engine.boolean import Evaluator, Matches, term_matches, top_k
from docsearch.engine.memory import MemoryIndex
from docsearch.engine.pg import InvalidFilterError, NewDocument, PgIndex
from docsearch.query import analyse, parse, text_contains_phrase

# ---------------------------------------------------------------- phrase check and script tags


def test_phrase_match_is_consecutive_and_ignores_stop_words() -> None:
    phrase = tuple(t.term for t in tokenize("climate change"))
    assert text_contains_phrase("Climate change is real", phrase)
    assert text_contains_phrase("the climate and change", phrase)  # "and" is a stop word
    assert not text_contains_phrase("change in the climate", phrase)
    assert not text_contains_phrase("climate then something then change", phrase)
    assert not text_contains_phrase("climate", phrase)
    assert not text_contains_phrase("", phrase)
    assert not text_contains_phrase("anything", ())


def test_stop_words_between_phrase_words_do_not_matter() -> None:
    phrase = tuple(t.term for t in tokenize("sea level"))
    assert text_contains_phrase("the sea of the level", phrase)  # of/the are not tokens
    assert not text_contains_phrase("sea rising level", phrase)


def test_script_tags() -> None:
    assert script_of("plain english text") == "latn"
    assert script_of("भारत की राजधानी नई दिल्ली है") == "deva"
    assert script_of("Delhi दिल्ली Mumbai मुंबई") == "mixed"
    assert script_of("1234 !!") == ""


# ----------------------------------------------------------- evaluator over the in-memory index


def memory_corpus(texts: list[str]) -> MemoryIndex:
    memory = MemoryIndex()
    for number, text in enumerate(texts, start=1):
        memory.add(number, text)
    memory.finish()
    return memory


def brute_force_ids(texts: list[str], predicate) -> set[int]:  # type: ignore[no-untyped-def]
    return {
        n for n, text in enumerate(texts, start=1) if predicate({t.term for t in tokenize(text)})
    }


def run(memory: MemoryIndex, texts: list[str], query: str) -> set[int]:
    tree = analyse(parse(query))
    assert tree is not None

    def verify(ids: np.ndarray, phrase: tuple[str, ...]) -> np.ndarray:
        return np.array(
            [i for i in ids if text_contains_phrase(texts[int(i) - 1], phrase)], dtype=np.int64
        )

    matches = Evaluator(memory, verify).evaluate(tree)
    return {int(i) for i in matches.ids}


TEXTS = [
    "solar power plant and wind turbines",  # 1
    "wind energy report 2024",  # 2
    "solar panel cost report",  # 3
    "coal power plant emissions",  # 4
    "nuclear power and solar energy",  # 5
    "climate change policy and coal",  # 6
    "policy on change of climate",  # 7
    "wind solar coal nuclear all mentioned",  # 8
]


@pytest.fixture(scope="module")
def memory() -> MemoryIndex:
    return memory_corpus(TEXTS)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("solar", {1, 3, 5, 8}),
        ("solar wind", {1, 8}),
        ("solar OR wind", {1, 2, 3, 5, 8}),
        ("solar -wind", {3, 5}),
        ("solar NOT wind", {3, 5}),
        ("(solar OR wind) coal", {8}),
        ("(solar OR wind) -coal -nuclear", {1, 2, 3}),
        ("power AND (solar OR coal)", {1, 4, 5}),
        ('"climate change"', {6}),
        ('"climate change" policy', {6}),
        ('"climate change" OR "change of climate"', {6, 7}),
        ('policy -"climate change"', {7}),
        ('"power plant" -coal', {1}),
        ("-wind", set()),
        ("nonexistentword", set()),
        ("solar OR nonexistentword", {1, 3, 5, 8}),
        ("solar nonexistentword", set()),
    ],
)
def test_operators_match_the_brute_force_answer(
    memory: MemoryIndex, query: str, expected: set[int]
) -> None:
    assert run(memory, TEXTS, query) == expected


def test_a_negation_inside_or_is_ignored_not_inverted(memory: MemoryIndex) -> None:
    assert run(memory, TEXTS, "wind OR -solar") == {1, 2, 8}


def test_scores_add_across_and_and_or(memory: MemoryIndex) -> None:
    solar = term_matches(memory, "solar")
    wind = term_matches(memory, "wind")
    both = Evaluator(memory, lambda ids, _p: ids).evaluate(analyse(parse("solar wind")))  # type: ignore[arg-type]
    for doc in (1, 8):
        expected = float(
            solar.scores[list(solar.ids).index(doc)] + wind.scores[list(wind.ids).index(doc)]
        )
        assert float(both.scores[list(both.ids).index(doc)]) == pytest.approx(expected)


def test_random_trees_agree_with_set_algebra() -> None:
    rng = random.Random(8)
    vocabulary = [f"w{n}x" for n in range(12)]
    texts = [" ".join(rng.sample(vocabulary, rng.randint(2, 7))) for _ in range(150)]
    memory = memory_corpus(texts)
    sets = {w: brute_force_ids(texts, lambda terms, w=w: w in terms) for w in vocabulary}
    # tokens like w3x analyse to themselves; the assert below checks that every word is indexed
    assert all(sets.values())
    for _ in range(200):
        a, b, c, d = rng.sample(vocabulary, 4)
        cases = {
            f"{a} {b}": sets[a] & sets[b],
            f"{a} OR {b}": sets[a] | sets[b],
            f"{a} -{b}": sets[a] - sets[b],
            f"({a} OR {b}) {c}": (sets[a] | sets[b]) & sets[c],
            f"{a} {b} -{c} -{d}": (sets[a] & sets[b]) - sets[c] - sets[d],
            f"{a} ({b} OR {c}) -{d}": sets[a] & (sets[b] | sets[c]) - sets[d],
        }
        for query, expected in cases.items():
            assert run(memory, texts, query) == expected, query


def test_phrase_candidates_are_capped_and_flagged() -> None:
    texts = ["alpha beta " + "filler " * 3] * 30
    memory = memory_corpus([f"{t} number{n}" for n, t in enumerate(texts)])
    tree = analyse(parse('"alpha beta"'))
    assert tree is not None
    evaluator = Evaluator(memory, lambda ids, _p: ids, phrase_candidates=10)
    assert evaluator.evaluate(tree).ids.size == 10
    assert evaluator.approximate


def test_top_k_breaks_ties_by_id_and_hides_deleted_documents() -> None:
    matches = Matches(np.array([3, 5, 9, 12], dtype=np.int64), np.array([1.0, 2.0, 2.0, 0.5]))
    ranked, total = top_k(matches, frozenset(), 3)
    assert ranked == [(5, 2.0), (9, 2.0), (3, 1.0)]
    assert total == 4
    ranked, total = top_k(matches, frozenset({5}), 3)
    assert [i for i, _ in ranked] == [9, 3, 12]
    assert total == 3


# ------------------------------------------------------------------------------ PostgreSQL

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get(
    "DOCSEARCH_TEST_DSN", "postgresql://docsearch:docsearch_dev@127.0.0.1:5433/docsearch"
)


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


needs_db = pytest.mark.skipif(not _reachable(), reason="no PostgreSQL at DOCSEARCH_TEST_DSN")


@pytest.fixture
def index() -> Iterator[PgIndex]:
    pg = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    try:
        yield pg
    finally:
        pg.close()
        pg.drop_schema()


def titles(index: PgIndex, query: str, **kwargs: object) -> list[str]:
    return sorted(h.document.title for h in index.query(query, limit=100, **kwargs).hits)  # type: ignore[arg-type]


@pytest.fixture
def loaded(index: PgIndex) -> PgIndex:
    index.add_many(
        [NewDocument(f"d{n}", f"d{n}.txt", "text/plain", t) for n, t in enumerate(TEXTS, 1)]
    )
    return index


@needs_db
@pytest.mark.parametrize("compacted", [False, True])
def test_postgres_evaluates_the_same_as_the_oracle(loaded: PgIndex, compacted: bool) -> None:
    if compacted:
        loaded.compact()
    assert titles(loaded, "solar -wind") == ["d3", "d5"]
    assert titles(loaded, "(solar OR wind) -coal -nuclear") == ["d1", "d2", "d3"]
    assert titles(loaded, '"climate change"') == ["d6"]
    assert titles(loaded, 'policy -"climate change"') == ["d7"]
    assert titles(loaded, "-wind") == []
    assert titles(loaded, "solar wind") == ["d1", "d8"]  # plain words: same answer as `search`


@needs_db
def test_plain_queries_use_the_fast_path_and_operators_the_exact_one(loaded: PgIndex) -> None:
    assert loaded.query("solar wind").strategy.startswith(("scan", "and", "wand"))
    assert loaded.query("solar OR wind").strategy == "boolean"
    assert loaded.query('"climate change"').strategy == "boolean"
    any_mode = loaded.query("solar wind", mode="any", limit=100)
    assert len(any_mode.hits) == 5


@needs_db
def test_pagination_with_operators(loaded: PgIndex) -> None:
    everything = [h.document.id for h in loaded.query("solar OR wind", limit=100).hits]
    page1 = [h.document.id for h in loaded.query("solar OR wind", limit=2).hits]
    page2 = [h.document.id for h in loaded.query("solar OR wind", limit=2, offset=2).hits]
    assert page1 + page2 == everything[:4]
    assert loaded.query("solar OR wind", limit=2).total == 5


@needs_db
def test_filters_by_type_extension_language_and_date(index: PgIndex) -> None:
    index.add_many(
        [
            NewDocument(
                "report", "q3.pdf", "application/pdf", "annual glacier report", lang="latn"
            ),
            NewDocument("notes", "notes.txt", "text/plain", "glacier field notes", lang="latn"),
            NewDocument("हिंदी", "hi.txt", "text/plain", "glacier हिमालय अध्ययन", lang="deva"),
        ]
    )
    assert titles(index, "glacier type:pdf") == ["report"]
    assert titles(index, "glacier ext:txt") == ["notes", "हिंदी"]
    assert titles(index, "glacier ext:.txt lang:deva") == ["हिंदी"]
    assert titles(index, "glacier type:application") == ["report"]
    assert titles(index, "glacier after:2000-01-01") == ["notes", "report", "हिंदी"]
    assert titles(index, "glacier before:2000-01-01") == []
    assert titles(index, "glacier (field OR annual) type:pdf") == ["report"]
    assert titles(index, "type:pdf") == []  # a filter alone selects nothing: a term is required
    assert titles(index, "glacier type:100%") == []  # LIKE wildcards in a value are literal


@needs_db
def test_a_malformed_date_is_a_clear_error(loaded: PgIndex) -> None:
    with pytest.raises(InvalidFilterError, match="expects a date"):
        loaded.query("solar after:yesterday")


@needs_db
def test_deleted_documents_are_excluded_from_operator_queries(loaded: PgIndex) -> None:
    with loaded.connection() as conn:
        (victim,) = conn.execute("SELECT id FROM documents WHERE title = 'd5'").fetchone()  # type: ignore[misc]
    assert titles(loaded, "power -wind") == ["d4", "d5"]
    loaded.delete_documents([victim])
    assert titles(loaded, "solar OR nuclear") == ["d1", "d3", "d8"]
    assert titles(loaded, "power -wind") == ["d4"]


@needs_db
def test_snippets_highlight_positive_terms_only(loaded: PgIndex) -> None:
    hit = loaded.query("solar -wind", limit=1).hits[0]
    shown = [hit.snippet[s:e].lower() for s, e in hit.highlights]
    assert shown
    assert "wind" not in shown
