"""PostgreSQL index vs the SQLite reference, in every storage state.

Needs a Postgres reachable at DOCSEARCH_TEST_DSN (default: the docker compose service on
127.0.0.1:5433). Without one the module is skipped, so `pytest` still works on a bare machine; CI
provides the service. Every test gets its own schema, dropped afterwards.
"""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from docsearch.analysis import query_terms
from docsearch.engine.pg import NewDocument, PgIndex
from docsearch.engine.search import Mode
from docsearch.index import EmptyDocumentError, Index

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


pytestmark = pytest.mark.skipif(not _reachable(), reason="no PostgreSQL at DOCSEARCH_TEST_DSN")


@pytest.fixture
def pg() -> Iterator[PgIndex]:
    index = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    try:
        yield index
    finally:
        index.close()
        index.drop_schema()


def corpus(seed: int, documents: int, vocabulary: int = 300) -> list[str]:
    rng = random.Random(seed)
    words = [f"w{n}x" for n in range(vocabulary)]
    weights = [1.0 / (rank + 1) for rank in range(vocabulary)]
    return [" ".join(rng.choices(words, weights, k=rng.randint(3, 60))) for _ in range(documents)]


def doc(position: int, text: str) -> NewDocument:
    return NewDocument(str(position), f"{position}.txt", "text/plain", text)


def load_reference(tmp_path: Path, texts: list[str], start: int = 0) -> Index:
    reference = Index(tmp_path / "ref.db")
    for position, text in enumerate(texts, start=start):
        reference.add(
            title=str(position), source_name=f"{position}.txt", media_type="text/plain", text=text
        )
    return reference


def assert_same(pg: PgIndex, reference: Index, queries: list[str], mode: Mode) -> None:
    for query in queries:
        for k in (1, 10, 40):
            expected = reference.search(query, mode=mode, limit=k)
            got = pg.search(query, mode=mode, limit=k)
            assert [h.document.title for h in got.hits] == [
                h.document.title for h in expected.hits
            ], (query, mode, k, got.strategy)
            for mine, theirs in zip(got.hits, expected.hits, strict=True):
                assert mine.score == pytest.approx(theirs.score, rel=1e-9), query


def random_queries(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    words = [f"w{n}x" for n in range(300)]
    weights = [1.0 / (rank + 1) for rank in range(300)]
    return [" ".join(rng.choices(words, weights, k=rng.randint(1, 4))) for _ in range(count)]


@pytest.mark.parametrize("mode", ["any", "all"])
def test_uncompacted_index_matches_reference(pg: PgIndex, tmp_path: Path, mode: Mode) -> None:
    texts = corpus(1, 600)
    pg.add_many([doc(i, t) for i, t in enumerate(texts)])
    assert pg.stats().block_postings == 0
    assert pg.stats().delta_postings > 0
    assert_same(pg, load_reference(tmp_path, texts), random_queries(10, 25), mode)


@pytest.mark.parametrize("mode", ["any", "all"])
def test_compacted_index_matches_reference(pg: PgIndex, tmp_path: Path, mode: Mode) -> None:
    texts = corpus(2, 600)
    pg.add_many([doc(i, t) for i, t in enumerate(texts)])
    result = pg.compact(term_batch=50)  # several chunks
    assert result.postings > 0
    assert not result.skipped
    stats = pg.stats()
    assert stats.delta_postings == 0
    assert stats.block_postings == result.postings
    assert_same(pg, load_reference(tmp_path, texts), random_queries(11, 25), mode)


@pytest.mark.parametrize("mode", ["any", "all"])
def test_blocks_plus_delta_match_reference_through_repeated_compaction(
    pg: PgIndex, tmp_path: Path, mode: Mode
) -> None:
    texts = corpus(3, 900)
    queries = random_queries(12, 20)
    for start in range(0, 900, 300):
        pg.add_many([doc(start + i, t) for i, t in enumerate(texts[start : start + 300])])
        assert_same(pg, load_reference(tmp_path / str(start), texts[: start + 300]), queries, mode)
        pg.compact()  # tops up partial tail blocks and appends new ones
        assert_same(pg, load_reference(tmp_path / f"c{start}", texts[: start + 300]), queries, mode)


def test_compaction_is_idempotent_and_search_is_unchanged_by_it(pg: PgIndex) -> None:
    pg.add_many([doc(i, t) for i, t in enumerate(corpus(4, 300))])
    before = pg.search("w0x w1x", mode="any", limit=20)
    first = pg.compact()
    second = pg.compact()
    after = pg.search("w0x w1x", mode="any", limit=20)
    assert first.postings > 0
    assert second.postings == 0
    assert [h.document.id for h in before.hits] == [h.document.id for h in after.hits]


@pytest.mark.parametrize("pruning", ["always", "never"])
def test_both_strategies_agree_on_postgres(pg: PgIndex, pruning: str) -> None:
    pg.add_many([doc(i, t) for i, t in enumerate(corpus(5, 700))])
    pg.compact()
    reference = pg.search("w0x w200x", mode="any", limit=15, pruning="never")
    got = pg.search("w0x w200x", mode="any", limit=15, pruning=pruning)  # type: ignore[arg-type]
    assert [h.document.id for h in got.hits] == [h.document.id for h in reference.hits]


def test_duplicates_are_stored_once_and_reported(pg: PgIndex) -> None:
    first = pg.add(doc(0, "alpha beta gamma"))
    again = pg.add(doc(1, "alpha beta gamma"))
    assert not first.duplicate
    assert again.duplicate
    assert again.document_id == first.document_id
    batch = pg.add_many([doc(2, "delta epsilon"), doc(3, "delta epsilon"), doc(4, "zeta eta")])
    assert [a.duplicate for a in batch] == [False, True, False]
    assert batch[0].document_id == batch[1].document_id
    assert pg.stats().documents == 3


def test_empty_text_is_rejected_before_touching_the_database(pg: PgIndex) -> None:
    with pytest.raises(EmptyDocumentError):
        pg.add(doc(0, "the of and"))
    assert pg.stats().documents == 0


def test_search_on_an_empty_index_and_with_no_usable_terms(pg: PgIndex) -> None:
    assert pg.search("anything").hits == []
    pg.add(doc(0, "hello world"))
    assert pg.search("the of").hits == []
    assert pg.search("nonexistent").hits == []


def test_snippets_and_hindi_round_trip(pg: PgIndex) -> None:
    pg.add(doc(0, "भारत की राजधानी नई दिल्ली है। वहाँ बहुत सी किताबें मिलती हैं।"))
    pg.compact()
    hit = pg.search("किताब").hits[0]
    assert [hit.snippet[s:e] for s, e in hit.highlights] == ["किताबें"]


def test_exact_total_and_pruned_total(pg: PgIndex) -> None:
    pg.add_many([doc(i, t) for i, t in enumerate(corpus(6, 400))])
    pg.compact()
    exact = pg.search("w0x w1x", mode="any", exact_total=True)
    assert exact.total is not None
    assert exact.total > 10
    assert pg.search("w0x w1x", mode="any", pruning="always").total is None


def test_schema_names_are_validated() -> None:
    with pytest.raises(ValueError, match="invalid schema"):
        PgIndex(DSN, schema='x"; drop table y; --')


def test_query_terms_helper_agrees_with_what_was_indexed(pg: PgIndex) -> None:
    pg.add(doc(0, "running dogs"))
    assert query_terms("runs dog") == ["run", "dog"]
    assert pg.search("runs dog").hits[0].document.title == "0"


def test_a_running_compactor_blocks_only_its_own_schema(pg: PgIndex) -> None:
    other = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    try:
        for index in (pg, other):
            index.add(doc(0, "alpha beta gamma"))
        with psycopg.connect(DSN, autocommit=True) as holder:
            holder.execute("SELECT pg_advisory_lock(2, hashtext(%s))", (pg._schema,))
            assert pg.compact().skipped
            assert not other.compact().skipped
            holder.execute("SELECT pg_advisory_unlock(2, hashtext(%s))", (pg._schema,))
        assert not pg.compact().skipped
    finally:
        other.close()
        other.drop_schema()
