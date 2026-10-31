"""The scale-out HTTP service end to end, with a real PostgreSQL and an in-memory object store."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from docsearch.config import ServiceSettings
from docsearch.engine.pg import PgIndex
from docsearch.ingest.objects import MemoryStore
from docsearch.ingest.worker import Worker
from docsearch.service import create_service_app
from tests.conftest import make_docx, make_pdf

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


class Rig:
    def __init__(self, client: TestClient, index: PgIndex, store: MemoryStore) -> None:
        self.client = client
        self.index = index
        self.store = store
        self.worker = Worker(index, store, worker_id="test-worker")

    def upload(self, name: str, data: bytes, **params: str):  # type: ignore[no-untyped-def]
        return self.client.post(
            "/v1/documents", files={"file": (name, data, "application/octet-stream")}, params=params
        )

    def drain(self) -> None:
        for _ in range(100):  # bounded: a job nobody can take must fail the test, not hang it
            self.worker.run_once()
            counts = self.worker.queue.counts()
            if not counts["queued"] and not counts["running"]:
                return
        raise AssertionError(f"queue did not drain: {self.worker.queue.counts()}")


@pytest.fixture
def rig() -> Iterator[Rig]:
    index = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    store = MemoryStore()
    settings = ServiceSettings(
        max_upload_bytes=64 * 1024, max_media_bytes=256 * 1024, max_results=5
    )
    with TestClient(create_service_app(settings, index=index, store=store)) as client:
        yield Rig(client, index, store)
    index.close()
    index.drop_schema()


def test_upload_returns_202_then_the_worker_makes_it_searchable(rig: Rig) -> None:
    response = rig.upload("glaciers.txt", b"glacier retreat is accelerating", title="Glaciers")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["duplicate"] is False
    asset_id = body["asset_id"]
    assert rig.client.get("/v1/search?q=glacier").json()["results"] == []  # not indexed yet
    rig.drain()
    asset = rig.client.get(f"/v1/assets/{asset_id}").json()
    assert asset["status"] == "indexed"
    assert asset["error"] is None
    assert asset["title"] == "Glaciers"
    found = rig.client.get("/v1/search?q=glacier").json()
    assert [r["document"]["title"] for r in found["results"]] == ["Glaciers"]
    assert found["results"][0]["document"]["id"] == asset["document_id"]
    hit = found["results"][0]
    assert [hit["snippet"][s:e] for s, e in hit["highlights"]] == ["glacier"]
    assert rig.client.get(f"/v1/documents/{asset['document_id']}").json()["title"] == "Glaciers"


def test_the_same_bytes_again_is_200_and_not_a_new_job(rig: Rig) -> None:
    first = rig.upload("a.txt", b"monsoon rainfall data").json()
    again = rig.upload("copy.txt", b"monsoon rainfall data")
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["asset_id"] == first["asset_id"]
    assert again.json()["job_id"] is None


def test_a_failed_file_reports_why_and_can_be_retried_by_uploading_again(rig: Rig) -> None:
    sub = rig.upload("scan.pdf", b"%PDF-1.4 truncated nonsense").json()
    rig.drain()
    asset = rig.client.get(f"/v1/assets/{sub['asset_id']}").json()
    assert asset["status"] == "failed"
    assert asset["error"]
    failed = rig.client.get("/v1/assets?status=failed").json()
    assert [a["asset_id"] for a in failed] == [sub["asset_id"]]
    retry = rig.upload("scan.pdf", b"%PDF-1.4 truncated nonsense")
    assert retry.status_code == 202
    assert retry.json()["status"] == "queued"


def test_all_supported_formats_flow_through(rig: Rig) -> None:
    rig.upload("a.txt", b"volcano")
    rig.upload("b.docx", make_docx("tsunami"))
    rig.upload("c.pdf", make_pdf("earthquake"))
    rig.upload("d.csv", b"k,v\nflood,1\n")
    rig.drain()
    for word in ("volcano", "tsunami", "earthquake", "flood"):
        assert len(rig.client.get(f"/v1/search?q={word}").json()["results"]) == 1, word
    stats = rig.client.get("/v1/stats").json()
    assert stats["documents"] == 4
    assert stats["queue"]["done"] == 4
    assert ".pdf" in stats["accepted_extensions"]


def test_search_pagination_modes_and_limits(rig: Rig) -> None:
    for n in range(8):
        rig.upload(f"{n}.txt", f"wind turbine blade design number {n} wind".encode())
    rig.drain()
    page1 = rig.client.get("/v1/search?q=wind&limit=3").json()
    page2 = rig.client.get("/v1/search?q=wind&limit=3&offset=3").json()
    ids = [r["document"]["id"] for r in page1["results"] + page2["results"]]
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert len(rig.client.get("/v1/search?q=wind&limit=100").json()["results"]) == 5  # max_results
    any_mode = rig.client.get("/v1/search?q=wind+nonexistentword&mode=any").json()
    assert len(any_mode["results"]) == 5
    all_mode = rig.client.get("/v1/search?q=wind+nonexistentword&mode=all").json()
    assert all_mode["results"] == []
    exact = rig.client.get("/v1/search?q=wind&exact_total=true").json()
    assert exact["total"] == 8


def test_documents_listing_is_newest_first(rig: Rig) -> None:
    for n in range(3):
        rig.upload(f"{n}.txt", f"document body {n} unique{n}".encode())
    rig.drain()
    listed = rig.client.get("/v1/documents?limit=2").json()
    assert len(listed) == 2
    assert listed[0]["id"] > listed[1]["id"]


def test_input_validation_and_error_codes(rig: Rig) -> None:
    assert rig.upload("x.exe", b"MZ").status_code == 415
    assert rig.upload("empty.txt", b"").status_code == 422
    assert rig.upload("big.txt", b"a" * (64 * 1024 + 1)).status_code == 413
    assert rig.client.get("/v1/assets/999999").status_code == 404
    assert rig.client.get("/v1/documents/999999").status_code == 404
    assert rig.client.get("/v1/search").status_code == 422
    assert rig.client.get("/v1/search?q=" + "x" * 501).status_code == 422
    assert rig.client.get("/v1/search?q=a&offset=10001").status_code == 422
    assert rig.client.get("/v1/assets?status=bogus").status_code == 422
    assert rig.client.get("/v1/assets?limit=0").status_code == 422


def test_declared_oversize_bodies_are_refused_before_reading(rig: Rig) -> None:
    response = rig.client.post(
        "/v1/documents", content=b"x", headers={"content-length": str(10 * 2**20)}
    )
    assert response.status_code == 413


def test_security_headers_and_request_id(rig: Rig) -> None:
    response = rig.client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-request-id"]


def test_readiness_reports_dependency_failures(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    assert rig.client.get("/readyz").json()["status"] == "ready"

    def down() -> None:
        raise ConnectionError("object store unreachable")

    monkeypatch.setattr(rig.store, "ping", down)
    assert rig.client.get("/readyz").status_code == 503


def test_storage_outage_during_upload_is_a_503_not_a_crash(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise ConnectionError("object store unreachable")

    monkeypatch.setattr(rig.store, "put", refuse)
    response = rig.upload("a.txt", b"hello world")
    assert response.status_code == 503
    assert "object store" not in response.text  # internals are not leaked to clients


# ------------------------------------------------------------------ media upload and file serving


@pytest.mark.parametrize(
    ("header", "total", "expected"),
    [
        (None, 100, (0, 99)),
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=10-", 100, (10, 99)),
        ("bytes=-5", 100, (95, 99)),
        ("bytes=90-500", 100, (90, 99)),  # end clamped to the file
        ("bytes=100-", 100, (None, 0)),  # starts past the end
        ("bytes=5-2", 100, (None, 0)),
        ("bytes=-0", 100, (None, 0)),
        ("bytes=0-1,5-6", 100, (None, 0)),  # multiple ranges unsupported
        ("items=0-1", 100, (None, 0)),
        ("bytes=a-b", 100, (None, 0)),
        ("bytes=0-1", 0, (None, 0)),
    ],
)
def test_range_header_parsing(
    header: str | None, total: int, expected: tuple[int | None, int]
) -> None:
    from docsearch.service import _parse_range

    assert _parse_range(header, total) == expected


def test_audio_and_video_are_accepted_and_queued_for_speech_recognition(rig: Rig) -> None:
    for name in ("talk.mp3", "clip.mp4", "memo.wav"):
        response = rig.upload(name, name.encode() * 50)
        assert response.status_code == 202, name
    with rig.index.connection() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM ingest_jobs ORDER BY id")]
    assert kinds == ["asr", "asr", "asr"]
    accepted = rig.client.get("/v1/stats").json()["accepted_extensions"]
    assert {".mp3", ".mp4", ".mkv", ".flac"} <= set(accepted)


def test_media_may_be_larger_than_documents_but_not_larger_than_the_media_limit(rig: Rig) -> None:
    assert rig.upload("big.mp3", b"a" * (200 * 1024)).status_code == 202  # > 64 KB document limit
    assert rig.upload("huge.mp3", b"b" * (300 * 1024)).status_code == 413  # > 256 KB media limit
    assert rig.upload("doc.txt", b"c" * (70 * 1024)).status_code == 413  # > 64 KB document limit


def test_the_original_file_is_served_with_range_support(rig: Rig) -> None:
    payload = bytes(range(256)) * 40  # 10,240 bytes
    asset = rig.upload("clip.mp3", payload).json()["asset_id"]
    full = rig.client.get(f"/v1/assets/{asset}/file")
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"] == "audio/mpeg"
    assert full.headers["content-disposition"].startswith('inline; filename="clip.mp3"')
    part = rig.client.get(f"/v1/assets/{asset}/file", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == payload[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(payload)}"
    tail = rig.client.get(f"/v1/assets/{asset}/file", headers={"Range": "bytes=-16"})
    assert tail.content == payload[-16:]
    assert (
        rig.client.get(f"/v1/assets/{asset}/file", headers={"Range": "bytes=999999-"}).status_code
        == 416
    )
    assert rig.client.get("/v1/assets/999999/file").status_code == 404


def test_a_missing_object_behind_an_asset_is_a_404_not_a_500(rig: Rig) -> None:
    asset = rig.upload("a.mp3", b"audio bytes " * 20).json()["asset_id"]
    rig.store.delete(next(iter(rig.store._objects)))
    assert rig.client.get(f"/v1/assets/{asset}/file").status_code == 404


def test_streaming_upload_stops_reading_at_the_limit(rig: Rig) -> None:
    import io

    from docsearch.ingest.pipeline import TooLargeError, submit_stream

    class Counting(io.BytesIO):
        reads = 0

        def read(self, size: int | None = -1) -> bytes:
            Counting.reads += 1
            return super().read(size)

    stream = Counting(b"x" * (10 * 2**20))
    with pytest.raises(TooLargeError):
        submit_stream(rig.index, rig.store, name="a.mp3", stream=stream, max_bytes=1 * 2**20)
    assert Counting.reads <= 3  # 1 MiB chunks: stopped just past the limit, not after 10 MiB


def test_search_hits_in_a_recording_point_at_the_moment_and_the_file(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docsearch.asr import Transcript
    from docsearch.extract import Unit

    audio = b"pretend recording " * 30
    asset = rig.upload("keynote.mp3", audio, title="Keynote").json()["asset_id"]
    monkeypatch.setattr(
        "docsearch.ingest.pipeline.transcribe",
        lambda *_a, **_k: Transcript(
            [
                Unit("welcome everyone to the glacier workshop", 0, 20_000),
                Unit("now the volcano session begins", 20_000, 45_500),
            ],
            "en",
            0.97,
            45_500,
        ),
    )
    Worker(rig.index, rig.store, worker_id="asr", kinds=("asr",), batch_size=1).run_once()
    info = rig.client.get(f"/v1/assets/{asset}").json()
    assert (info["status"], info["units"], info["duration_ms"], info["language"]) == (
        "indexed",
        2,
        45_500,
        "en",
    )
    hit = rig.client.get("/v1/search?q=volcano").json()["results"][0]
    assert hit["document"]["title"] == "Keynote"
    assert hit["media"] == {
        "asset_id": asset,
        "kind": "transcript",
        "start_ms": 20_000,
        "end_ms": 45_500,
        "file_url": f"/v1/assets/{asset}/file",
    }
    assert rig.client.get(hit["media"]["file_url"]).content == audio
    plain = rig.upload("notes.txt", b"volcano notes on paper").json()["asset_id"]
    Worker(rig.index, rig.store, worker_id="t").run_once()
    texts = list(rig.client.get("/v1/search?q=volcano&mode=any").json()["results"])
    assert {bool(r["media"]) for r in texts} == {
        True,
        False,
    }  # recordings link out; documents do not
    assert plain


def test_metrics_endpoint_reports_requests_queue_and_documents(rig: Rig) -> None:
    rig.upload("a.txt", b"metrics test document about rainfall")
    rig.client.get("/v1/assets/999")  # a 404 on a templated route
    text = rig.client.get("/metrics").text
    assert "docsearch_up 1" in text
    assert (
        'docsearch_http_requests_total{method="POST",route="/v1/documents",status="202"} 1' in text
    )
    assert (
        'route="/v1/assets/{asset_id}",status="404"' in text
    )  # the template, not "/v1/assets/999"
    assert 'docsearch_jobs{state="queued"} 1' in text
    assert 'docsearch_assets{status="queued"} 1' in text
    assert 'docsearch_oldest_waiting_job_seconds{kind="text"}' in text
    assert 'docsearch_oldest_waiting_job_seconds{kind="asr"} 0' in text  # nothing waiting for it
    rig.drain()
    after = rig.client.get("/metrics").text
    assert "docsearch_documents 1" in after
    assert 'docsearch_jobs{state="done"} 1' in after
    assert "docsearch_http_request_duration_seconds_bucket" in after
