# Changelog

## 1.0.0

### Added
- `docsearch` package: analyzer (Unicode tokens, stop words, Snowball stemming), SQLite-backed inverted index with BM25 ranking, snippets with highlight offsets, AND/OR matching, pagination.
- Text extraction for `.txt`, `.md`, `.csv`, `.docx`, `.pdf`, and images via optional Tesseract OCR, with size, zip-bomb and page limits.
- FastAPI service (`/v1/documents`, `/v1/search`, `/v1/stats`, `/healthz`, `/readyz`), request ids, security headers, OpenAPI docs.
- Single-page UI (no inline script, strict CSP).
- The database schema, documented in `schema.sql` and `ARCHITECTURE.md` (the original was never committed).
- 82 tests (99% coverage; the one test that needs real Tesseract runs in CI), ruff, strict mypy, reproducible benchmark, Dockerfile, CI, ADRs.

### Changed
- Rebuilt from a Node/Express + Python-subprocess + MySQL prototype into a single Python service on SQLite.
- Search now works: queries and documents share one analyzer (the prototype stored unstemmed tokens and looked up the raw query).
- Configuration is environment-only; no credentials in source.

### Fixed (relative to the prototype)
- Search handler never returned results (`result_name == []` is never true).
- `database.py` used `os` without importing it; one commit per token; stop-word list rebuilt for every token.
- Uploads saved under the client-supplied file name (path traversal / overwrite); unknown extensions crashed the handler.

### Removed
- The Express/EJS app, the Python subprocess scripts, experiment files and placeholders, and the hard-coded MySQL password.
