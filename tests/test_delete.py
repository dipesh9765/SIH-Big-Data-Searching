"""Deletion: tombstones, purge, asset deletion and API-key protection."""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docsearch.config import ServiceSettings
from docsearch.engine.pg import NewDocument, PgIndex
from docsearch.engine.search import Mode, Pruning
from docsearch.index import Index
from docsearch.ingest.objects import MemoryStore
from docsearch.ingest.pipeline import delete_asset, submit
from docsearch.ingest.worker import Worker
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


@pytest.fixture
def index() -> Iterator[PgIndex]:
    pg = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    try:
        yield pg
    finally:
        pg.close()
        pg.drop_schema()


def doc(position: int, text: str) -> NewDocument:
    return NewDocument(str(position), f"{position}.txt", "text/plain", text)


def corpus(seed: int, documents: int) -> list[str]:
    rng = random.Random(seed)
    words = [f"w{n}x" for n in range(200)]
    cumulative = [sum(1.0 / (r + 1) for r in range(i + 1)) for i in range(200)]
    return [
        " ".join(rng.choices(words, cum_weights=cumulative, k=rng.randint(4, 40)))
        for _ in range(documents)
    ]


def titles(index: PgIndex, query: str, **kwargs: object) -> list[str]:
    return [h.document.title for h in index.search(query, limit=500, **kwargs).hits]  # type: ignore[arg-type]


# --------------------------------------------------------------------------- tombstones


@pytest.mark.parametrize("pruning", ["never", "always"])
@pytest.mark.parametrize("compact_first", [False, True])
def test_a_deleted_document_never_appears_again(
    index: PgIndex, pruning: Pruning, compact_first: bool
) -> None:
    index.add_many([doc(n, f"solar power report {n} with extra{n % 3} words") for n in range(30)])
    if compact_first:
        index.compact()
    victim = index.search("report 7", mode="all").hits[0].document.id
    assert index.delete_documents([victim]) == 1
    for mode in ("all", "any"):
        got = index.search("solar", mode=mode, limit=100, pruning=pruning).hits
        assert victim not in [h.document.id for h in got]
        assert len(got) == 29
    assert index.get_document(victim) is None
    assert victim not in [d.id for d in index.list_documents(limit=100)]
    assert index.stats().documents == 29


def test_deleting_twice_or_a_stranger_changes_nothing(index: PgIndex) -> None:
    index.add_many([doc(0, "alpha beta"), doc(1, "gamma delta")])
    first = index.search("alpha").hits[0].document.id
    assert index.delete_documents([first, 999_999]) == 1
    assert index.delete_documents([first]) == 0
    assert index.delete_documents([]) == 0
    assert index.stats().documents == 1


def test_the_same_text_can_be_indexed_again_after_deletion(index: PgIndex) -> None:
    added = index.add(doc(0, "glacier retreat monitoring"))
    assert index.delete_documents([added.document_id]) == 1
    assert index.search("glacier").hits == []
    again = index.add(doc(1, "glacier retreat monitoring"))
    assert not again.duplicate
    assert again.document_id != added.document_id
    assert [h.document.id for h in index.search("glacier").hits] == [again.document_id]


def test_deleted_documents_stay_out_of_boolean_scans_after_more_ingest(index: PgIndex) -> None:
    index.add_many([doc(n, f"tidal energy farm {n}") for n in range(10)])
    victim = index.search("farm 3").hits[0].document.id
    index.delete_documents([victim])
    index.add_many([doc(100 + n, f"tidal energy newer {n}") for n in range(5)])
    index.compact()
    assert victim not in [h.document.id for h in index.search("tidal", limit=100).hits]
    assert len(index.search("tidal", limit=100).hits) == 14


# --------------------------------------------------------------------------------- purge


def test_purge_removes_postings_and_text_but_keeps_the_tombstone(index: PgIndex) -> None:
    index.add_many([doc(n, f"volcano ash cloud number {n}") for n in range(20)])
    index.compact()
    before = index.stats().block_postings
    ids = [h.document.id for h in index.search("number 3 4 5", mode="any", limit=3).hits]
    index.delete_documents(ids)
    result = index.purge_deleted()
    assert (result.documents, result.skipped) == (3, False)
    assert result.postings > 0
    assert index.stats().block_postings == before - result.postings
    with index.connection() as conn:
        content, purged = conn.execute(
            "SELECT content, purged_at FROM documents WHERE id = %s", (ids[0],)
        ).fetchone()  # type: ignore[misc]
        (df,) = conn.execute("SELECT doc_freq FROM terms WHERE term = 'volcano'").fetchone()  # type: ignore[misc]
    assert content == ""
    assert purged is not None
    assert df == 17
    assert not set(ids) & {h.document.id for h in index.search("volcano", limit=100).hits}
    assert index.purge_deleted().documents == 0  # nothing left to do


def test_purge_also_clears_uncompacted_postings(index: PgIndex) -> None:
    added = index.add(doc(0, "tsunami warning buoy"))
    index.add(doc(1, "tsunami drill schedule"))
    index.delete_documents([added.document_id])
    index.purge_deleted()
    assert index.stats().delta_postings == 3  # only the surviving document's terms remain
    assert [h.document.title for h in index.search("tsunami").hits] == ["1"]


@pytest.mark.parametrize("mode", ["any", "all"])
def test_after_purge_rankings_equal_an_index_built_without_the_deleted_documents(
    index: PgIndex, tmp_path: Path, mode: Mode
) -> None:
    texts = corpus(11, 400)
    index.add_many([doc(n, t) for n, t in enumerate(texts)])
    index.compact()
    rng = random.Random(3)
    doomed = set(rng.sample(range(400), 90))
    with index.connection() as conn:
        by_title: dict[str, int] = dict(conn.execute("SELECT title, id FROM documents"))
    index.delete_documents([by_title[str(n)] for n in doomed])
    index.purge_deleted()
    index.compact()
    reference = Index(tmp_path / "ref.db")
    for n, text in enumerate(texts):
        if n not in doomed:
            reference.add(title=str(n), source_name="t.txt", media_type="text/plain", text=text)
    for query in ("w0x", "w1x w2x", "w5x w40x", "w100x", "w3x w9x w27x"):
        expected = reference.search(query, mode=mode, limit=25)
        got = index.search(query, mode=mode, limit=25)
        assert [h.document.title for h in got.hits] == [h.document.title for h in expected.hits]
        for mine, theirs in zip(got.hits, expected.hits, strict=True):
            assert mine.score == pytest.approx(theirs.score, rel=1e-9), query
    reference.close()


# ----------------------------------------------------------------------------- assets


def test_deleting_an_upload_removes_text_row_jobs_and_the_original(index: PgIndex) -> None:
    store = MemoryStore()
    sub = submit(index, store, name="a.txt", data=b"rainfall statistics for the district")
    Worker(index, store, worker_id="w").run_once()
    assert len(index.search("rainfall").hits) == 1
    assert len(list(store.list_objects("assets/"))) == 1
    assert delete_asset(index, store, sub.asset_id)
    assert index.search("rainfall").hits == []
    assert list(store.list_objects("assets/")) == []
    with index.connection() as conn:
        assert conn.execute("SELECT count(*) FROM assets").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ingest_jobs").fetchone() == (0,)
    assert not delete_asset(index, store, sub.asset_id)


def test_text_shared_by_two_uploads_survives_until_the_last_one_is_deleted(
    index: PgIndex,
) -> None:
    store = MemoryStore()
    one = submit(index, store, name="a.txt", data=b"identical words about drought")
    two = submit(index, store, name="b.txt", data=b"\xef\xbb\xbfidentical words about drought")
    Worker(index, store, worker_id="w", batch_size=5).run_once()
    assert index.stats().documents == 1
    assert delete_asset(index, store, one.asset_id)
    assert len(index.search("drought").hits) == 1  # still owned by the second upload
    assert delete_asset(index, store, two.asset_id)
    assert index.search("drought").hits == []


def test_deleting_a_recording_removes_all_of_its_transcript_units(
    index: PgIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docsearch.asr import Transcript
    from docsearch.extract import Unit

    store = MemoryStore()
    sub = submit(index, store, name="talk.mp3", data=b"audio bytes " * 30)
    monkeypatch.setattr(
        "docsearch.ingest.pipeline.transcribe",
        lambda *_a, **_k: Transcript(
            [
                Unit("first part about glaciers", 0, 9000),
                Unit("second part about glaciers", 9000, 20000),
            ],
            "en",
            0.9,
            20000,
        ),
    )
    Worker(index, store, worker_id="w", kinds=("asr",), batch_size=1).run_once()
    assert len(index.search("glaciers").hits) == 2
    assert delete_asset(index, store, sub.asset_id)
    assert index.search("glaciers").hits == []
    assert index.stats().documents == 0


def test_text_indexed_for_an_upload_deleted_mid_processing_is_taken_back(
    index: PgIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    sub = submit(index, store, name="a.txt", data=b"landslide susceptibility mapping")
    original = index.add_many_skipping_empty

    def delete_while_working(docs):  # type: ignore[no-untyped-def]
        result = original(docs)
        with index.connection() as conn:  # the user deletes the upload right now
            conn.execute("DELETE FROM assets WHERE id = %s", (sub.asset_id,))
        return result

    monkeypatch.setattr(index, "add_many_skipping_empty", delete_while_working)
    outcome = Worker(index, store, worker_id="w").run_once()
    assert outcome.lost == 1
    assert index.search("landslide").hits == []  # no searchable text without an owner


# ----------------------------------------------------------------------- HTTP and API key


@pytest.fixture
def secured() -> Iterator[tuple[TestClient, PgIndex, MemoryStore]]:
    pg = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    store = MemoryStore()
    settings = ServiceSettings(api_key="s3cret-key")
    with TestClient(create_service_app(settings, index=pg, store=store)) as client:
        yield client, pg, store
    pg.close()
    pg.drop_schema()


def test_the_api_key_protects_v1_but_not_probes(
    secured: tuple[TestClient, PgIndex, MemoryStore],
) -> None:
    client, _, _ = secured
    assert client.get("/v1/stats").status_code == 401
    assert client.get("/v1/stats").headers["www-authenticate"] == "Bearer"
    assert client.get("/v1/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.get("/v1/stats", headers={"Authorization": "Bearer s3cret-key"}).status_code == 200
    )
    assert client.get("/v1/stats", headers={"X-API-Key": "s3cret-key"}).status_code == 200
    assert client.post("/v1/documents", files={"file": ("a.txt", b"x")}).status_code == 401
    assert client.delete("/v1/assets/1").status_code == 401
    for open_path in ("/healthz", "/readyz", "/metrics"):
        assert client.get(open_path).status_code == 200


def test_delete_endpoints(secured: tuple[TestClient, PgIndex, MemoryStore]) -> None:
    client, pg, store = secured
    auth = {"Authorization": "Bearer s3cret-key"}
    asset = client.post(
        "/v1/documents", files={"file": ("a.txt", b"hydrology survey of rivers")}, headers=auth
    ).json()["asset_id"]
    Worker(pg, store, worker_id="w").run_once()
    document = client.get(f"/v1/assets/{asset}", headers=auth).json()["document_id"]
    assert client.delete(f"/v1/documents/{document}", headers=auth).status_code == 204
    assert client.delete(f"/v1/documents/{document}", headers=auth).status_code == 404
    assert client.get("/v1/search?q=hydrology", headers=auth).json()["results"] == []
    assert client.delete(f"/v1/assets/{asset}", headers=auth).status_code == 204
    assert client.delete(f"/v1/assets/{asset}", headers=auth).status_code == 404
    assert client.get(f"/v1/assets/{asset}/file", headers=auth).status_code == 404
