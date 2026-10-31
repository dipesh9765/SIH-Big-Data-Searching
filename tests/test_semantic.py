"""Semantic and image search: storage, jobs, ranking and HTTP, with a stand-in embedder; the real
models are exercised in the container tests at the bottom (skipped when fastembed is absent)."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from docsearch.config import ServiceSettings
from docsearch.engine.pg import NewDocument, PgIndex
from docsearch.engine.vectors import VectorStore, fuse, literal
from docsearch.ingest.embed import embed_asset
from docsearch.ingest.objects import MemoryStore
from docsearch.ingest.pipeline import delete_asset, submit
from docsearch.ingest.queue import JobQueue
from docsearch.ingest.worker import Worker
from docsearch.ocr import OcrResult
from docsearch.semantic import (
    IMAGE_DIM,
    TEXT_DIM,
    Embedder,
    HashingEmbedder,
    chunk_text,
)
from docsearch.service import create_service_app

# ----------------------------------------------------------------------- pure functions


def test_chunks_respect_size_sentences_and_limit() -> None:
    text = "First sentence here. " * 100
    chunks = chunk_text(text, size=300, limit=5)
    assert len(chunks) == 5
    assert all(len(c) <= 300 for c in chunks)
    assert all(c.endswith(".") for c in chunks[:-1])  # cut at sentence ends
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []
    assert chunk_text("short") == ["short"]


def test_chunks_break_hindi_text_at_the_danda() -> None:
    text = "यह पहला वाक्य है। " * 80
    chunks = chunk_text(text, size=200, limit=3)
    assert len(chunks) == 3
    assert all(c.endswith("।") for c in chunks)


def test_a_long_unbroken_run_is_still_split() -> None:
    chunks = chunk_text("x" * 5000, size=700, limit=16)
    assert chunks
    assert sum(len(c) for c in chunks) == 5000
    assert len(chunks) >= 5


def test_reciprocal_rank_fusion() -> None:
    fused = fuse([[1, 2, 3], [3, 1, 4]], k=60)
    assert [i for i, _ in fused] == [1, 3, 2, 4]  # 1 and 3 are in both lists; 1 ranks better in one
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert fuse([]) == []
    assert [i for i, _ in fuse([[5, 6], [6, 5]])] == [5, 6]  # exact tie: lower id first


def test_vector_literal_is_pgvector_text() -> None:
    assert literal(np.array([0.5, -1.0, 0.25], dtype=np.float32)) == "[0.5,-1,0.25]"


def test_the_test_embedder_is_normalised_and_keeps_shared_words_close() -> None:
    e = HashingEmbedder()
    v = e.embed_texts(["glacier melt", "glacier melting fast", "tax return form"])
    assert v.shape == (3, TEXT_DIM)
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    assert float(v[0] @ v[1]) > float(v[0] @ v[2])


# ------------------------------------------------------------------------------ database

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


@pytest.fixture
def vectors(index: PgIndex) -> VectorStore:
    store = VectorStore(index)
    if not store.available:
        pytest.skip("pgvector is not installed in this database")
    return store


def solid(colour: str) -> bytes:
    rgb = HashingEmbedder.COLOURS[colour]
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), rgb).save(buffer, format="PNG")
    return buffer.getvalue()


@needs_db
def test_nearest_documents_by_best_passage(index: PgIndex, vectors: VectorStore) -> None:
    e = HashingEmbedder()
    a = index.add(NewDocument("a", "a.txt", "text/plain", "glacier melt report")).document_id
    b = index.add(NewDocument("b", "b.txt", "text/plain", "income tax return")).document_id
    vectors.add_text(a, e.embed_texts(["glacier melt report", "unrelated filler words"]))
    vectors.add_text(b, e.embed_texts(["income tax return"]))
    found = vectors.search_text(e.embed_query("glacier melting"), 5)
    assert [d for d, _ in found] == [a, b]
    assert found[0][1] > found[1][1]
    assert vectors.counts() == {"documents": 2, "images": 0}
    vectors.add_text(a, e.embed_texts(["completely different text"]))  # replaces, not appends
    assert vectors.search_text(e.embed_query("glacier melting"), 1)[0][0] in (a, b)
    assert vectors.embedded_documents([a, b, 999]) == {a, b}


@needs_db
def test_deleted_documents_are_not_returned_by_similarity(
    index: PgIndex, vectors: VectorStore
) -> None:
    e = HashingEmbedder()
    keep = index.add(NewDocument("k", "k.txt", "text/plain", "wind turbine blades")).document_id
    gone = index.add(NewDocument("g", "g.txt", "text/plain", "wind turbine towers")).document_id
    for doc, text in ((keep, "wind turbine blades"), (gone, "wind turbine towers")):
        vectors.add_text(doc, e.embed_texts([text]))
    index.delete_documents([gone])
    assert [d for d, _ in vectors.search_text(e.embed_query("wind turbine"), 5)] == [keep]


@needs_db
def test_images_are_found_by_a_text_description(index: PgIndex, vectors: VectorStore) -> None:
    e = HashingEmbedder()
    store = MemoryStore()
    ids = {}
    for colour in ("red", "blue", "green"):
        sub = submit(index, store, name=f"{colour}.png", data=solid(colour))
        vectors.add_image(sub.asset_id, e.embed_images([solid(colour)])[0])
        ids[colour] = sub.asset_id
    found = vectors.search_images(e.embed_image_query("a red square"), 3)
    assert found[0][0] == ids["red"]
    assert found[0][1] > found[-1][1]
    assert vectors.has_image(ids["red"])
    assert not vectors.has_image(999)


# ----------------------------------------------------------------------- jobs and pipeline


def embedding_worker(index: PgIndex, store: MemoryStore, embedder: Embedder) -> Worker:
    return Worker(index, store, worker_id="emb", kinds=("embed",), batch_size=5, embedder=embedder)


@needs_db
def test_indexing_queues_an_embed_job_and_the_embed_worker_fills_the_vectors(
    index: PgIndex, vectors: VectorStore
) -> None:
    store = MemoryStore()
    e = HashingEmbedder()
    sub = submit(index, store, name="a.txt", data=b"glacier retreat in the himalaya. " * 10)
    worker = Worker(index, store, worker_id="t", enqueue_embeddings=True)
    assert worker.run_once().indexed == 1
    with index.connection() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM ingest_jobs ORDER BY id")]
    assert kinds == ["text", "embed"]
    assert vectors.counts()["documents"] == 0
    assert embedding_worker(index, store, e).run_once().indexed == 1
    assert vectors.counts()["documents"] == 1
    embed_asset(index, store, vectors, e, sub.asset_id)  # a re-run changes nothing
    assert vectors.counts()["documents"] == 1


@needs_db
def test_without_semantic_search_no_embed_jobs_are_created(index: PgIndex) -> None:
    store = MemoryStore()
    submit(index, store, name="a.txt", data=b"plain keyword indexing only")
    Worker(index, store, worker_id="t").run_once()
    assert JobQueue(index).counts()["queued"] == 0


@needs_db
def test_every_unit_of_a_recording_is_embedded(
    index: PgIndex, vectors: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docsearch.asr import Transcript
    from docsearch.extract import Unit

    store = MemoryStore()
    submit(index, store, name="talk.mp3", data=b"audio " * 40)
    monkeypatch.setattr(
        "docsearch.ingest.pipeline.transcribe",
        lambda *_a, **_k: Transcript(
            [Unit("melting glaciers", 0, 9000), Unit("volcanic ash clouds", 9000, 19000)],
            "en",
            0.9,
            19000,
        ),
    )
    Worker(
        index, store, worker_id="a", kinds=("asr",), batch_size=1, enqueue_embeddings=True
    ).run_once()
    assert embedding_worker(index, store, HashingEmbedder()).run_once().indexed == 1
    assert vectors.counts()["documents"] == 2


@needs_db
def test_a_picture_without_writing_is_kept_when_semantic_search_is_on(
    index: PgIndex, vectors: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "docsearch.ingest.pipeline.ocr_image", lambda *_a, **_k: OcrResult("", None, 1, 1)
    )
    store = MemoryStore()
    on = submit(index, store, name="photo.png", data=solid("red"))
    off = submit(index, store, name="photo2.png", data=solid("blue"))
    Worker(
        index, store, worker_id="o", kinds=("ocr",), batch_size=1, enqueue_embeddings=True
    ).run_once()
    with index.connection() as conn:
        status, doc, units = conn.execute(
            "SELECT status, document_id, units FROM assets WHERE id = %s", (on.asset_id,)
        ).fetchone()  # type: ignore[misc]
    assert (status, doc, units) == ("indexed", None, 0)
    assert embedding_worker(index, store, HashingEmbedder()).run_once().indexed == 1
    assert vectors.has_image(on.asset_id)
    # Without semantic search the same picture is still refused, as before.
    Worker(index, store, worker_id="o2", kinds=("ocr",), batch_size=1).run_once()
    with index.connection() as conn:
        row = conn.execute(
            "SELECT status, error FROM assets WHERE id = %s", (off.asset_id,)
        ).fetchone()
    assert row == ("failed", "the file contains no indexable text")


@needs_db
def test_a_failing_embed_job_never_fails_the_upload(index: PgIndex, vectors: VectorStore) -> None:
    class Broken(HashingEmbedder):
        def embed_texts(self, texts):  # type: ignore[no-untyped-def]
            raise RuntimeError("model crashed")

    store = MemoryStore()
    sub = submit(index, store, name="a.txt", data=b"text that is indexed fine")
    Worker(index, store, worker_id="t", enqueue_embeddings=True).run_once()
    with index.connection() as conn:
        conn.execute("UPDATE ingest_jobs SET max_attempts = 1 WHERE kind = 'embed'")
    result = embedding_worker(index, store, Broken()).run_once()
    assert result.failed == 1
    assert JobQueue(index).counts()["dead"] == 1
    with index.connection() as conn:
        status = conn.execute("SELECT status FROM assets WHERE id = %s", (sub.asset_id,)).fetchone()
    assert status == ("indexed",)  # still searchable by keyword
    assert [h.document.title for h in index.search("indexed").hits] == ["a.txt"]


@needs_db
def test_embedding_a_deleted_upload_is_a_no_op(index: PgIndex, vectors: VectorStore) -> None:
    store = MemoryStore()
    sub = submit(index, store, name="a.txt", data=b"doomed text content")
    Worker(index, store, worker_id="t", enqueue_embeddings=True).run_once()
    delete_asset(index, store, sub.asset_id)
    assert JobQueue(index).counts()["queued"] == 0  # deleting the upload removed its pending jobs
    # A job already claimed when the upload was deleted finds nothing to embed and ends cleanly.
    embed_asset(index, store, vectors, HashingEmbedder(), sub.asset_id)
    assert vectors.counts() == {"documents": 0, "images": 0}


# --------------------------------------------------------------------------------- HTTP


@pytest.fixture
def client(index: PgIndex) -> Iterator[tuple[TestClient, PgIndex, MemoryStore]]:
    store = MemoryStore()
    settings = ServiceSettings(semantic=True)
    app = create_service_app(settings, index=index, store=store, embedder=HashingEmbedder())
    with TestClient(app) as c:
        yield c, index, store


@needs_db
def test_semantic_and_hybrid_ranking_over_http(
    client: tuple[TestClient, PgIndex, MemoryStore],
) -> None:
    c, index, store = client
    for name, text in {
        "glaciers.txt": "himalayan glaciers are melting rapidly",
        "tax.txt": "how to file your income tax return",
        "wind.txt": "offshore wind farms and turbine maintenance",
    }.items():
        c.post("/v1/documents", files={"file": (name, text.encode())})
    worker = Worker(
        index, store, worker_id="w", enqueue_embeddings=True, embedder=HashingEmbedder()
    )
    worker.run_once()
    Worker(index, store, worker_id="e", kinds=("embed",), embedder=HashingEmbedder()).run_once()
    semantic = c.get("/v1/search", params={"q": "glacier melting", "ranking": "semantic"}).json()
    assert semantic["strategy"] == "semantic"
    assert semantic["results"][0]["document"]["title"] == "glaciers.txt"
    hybrid = c.get("/v1/search", params={"q": "glaciers", "ranking": "hybrid"}).json()
    assert hybrid["strategy"] == "hybrid"
    assert hybrid["results"][0]["document"]["title"] == "glaciers.txt"
    assert c.get("/v1/stats").status_code == 200


@needs_db
def test_semantic_search_is_refused_when_not_enabled(index: PgIndex) -> None:
    app = create_service_app(ServiceSettings(), index=index, store=MemoryStore())
    with TestClient(app) as c:
        assert c.get("/v1/search", params={"q": "x", "ranking": "semantic"}).status_code == 501
        assert c.get("/v1/search/images", params={"q": "x"}).status_code == 501
        assert c.get("/v1/search", params={"q": "x", "ranking": "bogus"}).status_code == 422


@needs_db
def test_image_search_over_http(client: tuple[TestClient, PgIndex, MemoryStore]) -> None:
    c, index, store = client
    vs = VectorStore(index)
    e = HashingEmbedder()
    assets = {}
    for colour in ("red", "blue"):
        sub = submit(index, store, name=f"{colour}.png", data=solid(colour), title=f"{colour} card")
        vs.add_image(sub.asset_id, e.embed_images([solid(colour)])[0])
        assets[colour] = sub.asset_id
    found = c.get("/v1/search/images", params={"q": "blue"}).json()
    assert found[0]["asset_id"] == assets["blue"]
    assert found[0]["title"] == "blue card"
    assert found[0]["file_url"] == f"/v1/assets/{assets['blue']}/file"
    assert len(found) == 2
    assert c.get(found[0]["file_url"]).headers["content-type"] == "image/png"
    assert IMAGE_DIM == 512
