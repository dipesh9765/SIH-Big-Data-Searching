"""Job queue and ingestion pipeline against a real PostgreSQL (skipped when none is reachable)."""

from __future__ import annotations

import codecs
import os
import random
import threading
import time
import uuid
from collections.abc import Iterator

import pytest

from docsearch.engine.pg import PgIndex
from docsearch.extract import UnsupportedMediaTypeError
from docsearch.ingest.objects import MemoryStore, ObjectNotFoundError, S3Store, object_key
from docsearch.ingest.pipeline import BatchResult, process_batch, submit
from docsearch.ingest.queue import BACKOFF_CAP_SECONDS, JobQueue, backoff_seconds
from docsearch.ingest.worker import Worker
from tests.conftest import make_docx, make_pdf

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


pytestmark = pytest.mark.skipif(not _reachable(), reason="no PostgreSQL at DOCSEARCH_TEST_DSN")


@pytest.fixture
def index() -> Iterator[PgIndex]:
    pg = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    try:
        yield pg
    finally:
        pg.close()
        pg.drop_schema()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


def text_file(n: int) -> bytes:
    return f"report number {n} about solar power and wind energy".encode()


def asset(index: PgIndex, asset_id: int) -> tuple[str, int | None, str | None]:
    with index.connection() as conn:
        row = conn.execute(
            "SELECT status, document_id, error FROM assets WHERE id = %s", (asset_id,)
        ).fetchone()
    assert row is not None
    return row


def force_due(index: PgIndex) -> None:
    """Make every queued job runnable now (skips the retry backoff)."""
    with index.connection() as conn:
        conn.execute("UPDATE ingest_jobs SET run_after = now() WHERE state = 'queued'")


def expire_leases(index: PgIndex) -> None:
    with index.connection() as conn:
        conn.execute(
            "UPDATE ingest_jobs SET locked_until = now() - interval '1 second'"
            " WHERE state = 'running'"
        )


# ------------------------------------------------------------------------------ object store


def test_object_keys_are_content_addressed_and_validated() -> None:
    digest = "ab" + "0" * 62
    assert object_key(digest) == f"assets/ab/{digest}"
    for bad in ("", "xyz", "AB" + "0" * 62, "../" + "0" * 61):
        with pytest.raises(ValueError, match="SHA-256"):
            object_key(bad)


def test_memory_store_round_trip() -> None:
    store = MemoryStore()
    store.put("k", b"data")
    assert store.exists("k")
    assert store.get("k") == b"data"
    assert store.size("k") == 4
    assert store.get_range("k", 1, 2) == b"at"
    store.delete("k")
    assert not store.exists("k")
    with pytest.raises(ObjectNotFoundError):
        store.get("k")
    store.ping()


def test_s3_store_round_trip_when_a_server_is_available() -> None:
    endpoint = os.environ.get("DOCSEARCH_TEST_S3", "127.0.0.1:8333")
    try:
        s3 = S3Store(
            endpoint,
            "docsearch",
            "docsearch_dev_secret",
            "docsearch-test",
            secure=False,
        )
    except Exception:
        pytest.skip("no S3-compatible server reachable")
    key = object_key("cd" + "1" * 62)
    s3.put(key, b"hello", "text/plain")
    assert s3.exists(key)
    assert s3.get(key) == b"hello"
    assert s3.size(key) == 5
    assert s3.get_range(key, 1, 3) == b"ell"
    assert s3.get_range(key, 3, 100) == b"lo"
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "in.bin"
        source.write_bytes(b"streamed " * 1000)
        s3.put_file(key + "-f", source, "application/octet-stream")
        target = Path(scratch) / "out.bin"
        s3.download(key + "-f", target)
        assert target.read_bytes() == source.read_bytes()
        s3.delete(key + "-f")
    with pytest.raises(ObjectNotFoundError):
        s3.size(key + "-f")
    with pytest.raises(ObjectNotFoundError):
        s3.get_range(key + "-f", 0, 1)
    with pytest.raises(ObjectNotFoundError):
        s3.download(key + "-f", Path("nowhere.bin"))
    s3.delete(key)
    assert not s3.exists(key)
    with pytest.raises(ObjectNotFoundError):
        s3.get(key)
    s3.ping()


# ----------------------------------------------------------------------------------- backoff


def test_backoff_grows_exponentially_and_is_capped_with_jitter() -> None:
    rng = random.Random(1)
    first = [backoff_seconds(1, jitter=0.0, rng=rng), backoff_seconds(2, jitter=0.0, rng=rng)]
    assert first == [5.0, 10.0]
    assert backoff_seconds(30, jitter=0.0) == BACKOFF_CAP_SECONDS
    samples = [backoff_seconds(3, rng=random.Random(n)) for n in range(50)]
    assert all(10.0 <= s <= 30.0 for s in samples)
    assert len(set(samples)) > 10


# -------------------------------------------------------------------------------------- queue


def test_submit_creates_an_asset_a_job_and_stores_the_file(
    index: PgIndex, store: MemoryStore
) -> None:
    sub = submit(index, store, name="a.txt", data=text_file(1), title="First")
    assert not sub.duplicate
    assert sub.status == "queued"
    assert sub.job_id is not None
    assert asset(index, sub.asset_id)[0] == "queued"
    assert JobQueue(index).counts() == {"queued": 1, "running": 0, "done": 0, "dead": 0}


def test_claim_is_exclusive_and_ordered(index: PgIndex, store: MemoryStore) -> None:
    ids = [submit(index, store, name=f"{n}.txt", data=text_file(n)).job_id for n in range(5)]
    queue = JobQueue(index)
    first = queue.claim("w1", limit=3)
    second = queue.claim("w2", limit=3)
    assert [j.id for j in first] == ids[:3]
    assert [j.id for j in second] == ids[3:]
    assert queue.claim("w3") == []
    assert all(j.attempts == 1 for j in first + second)


def test_concurrent_claimers_never_receive_the_same_job(index: PgIndex, store: MemoryStore) -> None:
    for n in range(60):
        submit(index, store, name=f"{n}.txt", data=text_file(n))
    queue = JobQueue(index)
    claimed: list[int] = []
    guard = threading.Lock()

    def take(worker: str) -> None:
        while jobs := queue.claim(worker, limit=4):
            with guard:
                claimed.extend(j.id for j in jobs)

    threads = [threading.Thread(target=take, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 60
    assert len(set(claimed)) == 60


def test_only_the_lease_holder_can_settle_a_job(index: PgIndex, store: MemoryStore) -> None:
    submit(index, store, name="a.txt", data=text_file(1))
    queue = JobQueue(index)
    (job,) = queue.claim("w1", lease_seconds=60)
    assert not queue.complete(job.id, "intruder")
    assert queue.fail(job.id, "intruder", "boom", retry=True) == "lost"
    assert not queue.extend(job.id, "intruder", 60)
    assert queue.extend(job.id, "w1", 60)
    assert queue.complete(job.id, "w1")
    assert not queue.complete(job.id, "w1")  # already done
    assert queue.counts()["done"] == 1


def test_an_expired_lease_hands_the_job_to_another_worker_and_fences_the_first(
    index: PgIndex, store: MemoryStore
) -> None:
    submit(index, store, name="a.txt", data=text_file(1))
    queue = JobQueue(index)
    (first,) = queue.claim("slow", lease_seconds=60)
    assert queue.claim("fast") == []  # lease still valid
    expire_leases(index)
    (second,) = queue.claim("fast")
    assert second.id == first.id
    assert second.attempts == 2
    assert not queue.complete(first.id, "slow")  # the slow worker lost the job
    assert queue.complete(second.id, "fast")


def test_transient_failures_retry_with_backoff_then_die(index: PgIndex, store: MemoryStore) -> None:
    sub = submit(index, store, name="a.txt", data=text_file(1), max_attempts=3)
    queue = JobQueue(index)
    states = []
    for _ in range(3):
        force_due(index)
        (job,) = queue.claim("w1")
        states.append(queue.fail(job.id, "w1", "store timeout", retry=True))
        assert queue.claim("w1") == [] or states[-1] == "queued"  # backoff keeps it out of reach
        if states[-1] == "queued":
            with index.connection() as conn:
                (delay,) = conn.execute(
                    "SELECT extract(epoch FROM run_after - now()) FROM ingest_jobs"
                ).fetchone()  # type: ignore[misc]
            assert delay > 1
    assert states == ["queued", "queued", "dead"]
    status, _, error = asset(index, sub.asset_id)
    assert status == "failed"
    assert error == "store timeout"


def test_a_job_that_keeps_losing_its_worker_is_eventually_dead_lettered(
    index: PgIndex, store: MemoryStore
) -> None:
    sub = submit(index, store, name="a.txt", data=text_file(1), max_attempts=2)
    queue = JobQueue(index)
    for _ in range(2):
        assert queue.claim("crashy")
        expire_leases(index)
    assert queue.claim("w2") == []  # budget exhausted: reaped, not handed out again
    assert queue.counts()["dead"] == 1
    assert asset(index, sub.asset_id)[0] == "failed"


def test_stored_errors_are_truncated(index: PgIndex, store: MemoryStore) -> None:
    sub = submit(index, store, name="a.txt", data=text_file(1))
    queue = JobQueue(index)
    (job,) = queue.claim("w1")
    queue.fail(job.id, "w1", "x" * 5000, retry=False)
    assert len(asset(index, sub.asset_id)[2] or "") == 500


# ------------------------------------------------------------------------------------ submit


def test_submit_rejects_unsupported_types_before_storing_anything(
    index: PgIndex, store: MemoryStore
) -> None:
    with pytest.raises(UnsupportedMediaTypeError, match="unsupported file type"):
        submit(index, store, name="malware.exe", data=b"MZ")
    with pytest.raises(UnsupportedMediaTypeError):
        submit(index, store, name="noextension", data=b"x")
    assert JobQueue(index).counts()["queued"] == 0


def test_identical_bytes_are_one_asset_and_a_failed_asset_can_be_requeued(
    index: PgIndex, store: MemoryStore
) -> None:
    first = submit(index, store, name="a.txt", data=text_file(1))
    again = submit(index, store, name="renamed.txt", data=text_file(1))
    assert again.duplicate
    assert again.asset_id == first.asset_id
    assert again.job_id is None
    queue = JobQueue(index)
    (job,) = queue.claim("w1")
    queue.fail(job.id, "w1", "corrupt", retry=False)
    retry = submit(index, store, name="a.txt", data=text_file(1))
    assert retry.duplicate
    assert retry.job_id is not None
    assert retry.status == "queued"
    assert asset(index, first.asset_id)[2] is None  # the old error is cleared


def test_object_keys_never_contain_user_text(index: PgIndex, store: MemoryStore) -> None:
    sub = submit(index, store, name="../../etc/passwd.txt", data=text_file(9))
    with index.connection() as conn:
        (key,) = conn.execute(
            "SELECT object_key FROM assets WHERE id = %s", (sub.asset_id,)
        ).fetchone()  # type: ignore[misc]
    assert key.startswith("assets/")
    assert ".." not in key
    assert "passwd" not in key


# ---------------------------------------------------------------------------------- pipeline


def test_end_to_end_upload_to_searchable_for_every_text_format(
    index: PgIndex, store: MemoryStore
) -> None:
    files = {
        "notes.txt": b"the glacier retreat accelerated this decade",
        "table.csv": b"name,topic\nana,volcano eruption\n",
        "memo.docx": make_docx("Satellite imagery of coral bleaching"),
        "paper.pdf": make_pdf("Tectonic plates and earthquake prediction"),
    }
    subs = {name: submit(index, store, name=name, data=data) for name, data in files.items()}
    worker = Worker(index, store, worker_id="w1", batch_size=10)
    result = worker.run_once()
    assert result.indexed == 4
    assert result.failed == 0
    for sub in subs.values():
        status, document_id, error = asset(index, sub.asset_id)
        assert (status, error) == ("indexed", None)
        assert document_id is not None
    for query, name in [
        ("glacier", "notes.txt"),
        ("volcano", "table.csv"),
        ("coral bleaching", "memo.docx"),
        ("earthquake", "paper.pdf"),
    ]:
        hits = index.search(query).hits
        assert [h.document.source_name for h in hits] == [name]
    assert JobQueue(index).counts()["done"] == 4


def test_a_bad_file_fails_fast_with_a_readable_reason_without_blocking_others(
    index: PgIndex, store: MemoryStore
) -> None:
    good = submit(index, store, name="good.txt", data=b"volcanic ash cloud")
    corrupt = submit(index, store, name="broken.pdf", data=b"%PDF-1.4 definitely not a pdf")
    empty = submit(index, store, name="empty.txt", data=b"the of and")
    result = Worker(index, store, worker_id="w1").run_once()
    assert (result.indexed, result.failed) == (1, 2)
    assert asset(index, good.asset_id)[0] == "indexed"
    status, _, error = asset(index, corrupt.asset_id)
    assert status == "failed"
    assert error
    status, _, error = asset(index, empty.asset_id)
    assert (status, error) == ("failed", "the file contains no indexable text")
    assert JobQueue(index).counts()["dead"] == 2  # permanent errors are not retried


def test_a_transient_store_outage_is_retried_and_then_succeeds(index: PgIndex) -> None:
    class Flaky(MemoryStore):
        failures = 2

        def get(self, key: str) -> bytes:
            if self.failures:
                self.failures -= 1
                raise ConnectionError("object store unreachable")
            return super().get(key)

    flaky = Flaky()
    sub = submit(index, flaky, name="a.txt", data=b"aurora borealis observations")
    worker = Worker(index, flaky, worker_id="w1")
    first = worker.run_once()
    assert (first.retried, first.indexed) == (1, 0)
    assert asset(index, sub.asset_id)[0] == "queued"
    assert worker.run_once() == type(first)()  # backoff: nothing runnable yet
    force_due(index)
    assert worker.run_once().retried == 1
    force_due(index)
    assert worker.run_once().indexed == 1
    assert asset(index, sub.asset_id)[0] == "indexed"
    assert [h.document.source_name for h in index.search("aurora").hits] == ["a.txt"]


def test_a_missing_object_is_a_permanent_failure(index: PgIndex, store: MemoryStore) -> None:
    sub = submit(index, store, name="a.txt", data=b"tsunami warning systems")
    store.delete(object_key(__import__("hashlib").sha256(b"tsunami warning systems").hexdigest()))
    result = Worker(index, store, worker_id="w1").run_once()
    assert result.failed == 1
    assert asset(index, sub.asset_id)[2] == "the stored file is missing"


def test_a_database_failure_while_indexing_retries_the_whole_batch(
    index: PgIndex, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(3):
        submit(index, store, name=f"{n}.txt", data=text_file(n))

    def boom(_docs: object) -> object:
        raise RuntimeError("database went away")

    monkeypatch.setattr(index, "add_many_skipping_empty", boom)
    result = Worker(index, store, worker_id="w1").run_once()
    assert result.retried == 3
    monkeypatch.undo()
    force_due(index)
    assert Worker(index, store, worker_id="w1").run_once().indexed == 3


def test_reprocessing_after_a_crash_before_the_job_was_settled_is_harmless(
    index: PgIndex, store: MemoryStore
) -> None:
    """Indexed, then the worker died before `complete`: the retry must not duplicate anything."""
    sub = submit(index, store, name="a.txt", data=b"landslide risk mapping")
    queue = JobQueue(index)
    jobs = queue.claim("crashed")
    # Do the work but "crash" before settling: only the index and asset rows change.
    from docsearch.engine.pg import NewDocument

    index.add(NewDocument("a.txt", "a.txt", "text/plain", "landslide risk mapping"))
    expire_leases(index)
    assert jobs
    result = Worker(index, store, worker_id="rescuer").run_once()
    assert result.indexed == 1
    assert index.stats().documents == 1  # same text: stored once
    assert asset(index, sub.asset_id)[0] == "indexed"


def test_two_assets_with_the_same_text_share_one_document(
    index: PgIndex, store: MemoryStore
) -> None:
    a = submit(index, store, name="a.txt", data=b"identical words here")
    b = submit(index, store, name="b.md", data=codecs.BOM_UTF8 + b"identical words here")
    Worker(index, store, worker_id="w1").run_once()
    assert asset(index, a.asset_id)[1] == asset(index, b.asset_id)[1]
    assert index.stats().documents == 1


def test_worker_loop_stops_when_asked_and_drains_the_queue(
    index: PgIndex, store: MemoryStore
) -> None:
    for n in range(7):
        submit(index, store, name=f"{n}.txt", data=text_file(n))
    worker = Worker(index, store, worker_id="w1", batch_size=3)
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,), kwargs={"poll_interval": 0.05})
    thread.start()
    deadline = threading.Event()
    for _ in range(100):
        if JobQueue(index).counts()["done"] == 7:
            break
        deadline.wait(0.1)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert JobQueue(index).counts()["done"] == 7


def test_many_workers_in_parallel_index_everything_exactly_once(
    index: PgIndex, store: MemoryStore
) -> None:
    total = 80
    for n in range(total):
        submit(index, store, name=f"{n}.txt", data=text_file(n))
    workers = [Worker(index, store, worker_id=f"w{i}", batch_size=5) for i in range(4)]

    def drain(worker: Worker) -> None:
        for _ in range(200):  # bounded: never hang the suite
            counts = worker.queue.counts()
            if not counts["queued"] and not counts["running"]:
                return
            worker.run_once()

    threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert index.stats().documents == total
    assert JobQueue(index).counts() == {"queued": 0, "running": 0, "done": total, "dead": 0}
    with index.connection() as conn:
        (indexed,) = conn.execute("SELECT count(*) FROM assets WHERE status = 'indexed'").fetchone()  # type: ignore[misc]
    assert indexed == total


def test_process_batch_with_no_jobs_is_a_no_op(index: PgIndex, store: MemoryStore) -> None:
    result = process_batch(index, JobQueue(index), store, [], "w1")
    assert (result.indexed, result.retried, result.failed, result.lost) == (0, 0, 0, 0)


def test_background_compactor_folds_the_delta_and_stops_on_request(
    index: PgIndex, store: MemoryStore
) -> None:
    from docsearch.ingest.worker import run_compactor

    for n in range(5):
        submit(index, store, name=f"{n}.txt", data=text_file(n))
    Worker(index, store, worker_id="w1", batch_size=10).run_once()
    assert index.stats().delta_postings > 0
    stop = threading.Event()
    thread = threading.Thread(target=run_compactor, args=(index, stop, 0.05))
    thread.start()
    for _ in range(100):
        if index.stats().delta_postings == 0:
            break
        stop.wait(0.1)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    stats = index.stats()
    assert stats.delta_postings == 0
    assert stats.block_postings > 0
    assert len(index.search("solar power").hits) == 5


def test_compactor_survives_a_failing_compaction(
    index: PgIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docsearch.ingest.worker import run_compactor

    calls = []

    def flaky() -> object:
        calls.append(1)
        raise RuntimeError("database restarting")

    monkeypatch.setattr(index, "compact", flaky)
    stop = threading.Event()
    thread = threading.Thread(target=run_compactor, args=(index, stop, 0.02))
    thread.start()
    for _ in range(100):
        if len(calls) >= 3:
            break
        stop.wait(0.05)
    stop.set()
    thread.join(timeout=5)
    assert len(calls) >= 3  # kept trying after the first failure


# ------------------------------------------------------------------------------------- OCR


def ocr_worker(index: PgIndex, store: MemoryStore, **kwargs: object) -> Worker:
    return Worker(index, store, worker_id="ocr-1", kinds=("ocr",), batch_size=1, **kwargs)  # type: ignore[arg-type]


def job_kinds(index: PgIndex) -> list[str]:
    with index.connection() as conn:
        return [r[0] for r in conn.execute("SELECT kind FROM ingest_jobs ORDER BY id")]


def test_images_are_queued_for_the_ocr_pool_and_text_workers_never_take_them(
    index: PgIndex, store: MemoryStore
) -> None:
    from tests.ocr_helpers import png, render_text

    submit(index, store, name="photo.png", data=png(render_text(["hello"])))
    submit(index, store, name="notes.txt", data=b"plain text notes here")
    assert job_kinds(index) == ["ocr", "text"]
    result = Worker(index, store, worker_id="t1").run_once()
    assert result.indexed == 1  # only the text file
    assert JobQueue(index).counts()["queued"] == 1  # the image is still waiting for OCR


def test_a_scanned_pdf_is_rerouted_from_the_text_pool_to_the_ocr_pool(
    index: PgIndex, store: MemoryStore
) -> None:
    from tests.ocr_helpers import image_only_pdf, render_text

    sub = submit(index, store, name="scan.pdf", data=image_only_pdf(render_text(["scanned"])))
    assert job_kinds(index) == ["text"]
    first = Worker(index, store, worker_id="t1").run_once()
    assert (first.rerouted, first.indexed, first.failed) == (1, 0, 0)
    assert job_kinds(index) == ["ocr"]
    assert asset(index, sub.asset_id)[0] == "queued"
    with index.connection() as conn:
        (attempts,) = conn.execute("SELECT attempts FROM ingest_jobs").fetchone()  # type: ignore[misc]
    assert attempts == 0  # rerouting is not a failed attempt
    assert Worker(index, store, worker_id="t1").run_once() == type(first)()  # text pool: nothing


def test_reroute_is_fenced_like_every_other_state_change(
    index: PgIndex, store: MemoryStore
) -> None:
    submit(index, store, name="a.txt", data=text_file(1))
    queue = JobQueue(index)
    (job,) = queue.claim("w1")
    assert not queue.reroute(job.id, "intruder", "ocr")
    assert queue.reroute(job.id, "w1", "ocr")
    assert not queue.reroute(job.id, "w1", "ocr")  # no longer running


def test_long_ocr_jobs_renew_their_lease_page_by_page(
    index: PgIndex, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docsearch.ocr import OcrResult
    from tests.ocr_helpers import image_only_pdf, render_text

    submit(index, store, name="big.pdf", data=image_only_pdf(render_text(["x"])))
    Worker(index, store, worker_id="t1").run_once()  # reroute to ocr
    extensions = []
    queue = JobQueue(index)
    original = queue.extend

    def spy(job_id: int, worker_id: str, lease: float) -> bool:
        extensions.append(lease)
        return original(job_id, worker_id, lease)

    def fake_pdf(_data: bytes, _config: object = None, on_page: object = None) -> OcrResult:
        for page in range(1, 4):
            time.sleep(0.03)  # longer than the renewal interval (lease / 4)
            on_page(page, 3)  # type: ignore[operator]
        return OcrResult("recognised words from three pages", 88.0, 3, 3)

    monkeypatch.setattr("docsearch.ingest.pipeline.ocr_pdf", fake_pdf)
    worker = ocr_worker(index, store, lease_seconds=0.08)  # renew at most every 0.02 s
    monkeypatch.setattr(worker.queue, "extend", spy)
    result = worker.run_once()
    assert result.indexed == 1
    assert len(extensions) >= 2  # renewed while the pages were being processed...
    assert set(extensions) == {0.08}  # ...for a full lease each time
    with index.connection() as conn:
        (confidence,) = conn.execute("SELECT ocr_confidence FROM assets").fetchone()  # type: ignore[misc]
    assert confidence == pytest.approx(88.0)


def test_ocr_pool_rejects_types_it_cannot_read(index: PgIndex, store: MemoryStore) -> None:
    sub = submit(index, store, name="notes.txt", data=text_file(5))
    JobQueue(index).claim("x")  # take it away from the text pool, then misroute it
    with index.connection() as conn:
        conn.execute("UPDATE ingest_jobs SET state = 'queued', kind = 'ocr', locked_by = NULL")
    result = ocr_worker(index, store).run_once()
    assert result.failed == 1
    assert asset(index, sub.asset_id)[2] == "OCR handles images and PDFs only"


def test_a_corrupt_image_fails_permanently_with_a_readable_reason(
    index: PgIndex, store: MemoryStore
) -> None:
    sub = submit(index, store, name="broken.png", data=b"\x89PNG not really")
    result = ocr_worker(index, store).run_once()
    assert result.failed == 1
    assert asset(index, sub.asset_id)[2] == "not a readable image"


def test_ocr_end_to_end_makes_scanned_content_searchable(
    index: PgIndex, store: MemoryStore
) -> None:
    from docsearch.ocr import tesseract_available
    from tests.ocr_helpers import image_only_pdf, png, render_text

    if not tesseract_available():
        pytest.skip("tesseract not installed")
    photo = submit(
        index, store, name="sign.png", data=png(render_text(["Glacier monitoring station"]))
    )
    scan = submit(
        index,
        store,
        name="report.pdf",
        data=image_only_pdf(render_text(["Volcano eruption report"])),
    )
    Worker(index, store, worker_id="t1").run_once()  # the PDF moves to the OCR pool
    worker = ocr_worker(index, store)
    for _ in range(20):
        if worker.run_once() == BatchResult():
            break
    else:
        raise AssertionError("the OCR queue did not drain")
    for sub in (photo, scan):
        status, document_id, error = asset(index, sub.asset_id)
        assert (status, error) == ("indexed", None)
        assert document_id is not None
    assert [h.document.source_name for h in index.search("glacier").hits] == ["sign.png"]
    assert [h.document.source_name for h in index.search("volcano").hits] == ["report.pdf"]
    with index.connection() as conn:
        rows = conn.execute("SELECT ocr_confidence FROM assets ORDER BY id").fetchall()
    assert all(r[0] is not None and 40 <= r[0] <= 100 for r in rows)


def test_starting_two_processes_at_once_does_not_fail_on_the_bucket_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any
    from unittest.mock import MagicMock

    from minio.error import S3Error

    def failing(code: str) -> type:
        class Client:
            def __init__(self, *_a: object, **_k: object) -> None:
                pass

            def bucket_exists(self, _bucket: str) -> bool:
                return False  # looked absent...

            def make_bucket(self, _bucket: str) -> None:
                response: Any = MagicMock()
                raise S3Error(response, code, "x", "/b", "req", "host")  # ...then lost the race

        return Client

    monkeypatch.setattr("docsearch.ingest.objects.Minio", failing("BucketAlreadyOwnedByYou"))
    S3Store("x:1", "k", "s", "bucket")  # losing the race is success: must not raise
    monkeypatch.setattr("docsearch.ingest.objects.Minio", failing("AccessDenied"))
    with pytest.raises(S3Error):
        S3Store("x:1", "k", "s", "bucket")  # other failures still surface
