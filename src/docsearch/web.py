"""HTTP plumbing shared by the single-node API and the scale-out service."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from docsearch.metrics import Registry

log = logging.getLogger("docsearch")

MULTIPART_OVERHEAD = 64 * 1024  # headers and boundaries on top of the file itself
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def error(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def install_guard(app: FastAPI, max_upload_bytes: int, metrics: Registry | None = None) -> None:
    """Request id, security headers, access log, and an early 413 for oversized bodies.

    The declared `Content-Length` is checked before the body is read, so an oversized upload is
    refused without buffering it. (Routes still bound what they read: the header can lie.)
    """

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        declared = request.headers.get("content-length", "")
        too_big = declared.isdigit() and int(declared) > max_upload_bytes + MULTIPART_OVERHEAD
        if too_big:
            response: Response = JSONResponse({"detail": "request body too large"}, status_code=413)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        elapsed = time.perf_counter() - started
        if metrics is not None:
            route = request.scope.get("route")
            # The route *template* ("/v1/assets/{asset_id}"), never the raw path: raw paths would
            # make one metric series per id.
            path = getattr(route, "path", "unmatched")
            metrics.inc(
                "docsearch_http_requests_total",
                method=request.method,
                route=path,
                status=str(response.status_code),
            )
            metrics.observe(
                "docsearch_http_request_duration_seconds",
                elapsed,
                method=request.method,
                route=path,
            )
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
