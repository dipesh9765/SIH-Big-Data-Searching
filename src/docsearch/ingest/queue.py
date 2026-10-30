"""A job queue in PostgreSQL: `FOR UPDATE SKIP LOCKED`, leases, retries with backoff, dead letters.

Why a table and not Redis/Kafka (ADR 0009): the work already has to be recorded transactionally
next to the asset it concerns, volumes are modest (a few hundred jobs per second at most, each
doing much more work than the queue operation), and one less system to run is a real saving. The
limits are stated there: at sustained thousands of jobs per second, or fan-out to many consumer
groups, move to a broker.

Delivery is *at least once*. A worker claims a job with a lease (`locked_until`). If it crashes or
stalls, the lease expires and another worker claims the job again, so handlers must be idempotent.
Every state change checks `locked_by`, so a worker that lost its lease cannot overwrite the result
of the worker that took over (a "fencing" check).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from docsearch.engine.pg import PgIndex

BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 300.0
ERROR_CHARS = 500  # stored errors are truncated: they are shown to users and must stay small


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    asset_id: int
    kind: str
    attempts: int  # including this one
    max_attempts: int


def backoff_seconds(
    attempts: int, *, jitter: float = 0.5, rng: random.Random | None = None
) -> float:
    """Delay before retry number `attempts`: 5 s, 10 s, 20 s ... capped at 5 min, +-50% jitter.

    Jitter spreads retries out so a flaky dependency is not hit by every failed job at once.
    """
    base = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(0, attempts - 1))
    spread = (rng or random).uniform(1 - jitter, 1 + jitter)
    return float(base * spread)


class JobQueue:
    def __init__(self, index: PgIndex) -> None:
        self._index = index

    def claim(
        self,
        worker_id: str,
        kinds: Sequence[str] = ("text",),
        *,
        limit: int = 1,
        lease_seconds: float = 300.0,
    ) -> list[Job]:
        """Claim up to `limit` runnable jobs: queued and due, or running with an expired lease."""
        with self._index.connection() as conn:
            conn.execute(
                "WITH dead AS ("
                " UPDATE ingest_jobs SET state = 'dead', locked_by = NULL, updated_at = now(),"
                "  last_error = 'worker lost the job too many times'"
                " WHERE state = 'running' AND locked_until < now() AND attempts >= max_attempts"
                " RETURNING asset_id)"
                " UPDATE assets SET status = 'failed', updated_at = now(),"
                "  error = 'processing did not finish'"
                " FROM dead WHERE assets.id = dead.asset_id"
            )
            rows = conn.execute(
                "WITH next AS ("
                " SELECT id FROM ingest_jobs WHERE kind = ANY(%s) AND ("
                "  (state = 'queued' AND run_after <= now())"
                "  OR (state = 'running' AND locked_until < now() AND attempts < max_attempts))"
                " ORDER BY run_after, id LIMIT %s FOR UPDATE SKIP LOCKED)"
                " UPDATE ingest_jobs j SET state = 'running', attempts = j.attempts + 1,"
                "  locked_by = %s, locked_until = now() + make_interval(secs => %s),"
                "  updated_at = now()"
                " FROM next WHERE j.id = next.id"
                " RETURNING j.id, j.asset_id, j.kind, j.attempts, j.max_attempts",
                (list(kinds), limit, worker_id, lease_seconds),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE assets SET status = 'processing', updated_at = now()"
                    " WHERE id = ANY(%s) AND status = 'queued'",
                    ([r[1] for r in rows],),
                )
        return [Job(*row) for row in rows]

    def complete(self, job_id: int, worker_id: str) -> bool:
        """Mark done. False if this worker no longer owns the job (its lease expired)."""
        with self._index.connection() as conn:
            cursor = conn.execute(
                "UPDATE ingest_jobs SET state = 'done', locked_by = NULL, locked_until = NULL,"
                " updated_at = now() WHERE id = %s AND locked_by = %s AND state = 'running'",
                (job_id, worker_id),
            )
            return cursor.rowcount == 1

    def fail(
        self,
        job_id: int,
        worker_id: str,
        error: str,
        *,
        retry: bool,
        rng: random.Random | None = None,
    ) -> str:
        """Record a failure. Returns the new state: 'queued' (will retry), 'dead' or 'lost'.

        A non-retryable error (the file is corrupt; trying again cannot help) or an exhausted
        attempt budget moves the job to `dead` and marks its asset failed with the message.
        """
        message = error[:ERROR_CHARS]
        with self._index.connection() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts, asset_id FROM ingest_jobs"
                " WHERE id = %s AND locked_by = %s AND state = 'running' FOR UPDATE",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                return "lost"
            attempts, max_attempts, asset_id = row
            if retry and attempts < max_attempts:
                conn.execute(
                    "UPDATE ingest_jobs SET state = 'queued', locked_by = NULL,"
                    " locked_until = NULL,"
                    " run_after = now() + make_interval(secs => %s), last_error = %s,"
                    " updated_at = now() WHERE id = %s",
                    (backoff_seconds(attempts, rng=rng), message, job_id),
                )
                conn.execute(
                    "UPDATE assets SET status = 'queued', updated_at = now() WHERE id = %s",
                    (asset_id,),
                )
                return "queued"
            conn.execute(
                "UPDATE ingest_jobs SET state = 'dead', locked_by = NULL, locked_until = NULL,"
                " last_error = %s, updated_at = now() WHERE id = %s",
                (message, job_id),
            )
            conn.execute(
                "UPDATE assets SET status = 'failed', error = %s, updated_at = now() WHERE id = %s",
                (message, asset_id),
            )
            return "dead"

    def reroute(self, job_id: int, worker_id: str, kind: str) -> bool:
        """Hand a running job to another worker pool (text -> ocr). Not a failure: the attempt
        counter starts over for the new pool. False if this worker no longer owns the job."""
        with self._index.connection() as conn:
            row = conn.execute(
                "UPDATE ingest_jobs SET kind = %s, state = 'queued', attempts = 0,"
                " locked_by = NULL, locked_until = NULL, run_after = now(), updated_at = now()"
                " WHERE id = %s AND locked_by = %s AND state = 'running' RETURNING asset_id",
                (kind, job_id, worker_id),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE assets SET status = 'queued', updated_at = now() WHERE id = %s", (row[0],)
            )
            return True

    def extend(self, job_id: int, worker_id: str, lease_seconds: float) -> bool:
        """Push the lease out for a long-running job. False if the lease was already lost."""
        with self._index.connection() as conn:
            cursor = conn.execute(
                "UPDATE ingest_jobs SET locked_until = now() + make_interval(secs => %s)"
                " WHERE id = %s AND locked_by = %s AND state = 'running'",
                (lease_seconds, job_id, worker_id),
            )
            return cursor.rowcount == 1

    def counts(self) -> dict[str, int]:
        """Jobs per state (zero for states with none)."""
        with self._index.connection() as conn:
            found: dict[str, int] = dict(
                conn.execute("SELECT state, count(*) FROM ingest_jobs GROUP BY state")
            )
        return {state: int(found.get(state, 0)) for state in ("queued", "running", "done", "dead")}
