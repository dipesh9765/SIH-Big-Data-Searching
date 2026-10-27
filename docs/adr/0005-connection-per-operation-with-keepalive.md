# ADR 0005: Connection per operation, plus one keep-alive connection

**Status:** accepted

## Context
FastAPI runs synchronous routes on a thread pool, and `sqlite3` connections should not be shared across threads casually. The simplest safe pattern is a new connection per operation.

## Measurement
Profiling 200 inserts showed 1.38 s of 3.0 s spent in `Connection.close()`: in WAL mode SQLite checkpoints and deletes the WAL file when the last connection closes, which happened after every write.

## Decision
`Index` opens a connection per operation and also holds one idle connection for its lifetime so the WAL is never torn down between operations. `Index.close()` (called on application shutdown, and by a finalizer) releases it.

## Result
On a 500-document benchmark: 66.7 → 218.8 docs/s indexing; query p50 roughly halved as well.

## Consequences
- An `Index` must be closed (context manager) before its directory is deleted on Windows.
- If write contention ever matters more than simplicity, move to one writer thread with a queue.
