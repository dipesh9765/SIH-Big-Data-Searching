# Contributing

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
make install        # pinned dev dependencies + editable install
make check          # ruff, strict mypy, pytest with the 90% coverage gate
make bench          # reproducible benchmark (see benchmarks/bench.py)
make run            # single-node SQLite server with reload on http://127.0.0.1:8000
```

The scale-out service needs PostgreSQL (with pgvector) and an S3-compatible store: `docker compose -f deploy/docker-compose.yml up -d postgres objects`. Tests that need them read `DOCSEARCH_TEST_DSN` (default `127.0.0.1:5433`) and skip cleanly when the database is absent. Each test uses a throwaway schema.

```bash
cd web && npm ci && npm run lint && npm run typecheck && npm test && npm run build
docker build -f deploy/Dockerfile.worker --target test -t docsearch-worker-test .   # OCR, Whisper, embedding models
docker run --rm -v "$PWD:/app" -e DOCSEARCH_TEST_DSN=postgresql://docsearch:docsearch_dev@host.docker.internal:5433/docsearch docsearch-worker-test pytest -q --no-cov tests
```

The OCR, speech and embedding tests need the models, which live in that image; on a bare machine they skip.

## Conventions
- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `build:`, `refactor:`).
- New behaviour comes with a test; a bug fix starts with a failing regression test.
- Any change to ranking must keep `tests/test_index.py::test_matches_a_brute_force_scorer` and the hand-computed BM25 test passing.
- Changing the analyzer or schema invalidates existing indexes: bump `schema_version` in `schema.sql` and add a migration note to the CHANGELOG.
- Significant design decisions get an ADR in `docs/adr/`.
- Anything that changes how documents are indexed (analyzer, block format, schema) needs a migration note: existing indexes are not rebuilt automatically. The PostgreSQL schema file is idempotent (`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) and runs on start.
- A change to the search engine is checked against the SQLite engine (`tests/test_engine.py`, `tests/test_pg.py`): results and scores must match bit for bit. Keep these passing.
- Measure before optimising, and record failed experiments (see "Tried and rejected" in ADR 0008).
- Do not present accuracy measured on synthetic inputs as real-world accuracy; say what the input was.
