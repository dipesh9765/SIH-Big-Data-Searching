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
from collections.abc import Sequence

from docsearch.config import ServiceSettings
from docsearch.engine.pg import PgIndex
from docsearch.ingest.objects import ObjectStore, S3Store
from docsearch.ingest.pipeline import BatchResult, process_batch
from docsearch.ingest.queue import JobQueue
from docsearch.ocr import OcrConfig, check_ready

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
    ) -> None:
        self.index = index
        self.store = store
        self.queue = JobQueue(index)
        self.worker_id = worker_id or default_worker_id()
        self.kinds = tuple(kinds)
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.ocr = ocr or OcrConfig()

    def run_once(self) -> BatchResult:
        """Claim and process one batch; an empty result means there was nothing to do."""
        jobs = self.queue.claim(
            self.worker_id, self.kinds, limit=self.batch_size, lease_seconds=self.lease_seconds
        )
        return process_batch(
            self.index,
            self.queue,
            self.store,
            jobs,
            self.worker_id,
            ocr=self.ocr,
            lease_seconds=self.lease_seconds,
        )

    def run(self, stop: threading.Event, *, poll_interval: float = 1.0) -> None:
        """Process until `stop` is set, sleeping `poll_interval` when the queue is empty."""
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
                stop.wait(poll_interval)
            else:
                log.info(
                    "batch: %d indexed, %d retried, %d failed, %d rerouted, %d lost",
                    result.indexed,
                    result.retried,
                    result.failed,
                    result.rerouted,
                    result.lost,
                )
        log.info("worker %s stopped", self.worker_id)


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
    parser.add_argument("--poll", type=float, default=1.0, help="seconds to wait on an empty queue")
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
    if "ocr" in kinds:
        try:
            check_ready(ocr)  # refuse to run, rather than fail every job, if OCR is not usable
        except RuntimeError as exc:
            log.error("cannot start an OCR worker: %s", exc)
            raise SystemExit(2) from exc
    heavy = "ocr" in kinds
    with PgIndex(settings.dsn, schema=settings.schema) as index:
        worker = Worker(
            index,
            build_store(settings),
            kinds=kinds,
            batch_size=args.batch or (1 if heavy else 16),
            lease_seconds=args.lease or (900.0 if heavy else 300.0),
            ocr=ocr,
        )
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        if args.compact_interval > 0:
            threading.Thread(
                target=run_compactor, args=(index, stop, args.compact_interval), daemon=True
            ).start()
        worker.run(stop, poll_interval=args.poll)
        time.sleep(0)  # let a final log line flush on some platforms


if __name__ == "__main__":
    main()
