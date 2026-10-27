from typing import Any

import pytest
from fastapi.testclient import TestClient

from docsearch.api import create_app
from docsearch.config import Settings
from docsearch.index import Index
from tests.conftest import make_docx, make_pdf


def upload(client: TestClient, name: str, data: bytes, **params: str) -> dict[str, Any]:
    response = client.post("/v1/documents", files={"file": (name, data)}, params=params)
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return body


def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready", "documents": 0}


def test_upload_search_roundtrip_for_every_file_type(client: TestClient) -> None:
    upload(client, "notes.txt", b"The migration of arctic terns spans two poles")
    upload(client, "table.csv", b"species,habitat\nwalrus,coastal ice\n")
    upload(client, "memo.docx", make_docx("Budget approval for the telescope"))
    upload(client, "paper.pdf", make_pdf("Quantum entanglement experiment results"))
    for query, expected in [
        ("arctic tern", "notes.txt"),
        ("walrus", "table.csv"),
        ("telescope budget", "memo.docx"),
        ("entangled quantum", "paper.pdf"),
    ]:
        body = client.get("/v1/search", params={"q": query}).json()
        assert [r["document"]["source_name"] for r in body["results"]] == [expected], query


def test_search_response_has_snippet_highlights_and_timing(client: TestClient) -> None:
    upload(client, "a.txt", b"Alpha bravo charlie. The needle sits here. Delta echo.")
    body = client.get("/v1/search", params={"q": "needle"}).json()
    hit = body["results"][0]
    start, end = hit["highlights"][0]
    assert hit["snippet"][start:end] == "needle"
    assert body["total"] == 1
    assert body["took_ms"] >= 0
    assert hit["document"]["tokens"] > 0


def test_duplicate_upload_returns_200_and_the_existing_document(client: TestClient) -> None:
    first = client.post("/v1/documents", files={"file": ("a.txt", b"same content here")})
    second = client.post("/v1/documents", files={"file": ("b.txt", b"same content here")})
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["duplicate"] is True


def test_custom_title_and_sanitised_filename(client: TestClient) -> None:
    doc = upload(client, "../../evil/name.txt", b"some words to index", title="  My title ")
    assert doc["title"] == "My title"
    assert doc["source_name"] == "name.txt"


def test_delete_then_gone(client: TestClient) -> None:
    doc = upload(client, "a.txt", b"ephemeral content")
    assert client.delete(f"/v1/documents/{doc['id']}").status_code == 204
    assert client.get(f"/v1/documents/{doc['id']}").status_code == 404
    assert client.delete(f"/v1/documents/{doc['id']}").status_code == 404
    assert client.get("/v1/search", params={"q": "ephemeral"}).json()["total"] == 0


def test_list_and_get_documents(client: TestClient) -> None:
    ids = [upload(client, f"{n}.txt", f"unique{n} words".encode())["id"] for n in range(3)]
    listing = client.get("/v1/documents", params={"limit": 2}).json()
    assert [d["id"] for d in listing] == [ids[2], ids[1]]
    assert client.get(f"/v1/documents/{ids[0]}").json()["id"] == ids[0]


def test_error_statuses(client: TestClient) -> None:
    assert client.post("/v1/documents", files={"file": ("a.exe", b"MZ")}).status_code == 415
    assert client.post("/v1/documents", files={"file": ("a.txt", b"the of and")}).status_code == 422
    assert client.post("/v1/documents", files={"file": ("a.pdf", b"nope")}).status_code == 422
    assert client.post("/v1/documents").status_code == 422  # no file part
    assert client.get("/v1/documents/999").status_code == 404


def test_oversized_upload_is_rejected_both_ways(client: TestClient, settings: Settings) -> None:
    big = b"word " * (settings.max_upload_bytes // 5 + 10)
    assert client.post("/v1/documents", files={"file": ("big.txt", big)}).status_code == 413
    # Declared length alone is enough to refuse before the body is read.
    huge = client.post("/v1/documents", content=b"x", headers={"content-length": str(10**9)})
    assert huge.status_code == 413


def test_search_validation_and_limits(client: TestClient) -> None:
    for n in range(8):
        upload(client, f"{n}.txt", f"shared term{n}".encode())
    assert client.get("/v1/search").status_code == 422
    assert client.get("/v1/search", params={"q": ""}).status_code == 422
    assert client.get("/v1/search", params={"q": "x" * 501}).status_code == 422
    assert client.get("/v1/search", params={"q": "a", "mode": "fuzzy"}).status_code == 422
    capped = client.get("/v1/search", params={"q": "shared", "limit": 100}).json()
    assert (capped["total"], len(capped["results"])) == (8, 5)  # max_results=5 in the fixture
    page2 = client.get("/v1/search", params={"q": "shared", "limit": 5, "offset": 5}).json()
    assert len(page2["results"]) == 3


def test_modes(client: TestClient) -> None:
    upload(client, "both.txt", b"red green")
    upload(client, "one.txt", b"red blue")
    assert client.get("/v1/search", params={"q": "red green"}).json()["total"] == 1
    assert client.get("/v1/search", params={"q": "red green", "mode": "any"}).json()["total"] == 2


def test_injection_like_queries_are_just_text(client: TestClient) -> None:
    upload(client, "a.txt", b"harmless content")
    for q in ["' OR 1=1 --", '"; DROP TABLE documents; --', "%", "<script>alert(1)</script>"]:
        assert client.get("/v1/search", params={"q": q}).status_code == 200
    assert client.get("/v1/stats").json()["documents"] == 1


def test_stored_markup_is_returned_as_inert_text(client: TestClient) -> None:
    upload(client, "x.txt", b"<img src=x onerror=alert(1)> payload")
    snippet = client.get("/v1/search", params={"q": "payload"}).json()["results"][0]["snippet"]
    assert "<img" in snippet  # returned verbatim; the UI renders it with textContent


def test_stats_reports_capabilities(client: TestClient, settings: Settings) -> None:
    upload(client, "a.txt", b"alpha beta")
    stats = client.get("/v1/stats").json()
    assert stats["documents"] == 1
    assert stats["terms"] == 2
    assert ".pdf" in stats["accepted_extensions"]
    assert stats["max_upload_bytes"] == settings.max_upload_bytes


def test_security_headers_request_id_and_caching(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert len(response.headers["x-request-id"]) == 12
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "content-security-policy" not in client.get("/docs").headers


def test_ui_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert '<script src="/static/app.js"' in page.text
    assert "<script>" not in page.text  # no inline script: compatible with the strict CSP
    assert client.get("/static/app.js").status_code == 200


def test_readyz_reports_503_when_storage_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(create_app(settings)) as c:

        def broken(self: Index) -> None:
            raise RuntimeError("db gone")

        monkeypatch.setattr(Index, "stats", broken)
        assert c.get("/readyz").status_code == 503


def test_data_persists_across_app_restarts(settings: Settings) -> None:
    with TestClient(create_app(settings)) as first:
        upload(first, "a.txt", b"persistent knowledge")
    with TestClient(create_app(settings)) as second:
        assert second.get("/v1/search", params={"q": "persistent"}).json()["total"] == 1
