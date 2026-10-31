"""Embedding jobs: compute and store the vectors that semantic and image search use.

One `embed` job per upload, queued after the upload is indexed (when semantic search is enabled).
It embeds the passages of every document the upload produced and, for images, the picture itself.
The job is separate from indexing on purpose: a failed or slow embedding must never make a
searchable document unsearchable, and an embed job that dies leaves the asset's status alone
(see `JobQueue.fail`). Everything is idempotent, so a re-run only fills what is missing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from docsearch.engine.pg import PgIndex
from docsearch.engine.vectors import VectorStore
from docsearch.extract import IMAGE_MEDIA_TYPES, ExtractionError
from docsearch.ingest.objects import ObjectNotFoundError, ObjectStore
from docsearch.ingest.pipeline import BatchResult
from docsearch.ingest.queue import Job, JobQueue
from docsearch.semantic import Embedder, chunk_text

log = logging.getLogger("docsearch.embed")

PASSAGES_PER_CALL = 128  # passages sent to the model together


def _embed_documents(
    index: PgIndex, vectors: VectorStore, embedder: Embedder, asset_id: int
) -> int:
    with index.connection() as conn:
        rows = conn.execute(
            "SELECT id, content FROM documents WHERE deleted_at IS NULL AND content <> ''"
            " AND (asset_id = %s OR id = (SELECT document_id FROM assets WHERE id = %s))",
            (asset_id, asset_id),
        ).fetchall()
    done = vectors.embedded_documents([r[0] for r in rows])
    todo = [(doc_id, chunk_text(text)) for doc_id, text in rows if doc_id not in done]
    count = 0
    pending: list[tuple[int, list[str]]] = []
    size = 0

    def flush() -> None:
        nonlocal pending, size, count
        if not pending:
            return
        flat = [chunk for _, chunks in pending for chunk in chunks]
        matrix = embedder.embed_texts(flat)
        cursor = 0
        for doc_id, chunks in pending:
            vectors.add_text(doc_id, matrix[cursor : cursor + len(chunks)])
            cursor += len(chunks)
            count += 1
        pending, size = [], 0

    for doc_id, chunks in todo:
        if not chunks:
            continue
        pending.append((doc_id, chunks))
        size += len(chunks)
        if size >= PASSAGES_PER_CALL:
            flush()
    flush()
    return count


def embed_asset(
    index: PgIndex, store: ObjectStore, vectors: VectorStore, embedder: Embedder, asset_id: int
) -> None:
    """Fill in whatever embeddings this upload is missing. Raises `ExtractionError` for an image
    that cannot be decoded; a deleted upload is a no-op."""
    with index.connection() as conn:
        row = conn.execute(
            "SELECT source_name, object_key FROM assets WHERE id = %s", (asset_id,)
        ).fetchone()
    if row is None:
        return
    source_name, key = row
    _embed_documents(index, vectors, embedder, asset_id)
    if Path(source_name).suffix.lower() in IMAGE_MEDIA_TYPES and not vectors.has_image(asset_id):
        try:
            matrix = embedder.embed_images([store.get(key)])
        except (OSError, ValueError) as exc:  # PIL: unidentified or truncated image
            raise ExtractionError("not a readable image") from exc
        vectors.add_image(asset_id, matrix[0])


def process_embed_jobs(  # noqa: PLR0913, PLR0917 - the collaborators are all needed
    index: PgIndex,
    queue: JobQueue,
    store: ObjectStore,
    vectors: VectorStore,
    embedder: Embedder,
    jobs: list[Job],
    worker_id: str,
) -> BatchResult:
    indexed = retried = failed = lost = 0
    for job in jobs:
        try:
            embed_asset(index, store, vectors, embedder, job.asset_id)
        except (ExtractionError, ObjectNotFoundError) as exc:
            outcome = queue.fail(job.id, worker_id, str(exc) or "missing file", retry=False)
        except Exception as exc:
            log.warning("embed job %d attempt %d failed: %s", job.id, job.attempts, exc)
            outcome = queue.fail(job.id, worker_id, f"{type(exc).__name__}: {exc}", retry=True)
        else:
            if queue.complete(job.id, worker_id):
                indexed += 1
            else:
                lost += 1
            continue
        if outcome == "queued":
            retried += 1
        elif outcome == "dead":
            failed += 1
        else:
            lost += 1
    return BatchResult(indexed, retried, failed, 0, lost)
