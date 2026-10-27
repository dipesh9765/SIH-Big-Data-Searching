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
| E Tests | 82 tests, 99% coverage (gate 90%), ruff, strict mypy |
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
`ruff check`, `ruff format --check`, strict `mypy` and `pytest --cov` (81 passed, 1 skipped, 99%) in the working virtualenv. The UI was exercised in the built-in browser against a real server with real DOCX, PDF, CSV and TXT files: search, highlighting, a stored `<script>` payload staying inert, upload, duplicate upload, rejected type, delete (statistics returned exactly to their previous values).

## Not verified / open items
- **Clean-room install** from `requirements-dev.txt` in a fresh virtualenv was started but PyPI timed out repeatedly on this machine; see the status line at the end of this file for the outcome.
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
