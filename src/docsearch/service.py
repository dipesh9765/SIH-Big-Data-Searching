"""The scale-out HTTP service: PostgreSQL index, object store, asynchronous ingestion.

    POST   /v1/documents       store an upload and queue it; 202 with an asset id to poll
    GET    /v1/assets/{id}     where that upload is: queued / processing / indexed / failed
    GET    /v1/assets          recent uploads (optionally by status)
    GET    /v1/documents       indexed documents, newest first
    GET    /v1/documents/{id}  one document's metadata
    GET    /v1/search          BM25-ranked search with snippets
    GET    /v1/stats           index and queue statistics
    GET    /healthz, /readyz   liveness / readiness (database and object store)

Unlike the single-node `api.py`, an upload returns before indexing happens: extraction can take
seconds for a large PDF, and holding a request open for it would tie up a server thread per
upload. The caller polls the asset (or just searches later). Workers (`docsearch.ingest.worker`)
do the indexing; this process never extracts text.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from docsearch import __version__
from docsearch.analysis import query_terms
from docsearch.api import DocumentOut
from docsearch.config import ServiceSettings
from docsearch.engine.pg import InvalidFilterError, PgHit, PgIndex
from docsearch.engine.vectors import VectorStore, fuse
from docsearch.extract import (
    SPEECH_MEDIA_TYPES,
    UnsupportedMediaTypeError,
    media_type_for,
    service_extensions,
)
from docsearch.ingest.objects import ObjectNotFoundError, ObjectStore
from docsearch.ingest.pipeline import (
    EmptyUploadError,
    Submission,
    TooLargeError,
    delete_asset,
    submit_stream,
)
from docsearch.ingest.queue import JobQueue
from docsearch.ingest.worker import build_store
from docsearch.metrics import Registry
from docsearch.semantic import Embedder, FastEmbedder
from docsearch.web import error, install_guard

log = logging.getLogger("docsearch.service")
MAX_LIST = 200


class SubmissionOut(BaseModel):
    asset_id: int
    job_id: int | None
    status: str
    duplicate: bool

    @classmethod
    def of(cls, sub: Submission) -> SubmissionOut:
        return cls(
            asset_id=sub.asset_id, job_id=sub.job_id, status=sub.status, duplicate=sub.duplicate
        )


class AssetOut(BaseModel):
    asset_id: int
    title: str
    source_name: str
    size: int
    status: str
    document_id: int | None
    error: str | None
    created_at: str
    ocr_confidence: float | None
    units: int
    duration_ms: int | None
    language: str | None


class MediaOut(BaseModel):
    """Where in an uploaded recording this hit is spoken."""

    asset_id: int
    kind: str
    start_ms: int | None
    end_ms: int | None
    file_url: str  # the original, seekable with HTTP Range


class HitOut(BaseModel):
    document: DocumentOut
    score: float
    snippet: str
    highlights: list[tuple[int, int]]
    media: MediaOut | None = None

    @classmethod
    def of(cls, hit: PgHit) -> HitOut:
        return cls(
            document=DocumentOut.of(hit.document),
            score=round(hit.score, 6),
            snippet=hit.snippet,
            highlights=hit.highlights,
            media=None
            if hit.media is None
            else MediaOut(
                asset_id=hit.media.asset_id,
                kind=hit.media.unit_kind,
                start_ms=hit.media.start_ms,
                end_ms=hit.media.end_ms,
                file_url=f"/v1/assets/{hit.media.asset_id}/file",
            ),
        )


class ImageHitOut(BaseModel):
    asset_id: int
    title: str
    source_name: str
    similarity: float
    file_url: str


class SearchOut(BaseModel):
    query: str
    mode: Literal["all", "any"]
    total: int | None  # None: pruning skipped matches, so the exact count is not known
    did_you_mean: str | None = None  # a corrected query, offered only when nothing matched
    approximate: bool  # a phrase had more candidates than the engine verified
    strategy: str
    took_ms: float
    results: list[HitOut]


class StatsOut(BaseModel):
    documents: int
    terms: int
    block_postings: int
    delta_postings: int
    average_length: float
    queue: dict[str, int]
    accepted_extensions: list[str]
    max_upload_bytes: int
    version: str
    semantic: bool


_ASSET_COLUMNS = (
    "id, title, source_name, size, status, document_id, error, created_at, ocr_confidence,"
    " units, duration_ms, language"
)


def _asset(row: tuple[object, ...]) -> AssetOut:
    return AssetOut(
        asset_id=row[0],  # type: ignore[arg-type]
        title=row[1],  # type: ignore[arg-type]
        source_name=row[2],  # type: ignore[arg-type]
        size=row[3],  # type: ignore[arg-type]
        status=row[4],  # type: ignore[arg-type]
        document_id=row[5],  # type: ignore[arg-type]
        error=row[6],  # type: ignore[arg-type]
        created_at=row[7].isoformat(timespec="seconds"),  # type: ignore[attr-defined]
        ocr_confidence=None if row[8] is None else round(row[8], 1),  # type: ignore[call-overload]
        units=row[9],  # type: ignore[arg-type]
        duration_ms=row[10],  # type: ignore[arg-type]
        language=row[11],  # type: ignore[arg-type]
    )


FILE_CHUNK = 1024 * 1024
FILE_URL_SECONDS = 600
_FILE_PATH = re.compile(r"^/v1/assets/(\d+)/file$")


def sign_file_token(secret: str, asset_id: int, expires: int) -> str:
    """A time-limited token that lets a media element fetch one asset's file.

    `<audio>` and `<video>` cannot send an Authorization header, so with an API key set the UI asks
    (authenticated) for a short-lived URL instead of putting the key in a page URL."""
    mac = hmac.new(secret.encode(), f"file:{asset_id}:{expires}".encode(), hashlib.sha256)
    return f"{expires}.{mac.hexdigest()}"


def valid_file_token(secret: str, asset_id: int, token: str, now: float) -> bool:
    expires_text, _, _ = token.partition(".")
    if not expires_text.isdigit() or int(expires_text) < now:
        return False
    expected = sign_file_token(secret, asset_id, int(expires_text))
    return hmac.compare_digest(token.encode(), expected.encode())


def _parse_range(  # noqa: PLR0911 - one return per header shape
    header: str | None, total: int
) -> tuple[int | None, int]:
    """(start, end) inclusive for a `Range: bytes=a-b` header; (None, 0) if unsatisfiable.

    Supports one range (`a-b`, `a-`, `-n`), which is what browsers send when seeking. No header
    means the whole file. Multiple ranges are not supported and are treated as unsatisfiable.
    """
    if total == 0:
        return (0, -1) if header is None else (None, 0)
    if header is None:
        return 0, total - 1
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None, 0
    first, _, last = spec.strip().partition("-")
    try:
        if first == "":  # suffix: the last n bytes
            count = int(last)
            if count <= 0:
                return None, 0
            return max(0, total - count), total - 1
        begin = int(first)
        finish = total - 1 if last == "" else min(int(last), total - 1)
    except ValueError:
        return None, 0
    if begin > finish or begin >= total:
        return None, 0
    return begin, finish


def _chunks(store: ObjectStore, key: str, start: int, end: int) -> Iterator[bytes]:
    position = start
    while position <= end:
        piece = store.get_range(key, position, min(FILE_CHUNK, end - position + 1))
        if not piece:
            return
        yield piece
        position += len(piece)


def _inline_disposition(name: str) -> str:
    """`inline` with a header-safe file name (ASCII fallback plus the RFC 5987 UTF-8 form)."""
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("\\", "_")
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def create_service_app(
    settings: ServiceSettings | None = None,
    *,
    index: PgIndex | None = None,
    store: ObjectStore | None = None,
    embedder: Embedder | None = None,
) -> FastAPI:
    """Build the app. `index`, `store` and `embedder` may be injected (tests); else they come from
    settings."""
    settings = settings or ServiceSettings.from_env()
    owns_index = index is None
    index = index or PgIndex(settings.dsn, schema=settings.schema)
    store = store or build_store(settings)
    queue = JobQueue(index)
    vectors: VectorStore | None = None
    if settings.semantic:
        vectors = VectorStore(index)
        embedder = embedder or FastEmbedder(settings.embed_cache)
        if not vectors.available:
            vectors = None
    metrics = Registry()
    metrics.describe(
        "docsearch_http_requests_total", "counter", "HTTP requests by route and status"
    )
    metrics.describe(
        "docsearch_http_request_duration_seconds", "histogram", "HTTP request latency by route"
    )

    def gauges() -> Iterator[tuple[str, dict[str, str], float]]:
        try:
            counts = index.approximate_counts()
            jobs = queue.counts()
            with index.connection() as conn:
                assets: dict[str, int] = dict(
                    conn.execute("SELECT status, count(*) FROM assets GROUP BY status")
                )
                waiting: dict[str, float] = {
                    kind: float(age)
                    for kind, age in conn.execute(
                        "SELECT kind, extract(epoch FROM now() - min(run_after)) FROM ingest_jobs"
                        " WHERE state = 'queued' AND run_after <= now() GROUP BY kind"
                    )
                }
        except Exception:  # a scrape must not fail because the database is briefly unavailable
            log.exception("could not collect gauges")
            yield "docsearch_up", {}, 0.0
            return
        yield "docsearch_up", {}, 1.0
        yield "docsearch_documents", {}, counts["documents"]
        yield "docsearch_indexed_tokens", {}, counts["tokens"]
        yield "docsearch_terms_estimate", {}, counts["terms_estimate"]
        yield "docsearch_delta_postings_estimate", {}, counts["delta_postings_estimate"]
        yield "docsearch_posting_blocks_estimate", {}, counts["blocks_estimate"]
        for state, value in jobs.items():
            yield "docsearch_jobs", {"state": state}, value
        for status in ("queued", "processing", "indexed", "failed"):
            yield "docsearch_assets", {"status": status}, int(assets.get(status, 0))
        # Alert on this: a job kind with no worker (OCR, speech, embeddings) shows up as an age that
        # only grows.
        for kind in ("text", "ocr", "asr", "embed"):
            yield "docsearch_oldest_waiting_job_seconds", {"kind": kind}, waiting.get(kind, 0.0)

    metrics.gauge_source(gauges)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_index:
            index.close()

    def require_key(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> None:
        """When DOCSEARCH_API_KEY is set, every /v1 route needs it (Bearer or X-API-Key). Health,
        readiness, metrics and the docs page are left open for probes and scrapers."""
        if not settings.api_key or not request.url.path.startswith("/v1"):
            return
        match = _FILE_PATH.match(request.url.path)
        token = request.query_params.get("token", "")
        if (
            match
            and token
            and valid_file_token(settings.api_key, int(match.group(1)), token, time.time())
        ):
            return  # a signed, expiring link for exactly this file
        supplied = x_api_key or (
            authorization[7:] if authorization and authorization[:7].lower() == "bearer " else ""
        )
        if not hmac.compare_digest(supplied.encode(), settings.api_key.encode()):
            raise HTTPException(
                status_code=401,
                detail="missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI(
        dependencies=[Depends(require_key)],
        title="docsearch service",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    # Audio and video are far larger than documents, so the early body-size check uses the
    # larger limit and each route enforces the limit for its own type.
    install_guard(app, max(settings.max_upload_bytes, settings.max_media_bytes), metrics)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def prometheus() -> Response:
        """Prometheus scrape endpoint. Unauthenticated: expose it on the internal network only."""
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/readyz")
    def readyz() -> dict[str, object]:
        try:
            documents = index.stats().documents
            store.ping()
        except Exception as exc:
            log.error("readiness check failed: %s", exc)
            raise error(503, "a dependency is unavailable") from exc
        return {"status": "ready", "documents": documents}

    @app.post("/v1/documents", response_model=SubmissionOut, status_code=202)
    def upload(
        response: Response,
        file: Annotated[UploadFile, File(description="A .txt, .md, .csv, .docx or .pdf file")],
        title: Annotated[str | None, Query(max_length=200)] = None,
    ) -> SubmissionOut:
        name = file.filename or ""
        is_media = Path(name.lower()).suffix in SPEECH_MEDIA_TYPES
        limit = settings.max_media_bytes if is_media else settings.max_upload_bytes
        try:
            submission = submit_stream(
                index, store, name=name, stream=file.file, max_bytes=limit, title=title
            )
        except UnsupportedMediaTypeError as exc:
            raise error(415, str(exc)) from exc
        except TooLargeError as exc:
            raise error(413, str(exc)) from exc
        except EmptyUploadError as exc:
            raise error(422, str(exc)) from exc
        except Exception as exc:  # store or database unavailable: the client should retry
            log.error("upload failed: %s", exc)
            raise error(503, "could not accept the upload right now") from exc
        if submission.job_id is None:
            response.status_code = 200  # nothing new was queued: these bytes were uploaded before
        return SubmissionOut.of(submission)

    @app.get("/v1/assets/{asset_id}", response_model=AssetOut)
    def get_asset(asset_id: int) -> AssetOut:
        with index.connection() as conn:
            row = conn.execute(
                f"SELECT {_ASSET_COLUMNS} FROM assets WHERE id = %s", (asset_id,)
            ).fetchone()
        if row is None:
            raise error(404, "asset not found")
        return _asset(row)

    @app.post("/v1/assets/{asset_id}/file-url")
    def file_url(asset_id: int) -> dict[str, str]:
        """A URL for the original file that a media element can load: signed and short-lived when
        an API key is configured, the plain path otherwise."""
        with index.connection() as conn:
            found = conn.execute("SELECT 1 FROM assets WHERE id = %s", (asset_id,)).fetchone()
        if found is None:
            raise error(404, "asset not found")
        path = f"/v1/assets/{asset_id}/file"
        if not settings.api_key:
            return {"url": path}
        expires = int(time.time()) + FILE_URL_SECONDS
        return {"url": f"{path}?token={sign_file_token(settings.api_key, asset_id, expires)}"}

    @app.get("/v1/assets/{asset_id}/file")
    def get_asset_file(
        asset_id: int, range_header: Annotated[str | None, Header(alias="Range")] = None
    ) -> Response:
        """The original file, with HTTP Range support so a browser can seek in audio and video."""
        with index.connection() as conn:
            row = conn.execute(
                "SELECT object_key, source_name FROM assets WHERE id = %s", (asset_id,)
            ).fetchone()
        if row is None:
            raise error(404, "asset not found")
        key, source_name = row
        try:
            total = store.size(key)
        except ObjectNotFoundError as exc:
            raise error(404, "the stored file is missing") from exc
        start, end = _parse_range(range_header, total)
        if start is None:
            raise error(416, "unsatisfiable range")
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": _inline_disposition(source_name),
        }
        if range_header is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return StreamingResponse(
            _chunks(store, key, start, end),
            status_code=206 if range_header is not None else 200,
            media_type=media_type_for(source_name),
            headers=headers,
        )

    @app.delete("/v1/assets/{asset_id}", status_code=204)
    def remove_asset(asset_id: int) -> Response:
        """Delete an upload: it leaves search at once; its original is removed from storage."""
        if not delete_asset(index, store, asset_id):
            raise error(404, "asset not found")
        return Response(status_code=204)

    @app.delete("/v1/documents/{doc_id}", status_code=204)
    def remove_document(doc_id: int) -> Response:
        if index.delete_documents([doc_id]) == 0:
            raise error(404, "document not found")
        return Response(status_code=204)

    @app.get("/v1/assets", response_model=list[AssetOut])
    def list_assets(
        status: Literal["queued", "processing", "indexed", "failed"] | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> list[AssetOut]:
        with index.connection() as conn:
            rows = conn.execute(
                f"SELECT {_ASSET_COLUMNS} FROM assets WHERE (%s::text IS NULL OR status = %s)"
                " ORDER BY id DESC LIMIT %s OFFSET %s",
                (status, status, limit, offset),
            ).fetchall()
        return [_asset(row) for row in rows]

    @app.get("/v1/documents", response_model=list[DocumentOut])
    def list_documents(
        limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> list[DocumentOut]:
        return [DocumentOut.of(d) for d in index.list_documents(limit=limit, offset=offset)]

    @app.get("/v1/documents/{doc_id}", response_model=DocumentOut)
    def get_document(doc_id: int) -> DocumentOut:
        document = index.get_document(doc_id)
        if document is None:
            raise error(404, "document not found")
        return DocumentOut.of(document)

    @app.get("/v1/search", response_model=SearchOut)
    def search(  # noqa: PLR0913, PLR0917 - every parameter is a documented query option
        q: Annotated[str, Query(min_length=1, max_length=500)],
        mode: Literal["all", "any"] = "all",
        limit: Annotated[int, Query(ge=1)] = 10,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
        exact_total: bool = False,
        ranking: Literal["keyword", "semantic", "hybrid"] = "keyword",
    ) -> SearchOut:
        """`ranking=keyword` is BM25 (with the query language); `semantic` ranks by meaning
        (works across languages); `hybrid` fuses both with reciprocal rank fusion."""
        limit = min(limit, settings.max_results)
        started = time.perf_counter()
        if ranking != "keyword":
            if vectors is None or embedder is None:
                raise error(501, "semantic search is not enabled on this server")
            return _semantic_search(q, mode, limit, offset, ranking, started)
        try:
            result = index.query(q, mode=mode, limit=limit, offset=offset, exact_total=exact_total)
        except InvalidFilterError as exc:
            raise error(422, str(exc)) from exc
        return SearchOut(
            query=q,
            mode=mode,
            total=result.total,
            approximate=result.approximate,
            did_you_mean=None if result.hits else index.did_you_mean(q),
            strategy=result.strategy,
            took_ms=round((time.perf_counter() - started) * 1000, 2),
            results=[HitOut.of(h) for h in result.hits],
        )

    def _semantic_search(  # noqa: PLR0913, PLR0917 - a private helper with plain arguments
        q: str,
        mode: Literal["all", "any"],
        limit: int,
        offset: int,
        ranking: str,
        started: float,
    ) -> SearchOut:
        assert vectors is not None
        assert embedder is not None
        need = offset + limit
        terms = set(query_terms(q))
        semantic = vectors.search_text(embedder.embed_query(q), max(need, 50))
        if ranking == "semantic":
            scored = semantic[offset:need]
            hits = index.hits_for(scored, terms)
            strategy = "semantic"
        else:
            keyword = index.query(q, mode=mode, limit=max(need, 50)).hits
            fused = fuse([[h.document.id for h in keyword], [d for d, _ in semantic]])
            known = {h.document.id: h for h in keyword}
            page = fused[offset:need]
            extra = index.hits_for([(d, s) for d, s in page if d not in known], terms)
            built = {h.document.id: h for h in extra}
            hits = [
                PgHit(
                    (known.get(d) or built[d]).document,
                    s,
                    (known.get(d) or built[d]).snippet,
                    (known.get(d) or built[d]).highlights,
                    (known.get(d) or built[d]).media,
                )
                for d, s in page
            ]
            strategy = "hybrid"
        return SearchOut(
            query=q,
            mode=mode,
            total=None,
            approximate=False,
            strategy=strategy,
            took_ms=round((time.perf_counter() - started) * 1000, 2),
            results=[HitOut.of(h) for h in hits],
        )

    @app.get("/v1/search/images", response_model=list[ImageHitOut])
    def search_images(
        q: Annotated[str, Query(min_length=1, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> list[ImageHitOut]:
        """Find pictures by describing them (CLIP). Needs semantic search to be enabled."""
        if vectors is None or embedder is None:
            raise error(501, "image search is not enabled on this server")
        found = vectors.search_images(embedder.embed_image_query(q), limit)
        if not found:
            return []
        with index.connection() as conn:
            rows = {
                r[0]: r
                for r in conn.execute(
                    "SELECT id, title, source_name FROM assets WHERE id = ANY(%s)",
                    ([a for a, _ in found],),
                )
            }
        return [
            ImageHitOut(
                asset_id=a,
                title=rows[a][1],
                source_name=rows[a][2],
                similarity=round(s, 4),
                file_url=f"/v1/assets/{a}/file",
            )
            for a, s in found
            if a in rows
        ]

    @app.get("/v1/suggest", response_model=list[str])
    def suggest(
        q: Annotated[str, Query(min_length=1, max_length=100)],
        limit: Annotated[int, Query(ge=1, le=20)] = 8,
    ) -> list[str]:
        """Autocomplete: words starting with the last word typed."""
        words = q.split()
        return index.complete(words[-1], limit=limit) if words else []

    @app.get("/v1/stats", response_model=StatsOut)
    def stats() -> StatsOut:
        s = index.stats()
        return StatsOut(
            documents=s.documents,
            terms=s.terms,
            block_postings=s.block_postings,
            delta_postings=s.delta_postings,
            average_length=s.average_length,
            queue=queue.counts(),
            accepted_extensions=service_extensions(),
            max_upload_bytes=settings.max_upload_bytes,
            version=__version__,
            semantic=vectors is not None,
        )

    if settings.web_dir and Path(settings.web_dir).is_dir():
        # Mounted last, so every API route above takes precedence over the static files.
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


def create_app() -> FastAPI:
    """Factory for `uvicorn docsearch.service:create_app --factory`."""
    return create_service_app()
