"""Worker process: claim jobs, index them, and keep the index compacted.

    python -m docsearch.ingest.worker                       # settings from DOCSEARCH_* variables
    python -m docsearch.ingest.worker --batch 32 --compact-interval 20

Run as many as needed; they coordinate through the database only. Stops cleanly on SIGINT/SIGTERM
after the batch in hand (its jobs are settled, nothing is abandoned mid-lease).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Sequence

from docsearch import asr as asr_module
from docsearch.asr import AsrConfig
from docsearch.config import ServiceSettings
from docsearch.engine.pg import PgIndex
from docsearch.engine.vectors import VectorStore
from docsearch.ingest.embed import process_embed_jobs
from docsearch.ingest.maintenance import collect_orphans, prune_jobs
from docsearch.ingest.objects import ObjectStore, S3Store
from docsearch.ingest.pipeline import BatchResult, process_batch
from docsearch.ingest.queue import JobQueue, Wakeup
from docsearch.ocr import OcrConfig, check_ready
from docsearch.semantic import Embedder, FastEmbedder

log = logging.getLogger("docsearch.worker")


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    def __init__(  # noqa: PLR0913 - tuning knobs, all keyword-only after `store`
        self,
        index: PgIndex,
        store: ObjectStore,
        *,
        worker_id: str | None = None,
        kinds: Sequence[str] = ("text",),
        batch_size: int = 16,
        lease_seconds: float = 300.0,
        ocr: OcrConfig | None = None,
        asr: AsrConfig | None = None,
        embedder: Embedder | None = None,
        enqueue_embeddings: bool = False,
    ) -> None:
        self.index = index
        self.store = store
        self.queue = JobQueue(index)
        self.worker_id = worker_id or default_worker_id()
        self.kinds = tuple(kinds)
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.ocr = ocr or OcrConfig()
        self.asr = asr or AsrConfig()
        self.embedder = embedder
        self.enqueue_embeddings = enqueue_embeddings
        self._vectors: VectorStore | None = None

    def run_once(self) -> BatchResult:
        """Claim and process one batch; an empty result means there was nothing to do."""
        jobs = self.queue.claim(
            self.worker_id, self.kinds, limit=self.batch_size, lease_seconds=self.lease_seconds
        )
        embed_jobs = [j for j in jobs if j.kind == "embed"]
        result = process_batch(
            self.index,
            self.queue,
            self.store,
            [j for j in jobs if j.kind != "embed"],
            self.worker_id,
            ocr=self.ocr,
            asr=self.asr,
            lease_seconds=self.lease_seconds,
            embed=self.enqueue_embeddings,
        )
        if not embed_jobs:
            return result
        if self.embedder is None:
            raise RuntimeError("an embed job was claimed by a worker with no embedding model")
        if self._vectors is None:
            self._vectors = VectorStore(self.index)
        done = process_embed_jobs(
            self.index,
            self.queue,
            self.store,
            self._vectors,
            self.embedder,
            embed_jobs,
            self.worker_id,
        )
        return BatchResult(
            result.indexed + done.indexed,
            result.retried + done.retried,
            result.failed + done.failed,
            result.rerouted,
            result.lost + done.lost,
        )

    def run(self, stop: threading.Event, *, poll_interval: float = 5.0) -> None:
        """Process until `stop` is set. When the queue is empty, sleep until a job is queued
        (LISTEN/NOTIFY) or `poll_interval` seconds pass, whichever comes first."""
        wakeup = _try_wakeup(self.index)
        log.info(
            "worker %s started (kinds=%s, batch=%d)", self.worker_id, self.kinds, self.batch_size
        )
        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception:  # a database outage must not kill the worker; retry shortly
                log.exception("claim/process failed; retrying in %.0fs", poll_interval * 5)
                stop.wait(poll_interval * 5)
                continue
            if result == BatchResult():
                _sleep(stop, wakeup, poll_interval)
            else:
                log.info(
                    "batch: %d indexed, %d retried, %d failed, %d rerouted, %d lost",
                    result.indexed,
                    result.retried,
                    result.failed,
                    result.rerouted,
                    result.lost,
                )
        if wakeup is not None:
            wakeup.close()
        log.info("worker %s stopped", self.worker_id)


def _sleep(stop: threading.Event, wakeup: Wakeup | None, seconds: float) -> None:
    """Sleep until a job is announced, `seconds` pass, or `stop` is set (checked every second, so
    shutdown is prompt even though a notification wait blocks)."""
    if wakeup is None:
        stop.wait(seconds)
        return
    deadline = time.monotonic() + seconds
    while not stop.is_set() and (left := deadline - time.monotonic()) > 0:
        if wakeup.wait(min(1.0, left)):
            return


def _try_wakeup(index: PgIndex) -> Wakeup | None:
    try:
        return Wakeup(index)
    except Exception:
        log.warning("LISTEN/NOTIFY unavailable; falling back to polling", exc_info=True)
        return None


def run_maintenance(
    index: PgIndex,
    store: ObjectStore,
    stop: threading.Event,
    interval: float,
) -> None:
    """Prune old job rows and collect orphaned objects every `interval` seconds."""
    while not stop.wait(interval):
        try:
            pruned = prune_jobs(index)
            orphans = collect_orphans(index, store)
        except Exception:
            log.exception("maintenance failed; will retry")
            continue
        if pruned or orphans:
            log.info("maintenance: pruned %d jobs, deleted %d orphaned objects", pruned, orphans)


def run_compactor(index: PgIndex, stop: threading.Event, interval: float) -> None:
    """Fold the delta table into compressed blocks every `interval` seconds.

    Measured (ADR 0008): compacting continuously ran 4x faster than one big job at the end,
    because the pending postings stay small enough to remain in cache.
    """
    while not stop.wait(interval):
        try:
            result = index.compact()
        except Exception:
            log.exception("compaction failed; will retry")
            continue
        if result.postings:
            log.info("compacted %d postings across %d terms", result.postings, result.terms)


def build_store(settings: ServiceSettings) -> S3Store:
    return S3Store(
        settings.s3_endpoint,
        settings.s3_access_key,
        settings.s3_secret_key,
        settings.s3_bucket,
        secure=settings.s3_secure,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="docsearch ingestion worker")
    parser.add_argument(
        "--batch", type=int, default=None, help="jobs claimed per round (default 16; 1 for ocr)"
    )
    parser.add_argument(
        "--lease", type=float, default=None, help="job lease seconds (default 300; 900 for ocr)"
    )
    parser.add_argument("--kinds", default="text", help="comma-separated job kinds to handle")
    parser.add_argument(
        "--poll", type=float, default=5.0, help="max seconds to sleep on an empty queue"
    )
    parser.add_argument(
        "--maintenance-interval",
        type=float,
        default=0.0,
        help="also prune old jobs and delete orphaned objects every N seconds (0: off)",
    )
    parser.add_argument(
        "--compact-interval",
        type=float,
        default=0.0,
        help="also run the compactor every N seconds (0: this process does not compact)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = ServiceSettings.from_env()
    kinds = args.kinds.split(",")
    ocr = OcrConfig(languages=settings.ocr_languages, max_pages=settings.ocr_max_pages)
    asr = AsrConfig(
        model=settings.asr_model,
        model_dir=settings.asr_model_dir,
        language=settings.asr_language,
        max_minutes=settings.asr_max_minutes,
    )
    checks: dict[str, Callable[[], None]] = {
        "ocr": lambda: check_ready(ocr),
        "asr": lambda: asr_module.check_ready(asr),
    }
    for kind, check in checks.items():
        if kind in kinds:
            try:
                check()  # refuse to run, rather than fail every job, if the engine is unusable
            except RuntimeError as exc:
                log.error("cannot start a %s worker: %s", kind, exc)
                raise SystemExit(2) from exc
    embedder: Embedder | None = None
    if "embed" in kinds:
        embedder = FastEmbedder(settings.embed_cache)
        try:
            embedder.embed_texts(["ready"])  # loads the model now, so a bad install fails here
        except Exception as exc:
            log.error("cannot start an embed worker: %s", exc)
            raise SystemExit(2) from exc
    heavy = "ocr" in kinds or "asr" in kinds or "embed" in kinds
    with PgIndex(settings.dsn, schema=settings.schema) as index:
        worker = Worker(
            index,
            build_store(settings),
            kinds=kinds,
            batch_size=args.batch or (1 if heavy else 16),
            lease_seconds=args.lease or (900.0 if heavy else 300.0),
            ocr=ocr,
            asr=asr,
            embedder=embedder,
            enqueue_embeddings=settings.semantic,
        )
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        if args.maintenance_interval > 0:
            threading.Thread(
                target=run_maintenance,
                args=(index, worker.store, stop, args.maintenance_interval),
                daemon=True,
            ).start()
        if args.compact_interval > 0:
            threading.Thread(
                target=run_compactor, args=(index, stop, args.compact_interval), daemon=True
            ).start()
        worker.run(stop, poll_interval=args.poll)
        time.sleep(0)  # let a final log line flush on some platforms


if __name__ == "__main__":
    main()
