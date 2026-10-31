# Changelog

## 2.0.0 (unreleased)

The scale-out rebuild (branch `feature/scale-v2`). The single-node SQLite engine from 1.0.0 is unchanged and now serves as the correctness oracle.

### Added
- PostgreSQL inverted index: compressed 128-posting blocks with per-block score bounds, delta table + compactor, snapshot-consistent reads; block-max WAND, vectorised scan and a cost rule choosing between them.
- Asynchronous ingestion: S3-compatible object store (content-addressed), job queue in PostgreSQL (leases, fencing, backoff, dead letters, LISTEN/NOTIFY wake-ups), worker pools for text, OCR, speech and embeddings, streaming uploads, range-capable file serving, signed media links.
- OCR for images and scanned/mixed PDFs (Tesseract English, Hindi, Marathi); speech-to-text for audio and video with timestamped units (faster-whisper).
- Query language: phrases, AND/OR/NOT, `-term`, groups, filters; autocomplete, typo correction, "did you mean".
- Hindi/Marathi (Devanagari) tokenisation and stemming.
- Semantic and image search (pgvector, multilingual embeddings, CLIP), hybrid ranking.
- Deletion with tombstones and purge; `DOCSEARCH_API_KEY`; Prometheus `/metrics`; maintenance (job pruning, orphan collection).
- React + TypeScript web UI; API and worker Docker images; compose stack; CI for the whole system.
- ADRs 0006-0015.

### Fixed (found by running it)
- The tokenizer cut Devanagari words at every vowel sign (Python's `\w` ignores combining marks).
- Block-max pruning was slower than scanning for queries made only of common words.
- PyAV 19 broke faster-whisper; pinned below 19.
- Two processes starting together raced to create the object-store bucket and one crashed.
- Advisory locks were database-wide, so unrelated indexes blocked each other.
- Compaction slowed 4x once the delta outgrew the cache; it now runs continuously.
- A one-word PDF was mistaken for a scan and waited forever for an OCR worker.

## 1.0.0

### Added
- `docsearch` package: analyzer (Unicode tokens, stop words, Snowball stemming), SQLite-backed inverted index with BM25 ranking, snippets with highlight offsets, AND/OR matching, pagination.
- Text extraction for `.txt`, `.md`, `.csv`, `.docx`, `.pdf`, and images via optional Tesseract OCR, with size, zip-bomb and page limits.
- FastAPI service (`/v1/documents`, `/v1/search`, `/v1/stats`, `/healthz`, `/readyz`), request ids, security headers, OpenAPI docs.
- Single-page UI (no inline script, strict CSP).
- The database schema, documented in `schema.sql` and `ARCHITECTURE.md` (the original was never committed).
- 82 tests (99% coverage with the OCR packages, 98% without; the test that needs real Tesseract runs in CI), ruff, strict mypy, reproducible benchmark, Dockerfile, CI, ADRs.

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
