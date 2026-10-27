# ADR 0001: SQLite for storage

**Status:** accepted

## Context
The prototype used a MySQL server for two tables, which meant every developer, CI run and demo needed a database server, a password and a schema that was never committed (the original database no longer exists). The workload is a single-writer index with read-heavy queries.

## Decision
Store the index in a single SQLite database file in WAL mode, applying `schema.sql` automatically on first open.

## Consequences
- Zero setup: `pip install` and run. Tests and the benchmark create throw-away databases in milliseconds.
- Transactions give atomic index updates for free (a document's postings, `doc_freq` and corpus counters commit together).
- One writer at a time. Concurrent uploads queue on the busy timeout; reads continue during writes (WAL). Acceptable up to roughly the low millions of small documents; beyond that, shard or move to a server engine (ARCHITECTURE.md, "Limits").
- Backup is a file copy (use `sqlite3 .backup` while running).

## Alternatives considered
- **MySQL/PostgreSQL:** better multi-writer story, but adds an operational dependency this project does not need.
- **Elasticsearch/OpenSearch:** the right production answer for large corpora, but it hides the thing this project exists to demonstrate and is heavy for a demo.
