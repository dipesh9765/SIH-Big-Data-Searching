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
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from docsearch.analysis import script_of
from docsearch.asr import AsrConfig, transcribe
from docsearch.engine.pg import NewDocument, PgIndex
from docsearch.extract import (
    IMAGE_MEDIA_TYPES,
    SPEECH_MEDIA_TYPES,
    Extracted,
    ExtractionError,
    NeedsOcrError,
    Unit,
    UnsupportedMediaTypeError,
    clean_filename,
    clean_text,
    extract_text,
    media_type_for,
    service_extensions,
)
from docsearch.ingest.objects import ObjectNotFoundError, ObjectStore, object_key
from docsearch.ingest.queue import Job, JobQueue, notify_channel
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


class TooLargeError(Exception):
    """The upload exceeds the size limit for its type."""


class EmptyUploadError(ValueError):
    """The upload has no bytes."""


UPLOAD_CHUNK = 1024 * 1024


def _plan(name: str, title: str | None) -> tuple[str, str, str]:
    """Validate a file name and decide how it will be processed: (clean name, job kind, title)."""
    clean = clean_filename(name)
    suffix = Path(clean).suffix.lower()
    if suffix not in service_extensions():
        raise UnsupportedMediaTypeError(
            f"unsupported file type {suffix or '(none)'!r}; "
            f"accepted: {', '.join(service_extensions())}"
        )
    if suffix in SPEECH_MEDIA_TYPES:
        kind = "asr"  # audio and video: speech-to-text
    elif suffix in IMAGE_MEDIA_TYPES:
        kind = "ocr"  # images always need recognition
    else:
        kind = "text"
    return clean, kind, ((title or "").strip() or clean)[:MAX_TITLE_CHARS]


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
    clean, kind, shown = _plan(name, title)
    digest = hashlib.sha256(data).digest()
    key = object_key(digest.hex())
    store.put(key, data, media_type_for(clean))  # first: an orphan is harmless, a dangling row not
    return _register(index, key, digest, len(data), clean, shown, kind, max_attempts)


def submit_stream(  # noqa: PLR0913 - one entry point; everything after `store` is keyword-only
    index: PgIndex,
    store: ObjectStore,
    *,
    name: str,
    stream: BinaryIO,
    max_bytes: int,
    title: str | None = None,
    max_attempts: int = 5,
) -> Submission:
    """Like `submit`, for large uploads: spools `stream` to disk in chunks (hashing as it goes),
    never holding the file in memory, and uploads it from there. Raises `TooLargeError` as soon as
    more than `max_bytes` have been read."""
    clean, kind, shown = _plan(name, title)  # reject by type before reading a byte
    with tempfile.TemporaryDirectory(prefix="docsearch-upload-") as scratch:
        path = Path(scratch) / "upload"  # a fixed name: nothing from the client reaches the disk
        digest = hashlib.sha256()
        size = 0
        with path.open("wb") as out:
            while chunk := stream.read(UPLOAD_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise TooLargeError(f"file exceeds {max_bytes // 2**20} MB")
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise EmptyUploadError("the file is empty")
        key = object_key(digest.hexdigest())
        store.put_file(key, path, media_type_for(clean))
    return _register(index, key, digest.digest(), size, clean, shown, kind, max_attempts)


def _register(  # noqa: PLR0913, PLR0917 - an internal step with plain arguments
    index: PgIndex,
    key: str,
    digest: bytes,
    size: int,
    clean: str,
    shown: str,
    kind: str,
    max_attempts: int,
) -> Submission:
    channel = notify_channel(index.schema)
    with index.connection() as conn:
        created = conn.execute(
            "INSERT INTO assets (object_key, sha256, source_name, title, size)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING RETURNING id",
            (key, digest, clean, shown, size),
        ).fetchone()
        if created is not None:
            return Submission(
                created[0], _enqueue(conn, created[0], max_attempts, kind, channel), "queued", False
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
                asset_id, _enqueue(conn, asset_id, max_attempts, kind, channel), "queued", True
            )
        return Submission(asset_id, None, status, True)


def delete_asset(index: PgIndex, store: ObjectStore, asset_id: int) -> bool:
    """Remove an upload: its searchable text, its jobs, its row and the stored original.

    Order matters for crash safety: the documents are tombstoned first (idempotent), so a crash
    afterwards leaves an asset that can simply be deleted again, never searchable text without an
    owner. A text shared with another asset (identical content) is kept for that asset.
    """
    with index.connection() as conn:
        row = conn.execute(
            "SELECT object_key, document_id FROM assets WHERE id = %s", (asset_id,)
        ).fetchone()
        if row is None:
            return False
        key, document_id = row
        ids = [
            r[0] for r in conn.execute("SELECT id FROM documents WHERE asset_id = %s", (asset_id,))
        ]
        if document_id is not None and document_id not in ids:
            shared = conn.execute(
                "SELECT 1 FROM assets WHERE document_id = %s AND id <> %s LIMIT 1",
                (document_id, asset_id),
            ).fetchone()
            if shared is None:
                ids.append(document_id)
    index.delete_documents(ids)
    with index.connection() as conn:
        conn.execute("DELETE FROM ingest_jobs WHERE asset_id = %s", (asset_id,))
        conn.execute("DELETE FROM assets WHERE id = %s", (asset_id,))
    store.delete(key)
    return True


def _enqueue(conn: object, asset_id: int, max_attempts: int, kind: str, channel: str) -> int:
    row = conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO ingest_jobs (asset_id, kind, max_attempts) VALUES (%s, %s, %s) RETURNING id",
        (asset_id, kind, max_attempts),
    ).fetchone()
    conn.execute("SELECT pg_notify(%s, '')", (channel,))  # type: ignore[attr-defined]  # on commit
    return int(row[0])


class _Throttle:
    """True at most once per `interval` seconds."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last = time.monotonic()

    def due(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            return True
        return False


@dataclass(frozen=True, slots=True)
class _Failure:
    message: str
    retry: bool


@dataclass(frozen=True, slots=True)
class _Reroute:
    kind: str


def _transcribe(
    store: ObjectStore,
    source_name: str,
    key: str,
    asr: AsrConfig,
    heartbeat: Callable[[float, float], None],
) -> Extracted:
    """Download a recording to a scratch file and transcribe it into timestamped units."""
    suffix = Path(source_name).suffix.lower()
    if suffix not in SPEECH_MEDIA_TYPES:
        raise ExtractionError("speech recognition handles audio and video only")
    with tempfile.TemporaryDirectory(prefix="docsearch-asr-") as scratch:
        path = (
            Path(scratch) / f"media{suffix}"
        )  # an extension from our own whitelist, no client text
        store.download(key, path)
        transcript = transcribe(path, asr, on_progress=heartbeat)
    units = tuple(Unit(clean_text(u.text), u.start_ms, u.end_ms) for u in transcript.units)
    return Extracted(
        text=" ".join(u.text for u in units),
        media_type=SPEECH_MEDIA_TYPES[suffix],
        units=units,
        duration_ms=transcript.duration_ms,
        language=transcript.language,
    )


def _extract(  # noqa: PLR0911, PLR0913, PLR0917 - one return per outcome; plain arguments
    store: ObjectStore,
    source_name: str,
    key: str,
    job: Job,
    ocr: OcrConfig,
    asr: AsrConfig,
    heartbeat: Callable[[float, float], None],
) -> Extracted | _Failure | _Reroute:
    """Fetch and extract one file, sorting failures into permanent and transient.

    `text` jobs parse the file directly and hand scanned PDFs on to the OCR pool; `ocr` jobs
    recognise images and the scanned pages of PDFs.
    """
    try:
        if job.kind == "asr":
            return _transcribe(store, source_name, key, asr, heartbeat)
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


def process_batch(  # noqa: PLR0912, PLR0913, PLR0915 - one orchestration function
    index: PgIndex,
    queue: JobQueue,
    store: ObjectStore,
    jobs: list[Job],
    worker_id: str,
    *,
    ocr: OcrConfig | None = None,
    asr: AsrConfig | None = None,
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
    asr = asr or AsrConfig()
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

        beat = _Throttle(lease_seconds / 4)

        def heartbeat(_done: float, _total: float, job: Job = job, beat: _Throttle = beat) -> None:
            if beat.due():  # per-segment callbacks are frequent; renew the lease only now and then
                queue.extend(job.id, worker_id, lease_seconds)

        outcome = _extract(store, source_name, key, job, ocr, asr, heartbeat)
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

    documents: list[NewDocument] = []
    spans: list[tuple[int, int]] = []  # per ready file: its slice of `documents`
    for job, title, extracted in ready:
        source_name = assets[job.asset_id][0]
        begin = len(documents)
        if extracted.units:
            documents.extend(
                NewDocument(
                    title,
                    source_name,
                    extracted.media_type,
                    unit.text,
                    lang=extracted.language or "",
                    asset_id=job.asset_id,
                    start_ms=unit.start_ms,
                    end_ms=unit.end_ms,
                    unit_kind="transcript",
                    dedupe_key=f"{job.asset_id}:{unit.start_ms}:{unit.text}".encode(),
                )
                for unit in extracted.units
            )
        else:
            documents.append(
                NewDocument(
                    title,
                    source_name,
                    extracted.media_type,
                    extracted.text,
                    lang=script_of(extracted.text),
                )
            )
        spans.append((begin, len(documents)))
    try:
        added = index.add_many_skipping_empty(documents)
    except Exception as exc:
        log.warning("indexing a batch of %d failed: %s", len(ready), exc)
        for job, _, _ in ready:
            settle_failure(job, f"{type(exc).__name__}: {exc}", retry=True)
        return result_of()
    for (job, _, extracted), (begin, end) in zip(ready, spans, strict=True):
        results = [r for r in added[begin:end] if r is not None]
        if not results:
            reason = (
                "no speech was recognised"
                if extracted.units or extracted.duration_ms
                else ("the file contains no indexable text")
            )
            settle_failure(job, reason, retry=False)
            continue
        with index.connection() as conn:
            updated = conn.execute(
                "UPDATE assets SET status = 'indexed', document_id = %s, error = NULL,"
                " ocr_confidence = %s, units = %s, duration_ms = %s, language = %s,"
                " updated_at = now() WHERE id = %s",
                (
                    results[0].document_id,
                    extracted.confidence,
                    len(results),
                    extracted.duration_ms,
                    extracted.language,
                    job.asset_id,
                ),
            )
        if updated.rowcount == 0:
            # The asset was deleted while we were working on it. Undo what we just indexed, or
            # its text would stay searchable with nothing left to point at.
            index.delete_documents([r.document_id for r in results if not r.duplicate])
            queue.complete(job.id, worker_id)
            lost += 1
            continue
        if queue.complete(job.id, worker_id):
            indexed += 1
        else:
            lost += 1
    return result_of()
