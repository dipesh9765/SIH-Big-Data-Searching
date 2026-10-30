# 9. Ingestion jobs in a PostgreSQL table

Status: accepted (scale-out work, `feature/scale-v2`)

## Context
An upload must return quickly while extraction (a large PDF can take seconds, OCR and speech later minutes) happens elsewhere. That needs a queue with retries, back-pressure, visibility and no lost work.

## Decision
A table, `ingest_jobs` (`src/docsearch/ingest/queue.py`), claimed with `SELECT ... FOR UPDATE SKIP LOCKED`.
- **Lease, not lock:** a claim sets `locked_until`. A crashed or stalled worker's job becomes claimable again when the lease expires. Delivery is therefore *at least once* and every handler is idempotent (content-addressed object keys; indexing the same text is a no-op because `content_sha256` is unique; marking an asset indexed twice writes the same row).
- **Fencing:** `complete`, `fail` and `extend` check `locked_by`, so a worker that lost its lease cannot overwrite the result of the one that took over (tested).
- **Retry vs dead-letter:** permanent errors (corrupt, encrypted, unsupported, empty, object missing) go straight to `dead` and the asset becomes `failed` with a readable message; transient errors retry with exponential backoff (5 s doubling, capped at 5 min, +-50% jitter) up to `max_attempts` (5); a job whose workers keep dying is reaped to `dead` as well.
- **Why not Redis, RabbitMQ or Kafka:** the job record must be written in the same transaction as the asset row (no dual write to reconcile), volume is modest relative to the work each job does, and one fewer system to run, secure and monitor is a real saving. Re-uploading identical bytes of a failed asset re-queues it, which is the user-facing retry.
- **Kinds:** `ingest_jobs.kind` (today only `text`) lets later worker pools (OCR, speech, embeddings) claim only what they can do, with their own concurrency.

## Verified
`tests/test_ingest.py` (real PostgreSQL): claims are exclusive under 6 concurrent claimers; ordering; lease expiry and fencing; retry with backoff then death; reaping of repeatedly lost jobs; truncated errors; a worker crash after indexing but before settling is harmless; 4 parallel workers index 80 files exactly once. `tests/test_service.py` and a run of real `uvicorn` + CLI worker processes cover the HTTP path.

## Limits
- Polling (default 1 s when idle) rather than push; `LISTEN/NOTIFY` would cut idle latency.
- Dead rows are kept for inspection and never pruned yet.
- Sustained thousands of jobs per second, or fan-out to several independent consumers, is where to move to a broker.
- No `/metrics` endpoint yet; `GET /v1/stats` exposes queue depth by state.
