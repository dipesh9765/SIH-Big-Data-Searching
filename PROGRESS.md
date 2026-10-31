# Progress log

Branch: `overhaul/v1`. Baseline at the start: a Node/Express + EJS prototype that spawned Python scripts to extract text and write a hand-rolled inverted index into MySQL. 21 commits, a 57 MB `.git` (mostly deleted experiment directories), 161 KB of live files.

## Audit (every finding below was read from the code)

### Search never worked
| Finding | Where |
|---------|-------|
| The search handler pushes results only inside `if (result_name == [])`, which is never true (array identity), then renders `index` with no data | `app.js` `app.post("/")` |
| Query term is looked up verbatim, but the indexer stores stop-word-filtered, **unstemmed** tokens (the stemmed list is computed and then discarded) | `invertedindex.py` |
| Postings are a JSON array in one MySQL column, mutated with `JSON_ARRAY_APPEND` on every token occurrence: one commit per token, no term frequency, no ranking | `invertedindex.py` |
| `database.py` calls `os.getenv` without importing `os`, and `invertedindex.func` is called with a cursor it never created in some paths | `database.py` |
| `stopwords.words()` (all languages) is rebuilt **for every token** | `invertedindex.py` |
| Upload path result is rendered before the DB insert finishes; an insert error is `throw`n inside a callback and kills the process | `app.js` |

### Security
- A MySQL `root` password was hardcoded in `app.js` and is in git history (the database no longer exists, so the credential is dead; history was rewritten locally, see below).
- Uploads were saved under the client-supplied filename: path traversal and overwrite.
- No size limit, no type allowlist; unknown extensions crashed the handler (`python` undefined).
- SQL was parameterised (good), but file text was passed to a subprocess as a command-line argument (length limit, process-list exposure).

### Missing / hygiene
- No schema anywhere in the repo (reconstructed from the SQL: `files(id, file_src, file_text)` and `inverted_index(word_name, word_id JSON)`).
- No tests, CI, Docker, config, README; placeholder files (`try.txt` x5, `Image.txt`, `(15).png.txt`, `Python-Remove-Watermark`), `package.json` named `imageteseract`.

## Plan
Rebuild as **docsearch**: a small full-text search engine with a real ranking model.

| Step | Deliverable |
|------|-------------|
| A | `src/docsearch` package: text extraction (txt/md/csv/docx/pdf, optional OCR), analyzer (tokenise, stop words, stem), SQLite-backed inverted index with BM25, snippets |
| B | Schema in `schema.sql` (documents, terms, postings, stats) with migrations table; ERD in docs |
| C | FastAPI service: upload, list, delete, search, health; limits and allowlist; no raw-file storage |
| D | One-page UI |
| E | Tests (BM25 hand-checked, persistence, concurrency, API, extraction), ruff, strict mypy, coverage gate |
| F | Benchmark harness on a synthetic Zipf corpus: indexing throughput, query p50/p95, plus relevance sanity set |
| G | Dockerfile, CI, README, ARCHITECTURE, ADRs, SECURITY, CONTRIBUTING, CHANGELOG, LICENSE |

## Delivered

| Step | Result |
|------|--------|
| A Core | `analysis` (shared analyzer with offsets), `extract` (txt, md, csv, docx, pdf, optional OCR; size, zip-bomb, page and text caps), `index` (SQLite inverted index, BM25, snippets, transactional writes) |
| B Schema | `schema.sql` with documented tables, constraints and a `postings_by_doc` index; ERD in ARCHITECTURE.md |
| C API | FastAPI: upload, list, get, delete, search, stats, health/ready; limits; request ids; security headers; no query logging |
| D UI | One page, DOM-built results, no inline script, strict CSP |
| E Tests | 82 tests, 99% coverage with the OCR packages installed, 98% without (gate 90%), ruff, strict mypy |
| F Benchmark | `benchmarks/bench.py`, seeded Zipf corpus |
| G Build and docs | Dockerfile, Makefile, CI, README, ARCHITECTURE, 5 ADRs, SECURITY, CONTRIBUTING, CHANGELOG, LICENSE |

### Found while building
- Test: a newline survived file-name sanitisation (log-injection vector). Fixed with a stricter control-character pattern.
- Profile: 45% of indexing time was `Connection.close()` (SQLite deletes the WAL when the last connection closes). A keep-alive connection took 500-doc indexing from 66.7 to 218.8 docs/s (ADR 0005).
- Benchmark: the first known-item run reported 95% recall. Cause: the harness re-fed *stemmed* terms as the query and stemming is not idempotent (`roled` -> `role`). The engine was right; the harness now queries with original words and reports 100%. A first version of the harness also had an O(V) lookup per term that made it hang.
- UI (driven in the browser): the "Indexed ..." / "already indexed" message was overwritten by the accepted-types hint. Fixed with a separate element.
- Environment: starlette's TestClient warns that `httpx2` is preferred; the pinned `httpx` still works, so that single warning is filtered with a comment.

### Results (5,000 docs x 200 tokens, 19,971 terms, 701,969 postings, 26.7 MB)
| Measure | Result |
|---------|--------|
| Indexing | 80 docs/s |
| Two rare terms (AND) | p50 1.4 ms, p95 1.7 ms |
| Common + rare (AND) | p50 24 ms, p95 29 ms |
| Three common (OR) | p50 65 ms, p95 81 ms |
| Known-item recall@1 | 100% of 200 |

## Verified
`ruff check`, `ruff format --check`, strict `mypy` and `pytest --cov` (81 passed, 1 skipped, 99%) in the working virtualenv, and again in a **clean-room virtualenv built only from `requirements-dev.txt`** (no OCR packages): ruff, format check, strict mypy, 80 passed, 2 OCR tests skipped cleanly, 97.9% coverage. The UI was exercised in the built-in browser against a real server with real DOCX, PDF, CSV and TXT files: search, highlighting, a stored `<script>` payload staying inert, upload, duplicate upload, rejected type, delete (statistics returned exactly to their previous values).

## Not verified / open items
- **Real Tesseract OCR** never ran locally (the binary is not installed; the PATH entry is stale). The OCR path is covered with a stubbed engine and real Pillow; the real-engine test runs in CI where `tesseract-ocr` is installed.
- **Docker image never built** (no Docker here); CI builds and smoke-tests it.
- No screenshots (the browser pane's screenshot call times out here).
- Benchmark numbers are from one Windows laptop and a synthetic corpus: they show scaling behaviour, not real-world relevance.
- The server has no authentication or rate limiting.
- LICENSE is MIT (the original repository had none).

## History rewrite and the leaked password
The MySQL `root` password in `app.js` appeared in 10 commits. The database no longer exists, so the credential is dead. History was rewritten locally with `git filter-repo` (string replaced in all 21 commits; a backup bundle is at `../SIH-Big-Data-Searching.pre-rewrite.bundle`, outside the repo, and contains the **original** history, so keep it private or delete it). A force-push to `origin/main` was rejected by GitHub's branch protection ("Cannot force-push to this branch"), so **GitHub still holds the original history**, including pull request #1's ref (`refs/pull/1/head`), which a force-push could not remove anyway. Options: temporarily allow force-push on `main`, or delete and recreate the repository.

## Status
- [x] Audit
- [x] A-G
- [ ] Pushed (blocked, see above)


## Scale-out rebuild (branch `feature/scale-v2`, 2026-10-29 to 2026-11-01)

Goal: millions of documents of many media types, with the inverted index in a database. Stack: PostgreSQL 17 (+ pgvector, pg_trgm) for metadata, own compressed postings and the job queue; an S3-compatible store for originals; FastAPI + worker processes; React UI. MongoDB was rejected (ADR 0008).

### Delivered (all committed locally, nothing pushed)

| Area | What | ADR |
|---|---|---|
| Index engine | delta + varint postings blocks with score bounds; block-max WAND, vectorised scan, cost rule; differential tests vs the SQLite engine | 0006 |
| PostgreSQL | delta table + compactor, snapshot reads, pool, per-schema advisory locks | 0008 |
| Ingestion | content-addressed object store, Postgres job queue (leases, fencing, backoff, dead letters, NOTIFY), streaming uploads, range file serving, signed media links, housekeeping | 0009, 0010 |
| OCR | Tesseract eng+hin+mar, scanned/mixed PDFs, rerouting from the text pool, confidence | 0011 |
| Speech | faster-whisper `small`, timestamped units, hits that play from the moment | 0012 |
| Deletion | tombstones + purge, asset deletion, API-key auth | 0013 |
| Queries | phrases, AND/OR/NOT, filters (exact set algebra), autocomplete, typo correction, did-you-mean, Devanagari analysis | 0007, 0014 |
| Semantic | multilingual passage embeddings, CLIP image search, hybrid RRF, opt-in | 0015 |
| UI and ops | React + TS UI, `/metrics`, compose stack, API and worker images, CI workflow, smoke script | - |

### Verified
- **Host:** 374 tests passed, 21 skipped (the ones needing the models), 91.5% coverage against a 90% gate; ruff and strict mypy clean; PostgreSQL-backed tests run against the compose database.
- **Container (real models):** the whole suite inside the worker image with real Tesseract, faster-whisper `small` and the multilingual/CLIP models: 394 passed, 1 skipped (the S3 test cannot reach the host store from the container).
- **Web:** Vitest (36 tests), strict TypeScript, ESLint, production build 234 KB (74 KB gzip); driven in a real browser against the live service (autocomplete, highlighted hits, Hindi inflection, phrase, OR, NOT, filters, did-you-mean, library).
- **Whole stack:** `docker compose --profile app up` then `deploy/smoke.sh` (upload English and Hindi text, index, search) and OCR of images and a scanned PDF through the containerised workers.
- **Benchmark:** 300,000 synthetic documents against Postgres full-text search (README).
- **Bugs found only by running things**, all fixed and recorded: PyAV 19 vs faster-whisper; bucket-creation race between processes; database-wide advisory locks; pruning slower than scanning; compaction slowdown once the backlog outgrew cache; Devanagari words cut at vowel signs; a short PDF mistaken for a scan; an unbounded test loop that hung the suite.

### Not verified / open
- **CI has never run on GitHub.** The workflow (including the `containers` job that builds the 4 GB test image and the `stack` job) was written and its pieces were run locally, not as a whole.
- **No 1,000,000-document run**: this machine has 7.7 GB of RAM (Docker gets about 4 GB). Latency for common-term queries is linear in postings (about 160-230 ms at 300k).
- **Accuracy numbers are from synthetic inputs**: rendered text for OCR (95%+ confidence, clean fonts), eSpeak speech for Whisper, 8 passages and 5 pictures for the embeddings. Real scans, natural speech and a real relevance judgement set were not available. **Hindi and Marathi speech recognition was poor** (language detection fails; forced language gives partly right Devanagari).
- Marathi has no stemmer of its own (Hindi rules are applied).
- Phrase search verifies candidates against text (capped at 1,000); there is no positional index.
- Embedding backfill for existing documents has no command; video is searched through its audio only.
- Single shared API key; no per-user access, rate limiting or TLS. Tombstones are never removed.
- Sharding for 100M documents is designed in ARCHITECTURE.md, not implemented.
- The SIH GitHub repository still holds the old history with the dead MySQL password (a force-push was refused by branch protection); that needs the owner.
- `npm audit`: 11 moderate advisories in development tooling, not addressed.
