"""HTTP API and web UI.

Routes (all JSON except the page itself):
    POST   /v1/documents        upload a file, extract its text and index it
    GET    /v1/documents        list documents, newest first
    GET    /v1/documents/{id}   one document's metadata
    DELETE /v1/documents/{id}   remove a document from the index
    GET    /v1/search           BM25-ranked search with snippets
    GET    /v1/stats            corpus statistics and accepted upload types
    GET    /healthz, /readyz    liveness / readiness
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from docsearch import __version__
from docsearch.config import Settings
from docsearch.extract import (
    ExtractionError,
    UnsupportedMediaTypeError,
    clean_filename,
    extract_text,
    supported_extensions,
)
from docsearch.index import Document, EmptyDocumentError, Hit, Index

log = logging.getLogger("docsearch")
STATIC_DIR = Path(__file__).parent / "static"
MULTIPART_OVERHEAD = 64 * 1024  # headers and boundaries on top of the file itself
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class DocumentOut(BaseModel):
    id: int
    title: str
    source_name: str
    media_type: str
    tokens: int
    created_at: str

    @classmethod
    def of(cls, doc: Document) -> DocumentOut:
        return cls(
            id=doc.id,
            title=doc.title,
            source_name=doc.source_name,
            media_type=doc.media_type,
            tokens=doc.length,
            created_at=doc.created_at,
        )


class UploadOut(DocumentOut):
    duplicate: bool


class HitOut(BaseModel):
    document: DocumentOut
    score: float
    snippet: str
    highlights: list[tuple[int, int]]

    @classmethod
    def of(cls, hit: Hit) -> HitOut:
        return cls(
            document=DocumentOut.of(hit.document),
            score=round(hit.score, 6),
            snippet=hit.snippet,
            highlights=hit.highlights,
        )


class SearchOut(BaseModel):
    query: str
    mode: Literal["all", "any"]
    total: int
    took_ms: float
    results: list[HitOut]


class StatsOut(BaseModel):
    documents: int
    terms: int
    postings: int
    average_length: float
    accepted_extensions: list[str]
    max_upload_bytes: int
    version: str


def _error(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    index = Index(settings.db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        index.close()

    app = FastAPI(
        title="docsearch", version=__version__, docs_url="/docs", redoc_url=None, lifespan=lifespan
    )

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        declared = request.headers.get("content-length", "")
        too_big = (
            declared.isdigit() and int(declared) > settings.max_upload_bytes + MULTIPART_OVERHEAD
        )
        if too_big:
            response: Response = JSONResponse({"detail": "request body too large"}, status_code=413)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        for name, value in SECURITY_HEADERS.items():
            # Swagger UI at /docs needs inline script and a CDN, so it is the one page without CSP.
            if name == "Content-Security-Policy" and request.url.path == "/docs":
                continue
            response.headers.setdefault(name, value)
        if request.url.path.startswith(("/static", "/v1")) or request.url.path == "/":
            response.headers.setdefault("Cache-Control", "no-cache")
        # Method, path and status only: query strings carry what people search for.
        log.info(
            "%s %s -> %d in %.1f ms [%s]",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request_id,
        )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, object]:
        try:
            return {"status": "ready", "documents": index.stats().documents}
        except Exception as exc:
            log.error("readiness check failed: %s", exc)
            raise _error(503, "index unavailable") from exc

    @app.post("/v1/documents", response_model=UploadOut, status_code=201)
    def upload(
        response: Response,
        file: Annotated[UploadFile, File(description="A .txt, .md, .csv, .docx or .pdf file")],
        title: Annotated[str | None, Query(max_length=200)] = None,
    ) -> UploadOut:
        name = clean_filename(file.filename or "")
        data = file.file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise _error(413, f"file exceeds {settings.max_upload_bytes // 2**20} MB")
        try:
            extracted = extract_text(name, data)
            added = index.add(
                title=(title or name).strip() or name,
                source_name=name,
                media_type=extracted.media_type,
                text=extracted.text,
            )
        except UnsupportedMediaTypeError as exc:
            raise _error(415, str(exc)) from exc
        except (ExtractionError, EmptyDocumentError) as exc:
            raise _error(422, str(exc)) from exc
        if added.duplicate:
            response.status_code = 200
        return UploadOut(**DocumentOut.of(added.document).model_dump(), duplicate=added.duplicate)

    @app.get("/v1/documents", response_model=list[DocumentOut])
    def list_documents(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> list[DocumentOut]:
        return [DocumentOut.of(d) for d in index.list_documents(limit=limit, offset=offset)]

    @app.get("/v1/documents/{doc_id}", response_model=DocumentOut)
    def get_document(doc_id: int) -> DocumentOut:
        doc = index.get(doc_id)
        if doc is None:
            raise _error(404, "document not found")
        return DocumentOut.of(doc)

    @app.delete("/v1/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: int) -> Response:
        if not index.delete(doc_id):
            raise _error(404, "document not found")
        return Response(status_code=204)

    @app.get("/v1/search", response_model=SearchOut)
    def search(
        q: Annotated[str, Query(min_length=1, max_length=500)],
        mode: Literal["all", "any"] = "all",
        limit: Annotated[int, Query(ge=1)] = 10,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> SearchOut:
        limit = min(limit, settings.max_results)
        started = time.perf_counter()
        result = index.search(q, mode=mode, limit=limit, offset=offset)
        return SearchOut(
            query=q,
            mode=mode,
            total=result.total,
            took_ms=round((time.perf_counter() - started) * 1000, 2),
            results=[HitOut.of(h) for h in result.hits],
        )

    @app.get("/v1/stats", response_model=StatsOut)
    def stats() -> StatsOut:
        return StatsOut(
            **asdict(index.stats()),
            accepted_extensions=supported_extensions(),
            max_upload_bytes=settings.max_upload_bytes,
            version=__version__,
        )

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
