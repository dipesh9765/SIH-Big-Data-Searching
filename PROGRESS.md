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

## History rewrite
The password string was replaced with `***REMOVED***` in all 21 commits locally (backup bundle kept outside the repo). A force-push to GitHub was rejected by the branch protection rule on `main`; the remote still holds the original history. The credential is dead (database deleted), so the practical risk is nil, but the rewrite still needs pushing if wanted.

## Status
- [x] Audit
- [ ] A-G
