"""Housekeeping that keeps tables and the object store from growing without bound.

* `prune_jobs` deletes old finished jobs (the asset row keeps the outcome; the job row is only the
  execution record) and, much later, dead ones.
* `collect_orphans` deletes objects no asset row refers to. They arise when an upload crashes
  between writing the object and inserting the row (the object is written first on purpose, see ADR
  0010). A minimum age protects an object whose row is about to be inserted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from itertools import islice

from docsearch.engine.pg import PgIndex
from docsearch.ingest.objects import ObjectStore

log = logging.getLogger("docsearch.maintenance")

ORPHAN_BATCH = 500


def prune_jobs(
    index: PgIndex, *, done_after_days: float = 7.0, dead_after_days: float = 30.0
) -> int:
    """Delete finished jobs older than `done_after_days` and dead jobs older than `dead_after_days`.
    Returns how many rows were removed."""
    with index.connection() as conn:
        cursor = conn.execute(
            "DELETE FROM ingest_jobs WHERE"
            " (state = 'done' AND updated_at < now() - make_interval(secs => %s))"
            " OR (state = 'dead' AND updated_at < now() - make_interval(secs => %s))",
            (done_after_days * 86400, dead_after_days * 86400),
        )
        return cursor.rowcount


def _batches(keys: Iterator[tuple[str, float]], size: int) -> Iterator[list[tuple[str, float]]]:
    while batch := list(islice(keys, size)):
        yield batch


def collect_orphans(index: PgIndex, store: ObjectStore, *, min_age_seconds: float = 3600.0) -> int:
    """Delete stored objects that no asset refers to and that are older than `min_age_seconds`.
    Returns how many were deleted."""
    now = time.time()
    removed = 0
    listing = ((o.key, o.modified) for o in store.list_objects("assets/"))
    for batch in _batches(listing, ORPHAN_BATCH):
        with index.connection() as conn:
            known = {
                row[0]
                for row in conn.execute(
                    "SELECT object_key FROM assets WHERE object_key = ANY(%s)",
                    ([key for key, _ in batch],),
                )
            }
        for key, modified in batch:
            if key not in known and now - modified >= min_age_seconds:
                store.delete(key)
                removed += 1
    if removed:
        log.info("deleted %d orphaned objects", removed)
    return removed
