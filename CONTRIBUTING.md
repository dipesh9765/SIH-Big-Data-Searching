# Contributing

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
make install        # pinned dev dependencies + editable install
make check          # ruff, strict mypy, pytest with the 90% coverage gate
make bench          # reproducible benchmark (see benchmarks/bench.py)
make run            # dev server with reload on http://127.0.0.1:8000
```

OCR tests need the `tesseract` binary and `requirements-ocr.txt`; they skip cleanly without them.

## Conventions
- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `build:`, `refactor:`).
- New behaviour comes with a test; a bug fix starts with a failing regression test.
- Any change to ranking must keep `tests/test_index.py::test_matches_a_brute_force_scorer` and the hand-computed BM25 test passing.
- Changing the analyzer or schema invalidates existing indexes: bump `schema_version` in `schema.sql` and add a migration note to the CHANGELOG.
- Significant design decisions get an ADR in `docs/adr/`.
