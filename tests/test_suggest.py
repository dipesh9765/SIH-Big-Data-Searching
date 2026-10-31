"""Autocomplete and typo correction over the PostgreSQL term dictionary."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from docsearch.analysis import term_frequencies_with_surfaces
from docsearch.config import ServiceSettings
from docsearch.engine.pg import NewDocument, PgIndex
from docsearch.ingest.objects import MemoryStore
from docsearch.service import create_service_app

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

TEXTS = [
    "glacier retreat accelerates in the himalayan glaciers",
    "glacier monitoring stations record glacier mass balance",
    "operations research for operating power plants and operation schedules",
    "search engines index searches and searching behaviour",
    "volcano monitoring after the volcanic eruption",
    "भारत की राजधानी नई दिल्ली है और हिमालय के ग्लेशियर",
]


@pytest.fixture
def index() -> Iterator[PgIndex]:
    pg = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    pg.add_many([NewDocument(f"d{n}", f"{n}.txt", "text/plain", t) for n, t in enumerate(TEXTS)])
    pg.compact()
    try:
        yield pg
    finally:
        pg.close()
        pg.drop_schema()


def test_surface_forms_are_the_words_as_written() -> None:
    counts, _, surfaces = term_frequencies_with_surfaces("Operations and operating operation")
    assert counts["oper"] == 3
    assert surfaces["oper"] == "operations"  # the first written form, lower-cased


def test_completion_shows_words_not_stems_and_ranks_by_frequency(index: PgIndex) -> None:
    assert index.complete("glac")[0] == "glacier"
    assert index.complete("operat")[0] in {"operations", "operating", "operation"}
    assert "volcano" in index.complete("volc")
    assert index.complete("GLAC")[0] == "glacier"  # case-insensitive
    assert index.complete("zzz") == []
    assert index.complete("") == []
    assert index.complete("gl", limit=1) == ["glacier"]
    assert len(set(index.complete("s", limit=10))) == len(
        index.complete("s", limit=10)
    )  # no duplicates


def test_completion_works_in_devanagari(index: PgIndex) -> None:
    assert any(w.startswith("ग्लेश") for w in index.complete("ग्ले"))


def test_wildcard_characters_in_a_prefix_are_literal(index: PgIndex) -> None:
    assert index.complete("%") == []
    assert index.complete("_") == []


def test_typos_are_corrected_within_the_budget(index: PgIndex) -> None:
    assert index.correct("glaicer")[0] == "glacier"  # transposition
    assert index.correct("volcanoo")[0] == "volcano"  # extra letter
    assert index.correct("serch")[0].startswith("search")
    assert index.correct("glacier") == []  # already a word
    assert index.correct("xyz") == []  # too short to correct safely
    assert index.correct("qqqqqqqq") == []  # nothing close
    assert index.correct("two words") == []  # one word at a time


def test_did_you_mean_rewrites_only_the_unknown_words(index: PgIndex) -> None:
    assert index.did_you_mean("glaicer retreat") == "glacier retreat"
    assert index.did_you_mean("glacier retreat") is None
    assert index.did_you_mean("zzzzzzz") is None


@pytest.fixture
def client(index: PgIndex) -> Iterator[TestClient]:
    with TestClient(create_service_app(ServiceSettings(), index=index, store=MemoryStore())) as c:
        yield c


def test_suggest_endpoint_completes_the_last_word_typed(client: TestClient) -> None:
    assert client.get("/v1/suggest", params={"q": "glacier mon"}).json() == ["monitoring"]
    assert client.get("/v1/suggest", params={"q": "volc", "limit": 5}).json()
    assert client.get("/v1/suggest", params={"q": ""}).status_code == 422
    assert client.get("/v1/suggest", params={"q": "a", "limit": 99}).status_code == 422


def test_search_offers_a_correction_only_when_nothing_matched(client: TestClient) -> None:
    miss = client.get("/v1/search", params={"q": "glaicer"}).json()
    assert miss["results"] == []
    assert miss["did_you_mean"] == "glacier"
    hit = client.get("/v1/search", params={"q": "glacier"}).json()
    assert hit["results"]
    assert hit["did_you_mean"] is None
