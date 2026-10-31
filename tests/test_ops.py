"""Metrics, housekeeping and worker wake-ups."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator

import pytest

from docsearch.engine.pg import PgIndex
from docsearch.ingest.maintenance import collect_orphans, prune_jobs
from docsearch.ingest.objects import MemoryStore, object_key
from docsearch.ingest.pipeline import submit
from docsearch.ingest.queue import JobQueue, Wakeup
from docsearch.ingest.worker import Worker
from docsearch.metrics import Registry

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


needs_db = pytest.mark.skipif(not _reachable(), reason="no PostgreSQL at DOCSEARCH_TEST_DSN")


@pytest.fixture
def index() -> Iterator[PgIndex]:
    pg = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    try:
        yield pg
    finally:
        pg.close()
        pg.drop_schema()


# ------------------------------------------------------------------------------- registry


def test_registry_renders_counters_histograms_and_gauges() -> None:
    registry = Registry()
    registry.describe("hits_total", "counter", "Hits")
    registry.inc("hits_total", route="/a", status="200")
    registry.inc("hits_total", 2, route="/a", status="200")
    registry.inc("hits_total", route="/b", status="404")
    registry.observe("latency_seconds", 0.02, buckets=(0.01, 0.1), route="/a")
    registry.observe("latency_seconds", 0.5, buckets=(0.01, 0.1), route="/a")
    registry.gauge_source(
        lambda: [("depth", {"state": "queued"}, 7.0), ("depth", {"state": "dead"}, 0.0)]
    )
    text = registry.render()
    assert "# TYPE hits_total counter" in text
    assert 'hits_total{route="/a",status="200"} 3' in text
    assert 'hits_total{route="/b",status="404"} 1' in text
    assert 'latency_seconds_bucket{route="/a",le="0.01"} 0' in text
    assert 'latency_seconds_bucket{route="/a",le="0.1"} 1' in text
    assert 'latency_seconds_bucket{route="/a",le="+Inf"} 2' in text
    assert 'latency_seconds_count{route="/a"} 2' in text
    assert 'latency_seconds_sum{route="/a"} 0.52' in text
    assert text.count("# TYPE depth gauge") == 1
    assert 'depth{state="queued"} 7' in text


def test_label_values_are_escaped() -> None:
    registry = Registry()
    registry.inc("x_total", path='a"b\\c\nd')  # a quote, a backslash and a newline
    assert 'x_total{path="a\\"b\\\\c\\nd"} 1' in registry.render()


# ---------------------------------------------------------------------------- housekeeping


@needs_db
def test_old_finished_jobs_are_pruned_but_recent_and_pending_ones_stay(index: PgIndex) -> None:
    store = MemoryStore()
    for n in range(3):
        submit(index, store, name=f"{n}.txt", data=f"report number {n} on glaciers".encode())
    Worker(index, store, worker_id="w", batch_size=10).run_once()  # all three done
    submit(index, store, name="pending.txt", data=b"still waiting in the queue")
    assert prune_jobs(index) == 0  # nothing is old yet
    with index.connection() as conn:
        conn.execute(
            "UPDATE ingest_jobs SET updated_at = now() - interval '8 days' WHERE state = 'done'"
        )
    assert prune_jobs(index, done_after_days=7) == 3
    counts = JobQueue(index).counts()
    assert (counts["done"], counts["queued"]) == (0, 1)


@needs_db
def test_dead_jobs_are_kept_longer_than_done_ones(index: PgIndex) -> None:
    store = MemoryStore()
    submit(index, store, name="broken.pdf", data=b"%PDF-1.4 broken")
    Worker(index, store, worker_id="w").run_once()
    with index.connection() as conn:
        conn.execute("UPDATE ingest_jobs SET updated_at = now() - interval '10 days'")
    assert prune_jobs(index, done_after_days=7, dead_after_days=30) == 0
    assert prune_jobs(index, done_after_days=7, dead_after_days=5) == 1


@needs_db
def test_orphaned_objects_are_collected_only_when_old_enough(index: PgIndex) -> None:
    store = MemoryStore()
    kept = submit(index, store, name="a.txt", data=b"a file that has a database row")
    orphan_key = object_key("ab" + "1" * 62)
    store.put(orphan_key, b"uploaded, then the process crashed")
    assert kept.asset_id
    assert collect_orphans(index, store, min_age_seconds=3600) == 0  # too young: row may be coming
    assert store.exists(orphan_key)
    store._modified[orphan_key] -= 7200
    assert collect_orphans(index, store, min_age_seconds=3600) == 1
    assert not store.exists(orphan_key)
    assert len(list(store.list_objects("assets/"))) == 1  # the referenced object survives


# ------------------------------------------------------------------------------- wake-ups


@needs_db
def test_a_sleeping_worker_wakes_when_a_job_is_queued(index: PgIndex) -> None:
    store = MemoryStore()
    worker = Worker(index, store, worker_id="w")
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,), kwargs={"poll_interval": 30.0})
    thread.start()
    try:
        time.sleep(0.5)  # let it find the queue empty and go to sleep for up to 30 s
        started = time.perf_counter()
        sub = submit(index, store, name="x.txt", data=b"wake up call about volcanoes")
        deadline = time.time() + 5
        while time.time() < deadline:
            with index.connection() as conn:
                row = conn.execute(
                    "SELECT status FROM assets WHERE id = %s", (sub.asset_id,)
                ).fetchone()
            if row and row[0] == "indexed":
                break
            time.sleep(0.05)
        elapsed = time.perf_counter() - started
        assert row is not None
        assert row[0] == "indexed"
        assert elapsed < 3, f"took {elapsed:.1f}s: the worker slept through the notification"
    finally:
        stop.set()
        thread.join(timeout=35)
    assert not thread.is_alive()


@needs_db
def test_wakeup_times_out_quietly(index: PgIndex) -> None:
    wakeup = Wakeup(index)
    try:
        assert wakeup.wait(0.1) is False
    finally:
        wakeup.close()
