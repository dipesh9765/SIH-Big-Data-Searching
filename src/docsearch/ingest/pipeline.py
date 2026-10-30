"""Upload -> object store + job -> worker -> extracted text -> index.

`submit` is called by the API, `process_batch` by workers. The two meet only through the database
(an asset row and a job row), so they scale independently and a worker crash loses nothing: the
job's lease expires and another worker repeats the work. Every step is therefore idempotent:

* the object key is the SHA-256 of the bytes, so storing twice is a no-op;
* indexing the same text twice is a no-op (`documents.content_sha256` is unique);
* marking an asset indexed twice writes the same row.

Errors are sorted into *permanent* (the file is corrupt, encrypted, unsupported or empty: retrying
cannot help, so fail fast with a message the uploader can read) and *transient* (the object store or
database hiccuped: retry with backoff).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from docsearch.engine.pg import NewDocument, PgIndex
from docsearch.extract import (
    IMAGE_MEDIA_TYPES,
    Extracted,
    ExtractionError,
    NeedsOcrError,
    UnsupportedMediaTypeError,
    clean_filename,
    clean_text,
    extract_text,
    service_extensions,
)
from docsearch.ingest.objects import ObjectNotFoundError, ObjectStore, object_key
from docsearch.ingest.queue import Job, JobQueue
from docsearch.ocr import OcrConfig, ocr_image, ocr_pdf

log = logging.getLogger("docsearch.ingest")

MAX_TITLE_CHARS = 200


@dataclass(frozen=True, slots=True)
class Submission:
    asset_id: int
    job_id: int | None  # None when nothing new was queued (the same bytes were already uploaded)
    status: str  # queued | processing | indexed | failed
    duplicate: bool


@dataclass(frozen=True, slots=True)
class BatchResult:
    indexed: int = 0
    retried: int = 0
    failed: int = 0
    rerouted: int = 0  # text jobs handed to the OCR pool (scanned PDFs)
    lost: int = 0  # leases that expired while we worked: another worker owns the job now


def submit(  # noqa: PLR0913 - one entry point; everything after `store` is keyword-only
    index: PgIndex,
    store: ObjectStore,
    *,
    name: str,
    data: bytes,
    title: str | None = None,
    max_attempts: int = 5,
) -> Submission:
    """Store `data` and queue it for indexing. Raises `UnsupportedMediaTypeError` for unknown types.

    Uploading bytes that are already stored returns the existing asset instead of a new job. If
    that asset had failed, it is queued again: re-uploading is the way to ask for a retry.
    """
    clean = clean_filename(name)
    suffix = Path(clean).suffix.lower()
    if suffix not in service_extensions():
        raise UnsupportedMediaTypeError(
            f"unsupported file type {suffix or '(none)'!r}; "
            f"accepted: {', '.join(service_extensions())}"
        )
    kind = "ocr" if suffix in IMAGE_MEDIA_TYPES else "text"  # images always need recognition
    shown = ((title or "").strip() or clean)[:MAX_TITLE_CHARS]
    digest = hashlib.sha256(data).digest()
    key = object_key(digest.hex())
    store.put(key, data)  # first: an orphaned object is harmless, a row without its file is not
    with index.connection() as conn:
        created = conn.execute(
            "INSERT INTO assets (object_key, sha256, source_name, title, size)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING RETURNING id",
            (key, digest, clean, shown, len(data)),
        ).fetchone()
        if created is not None:
            return Submission(
                created[0], _enqueue(conn, created[0], max_attempts, kind), "queued", False
            )
        existing = conn.execute(
            "SELECT id, status FROM assets WHERE sha256 = %s FOR UPDATE", (digest,)
        ).fetchone()
        assert existing is not None  # the conflict proves the row exists
        asset_id, status = existing
        if status == "failed":
            conn.execute(
                "UPDATE assets SET status = 'queued', error = NULL, updated_at = now()"
                " WHERE id = %s",
                (asset_id,),
            )
            return Submission(
                asset_id, _enqueue(conn, asset_id, max_attempts, kind), "queued", True
            )
        return Submission(asset_id, None, status, True)


def _enqueue(conn: object, asset_id: int, max_attempts: int, kind: str) -> int:
    row = conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO ingest_jobs (asset_id, kind, max_attempts) VALUES (%s, %s, %s) RETURNING id",
        (asset_id, kind, max_attempts),
    ).fetchone()
    return int(row[0])


@dataclass(frozen=True, slots=True)
class _Failure:
    message: str
    retry: bool


@dataclass(frozen=True, slots=True)
class _Reroute:
    kind: str


def _extract(  # noqa: PLR0911, PLR0913, PLR0917 - one return per outcome; plain arguments
    store: ObjectStore,
    source_name: str,
    key: str,
    job: Job,
    ocr: OcrConfig,
    heartbeat: Callable[[int, int], None],
) -> Extracted | _Failure | _Reroute:
    """Fetch and extract one file, sorting failures into permanent and transient.

    `text` jobs parse the file directly and hand scanned PDFs on to the OCR pool; `ocr` jobs
    recognise images and the scanned pages of PDFs.
    """
    try:
        data = store.get(key)
        if job.kind != "ocr":
            return extract_text(source_name, data, detect_scanned=True)
        suffix = Path(source_name).suffix.lower()
        if suffix in IMAGE_MEDIA_TYPES:
            result = ocr_image(data, ocr)
            media_type = IMAGE_MEDIA_TYPES[suffix]
        elif suffix == ".pdf":
            result = ocr_pdf(data, ocr, on_page=heartbeat)
            media_type = "application/pdf"
        else:
            return _Failure("OCR handles images and PDFs only", retry=False)
        return Extracted(clean_text(result.text), media_type, result.confidence)
    except NeedsOcrError:
        return _Reroute("ocr")
    except ObjectNotFoundError:
        return _Failure("the stored file is missing", retry=False)
    except ExtractionError as exc:
        return _Failure(str(exc), retry=False)
    except Exception as exc:  # transient: the store or a decoder hiccuped; backoff decides
        log.warning("job %d attempt %d failed: %s", job.id, job.attempts, exc)
        return _Failure(f"{type(exc).__name__}: {exc}", retry=True)


def process_batch(  # noqa: PLR0912, PLR0913 - one orchestration function
    index: PgIndex,
    queue: JobQueue,
    store: ObjectStore,
    jobs: list[Job],
    worker_id: str,
    *,
    ocr: OcrConfig | None = None,
    lease_seconds: float = 300.0,
) -> BatchResult:
    """Extract and index the files behind `jobs`, then settle each job individually.

    Extraction is per file; indexing is one transaction for the whole batch (fewer lock
    acquisitions and round trips). A file that fails extraction does not hold up the others.
    """
    if not jobs:
        return BatchResult()
    with index.connection() as conn:
        assets = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT id, source_name, title, object_key FROM assets WHERE id = ANY(%s)",
                ([job.asset_id for job in jobs],),
            )
        }
    ocr = ocr or OcrConfig()
    indexed = retried = failed = lost = rerouted = 0

    def settle_failure(job: Job, error: str, *, retry: bool) -> None:
        nonlocal retried, failed, lost
        outcome = queue.fail(job.id, worker_id, error, retry=retry)
        if outcome == "queued":
            retried += 1
        elif outcome == "dead":
            failed += 1
        else:
            lost += 1

    ready: list[tuple[Job, str, Extracted]] = []

    def result_of() -> BatchResult:
        return BatchResult(indexed, retried, failed, rerouted, lost)

    for job in jobs:
        meta = assets.get(job.asset_id)
        if meta is None:
            settle_failure(job, "asset record is missing", retry=False)
            continue
        source_name, title, key = meta

        def heartbeat(_done: int, _total: int, job: Job = job) -> None:
            queue.extend(job.id, worker_id, lease_seconds)

        outcome = _extract(store, source_name, key, job, ocr, heartbeat)
        if isinstance(outcome, _Failure):
            settle_failure(job, outcome.message, retry=outcome.retry)
        elif isinstance(outcome, _Reroute):
            if queue.reroute(job.id, worker_id, outcome.kind):
                rerouted += 1
            else:
                lost += 1
        else:
            ready.append((job, title, outcome))
    if not ready:
        return result_of()

    documents = [
        NewDocument(title, assets[job.asset_id][0], extracted.media_type, extracted.text)
        for job, title, extracted in ready
    ]
    try:
        added = index.add_many_skipping_empty(documents)
    except Exception as exc:
        log.warning("indexing a batch of %d failed: %s", len(ready), exc)
        for job, _, _ in ready:
            settle_failure(job, f"{type(exc).__name__}: {exc}", retry=True)
        return result_of()
    for (job, _, extracted), result in zip(ready, added, strict=True):
        if result is None:
            settle_failure(job, "the file contains no indexable text", retry=False)
            continue
        with index.connection() as conn:
            conn.execute(
                "UPDATE assets SET status = 'indexed', document_id = %s, error = NULL,"
                " ocr_confidence = %s, updated_at = now() WHERE id = %s",
                (result.document_id, extracted.confidence, job.asset_id),
            )
        if queue.complete(job.id, worker_id):
            indexed += 1
        else:
            lost += 1
    return result_of()
