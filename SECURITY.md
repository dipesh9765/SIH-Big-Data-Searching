# Security

## Reporting
Open a private security advisory on the repository (Security → Report a vulnerability) rather than a public issue.

## Threat model
docsearch accepts files from clients and parses them. It has **no authentication**: run it behind a reverse proxy or on a trusted network, or add auth in front of it. Do not expose it directly to the internet.

## Controls
| Risk | Control |
|------|---------|
| Path traversal / overwrite via file name | Uploads are processed in memory; the name is reduced to its last path component, control characters stripped, and used only as metadata (tests: `test_clean_filename_never_yields_a_path`) |
| Oversized uploads | `Content-Length` checked before reading, then a bounded read; default 10 MB (`DOCSEARCH_MAX_UPLOAD_MB`) |
| Zip bombs / huge documents | `.docx` total uncompressed size ≤ 50 MB, PDF ≤ 2,000 pages, extracted text ≤ 5 M characters |
| Unexpected file types | Extension allowlist; binary content in text files rejected; encrypted PDFs refused |
| SQL injection | Every statement is parameterised; the only dynamic SQL builds `?,?,?` placeholder lists |
| Stored XSS | The API returns plain text plus highlight offsets, never HTML; the UI builds nodes with `textContent`; strict CSP (`default-src 'self'`) with no inline script |
| Clickjacking / sniffing | `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer` |
| Sensitive data in logs | Requests are logged by method, path, status and timing only: query strings (what people search for) and file contents are never logged |
| Secrets in source | None. Configuration comes from environment variables (`.env.example`); `.env` is git-ignored |

## Known gaps
- No authentication, authorisation, rate limiting or per-user quotas.
- Parsers (`pypdf`, `python-docx`, Tesseract) process untrusted input in-process; a parser vulnerability is a vulnerability here. Pin and update them, and run the container as non-root (the Dockerfile does).
- Uploaded text is stored as received; anything displayed elsewhere must be escaped by that consumer.
- `/docs` (Swagger UI) is served without the CSP because it needs inline script and a CDN; disable it in a hardened deployment.

## History note
The original prototype hard-coded a MySQL `root` password in `app.js`. That database no longer exists, so the credential is dead, but the string had been public in the git history; the history was rewritten locally to remove it. The configuration is now environment-only.
