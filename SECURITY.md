# Security

## Reporting
Open a private security advisory on the repository (Security → Report a vulnerability) rather than a public issue.

## Threat model
docsearch accepts files from clients and runs parsers, OCR and speech recognition on them. It has **one shared API key** (`DOCSEARCH_API_KEY`) and no per-user access control, rate limiting or TLS: run it on a trusted network or behind a reverse proxy that provides those, and keep `/metrics` internal.

## Controls
| Risk | Control |
|------|---------|
| Path traversal / overwrite via file name | The name is reduced to its last path component, control characters stripped, and kept only as metadata. Object keys are `assets/<2 hex>/<sha256>`: no client text ever reaches a storage path or a scratch-file name |
| Oversized uploads | `Content-Length` checked before reading, then a chunked read that stops at the limit (10 MB documents/images, 500 MB audio/video); uploads are spooled to disk, never held in memory |
| Zip bombs / huge documents | `.docx` uncompressed size, PDF page count, extracted text size, OCR image pixels (checked from the header before decoding), OCR page count, audio/video duration and a transcription deadline are all bounded |
| Unexpected file types | Extension allowlist at upload time; binary content in text files rejected; encrypted PDFs refused |
| SQL injection | Every statement is parameterised. Schema names are validated against `[a-z_][a-z0-9_]*`; LIKE wildcards in filter values are escaped |
| Stored XSS | The API returns text plus highlight offsets, never HTML; the UI renders them as text nodes (tested with an `<img onerror>` payload); strict CSP (`default-src 'self'`), no inline script or style |
| Clickjacking / sniffing | `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer` |
| Unauthenticated destructive calls | With `DOCSEARCH_API_KEY` set, every `/v1` route (uploads, deletes, search, files) requires it (constant-time compare). `/healthz`, `/readyz` and `/metrics` stay open for probes |
| Media players cannot send headers | `POST /v1/assets/{id}/file-url` (authenticated) returns an HMAC-signed link valid for 10 minutes for exactly that file; the key never appears in a URL |
| Key in the browser | Kept in `sessionStorage` (gone when the tab closes), sent as a Bearer header |
| Poisoned jobs | Permanent errors fail fast with a short message; transient ones back off; a job that keeps killing workers is dead-lettered after `max_attempts` |
| Data left after deletion | Deleted documents leave search immediately (tombstone); the purge clears their text and postings, and the stored original is deleted with the upload |
| Sensitive data in logs | Requests are logged by method, route, status and timing only; query strings and file contents are never logged |
| Secrets in source | None except clearly marked **local development defaults** (`docsearch_dev`, `deploy/s3.json`); every one is overridable by environment variable. Set real values anywhere shared |

## Known gaps
- One shared key: no users, roles, per-tenant isolation or audit log; no rate limiting or quotas; no TLS (terminate it in front).
- Parsers (`pypdf`, `python-docx`, Tesseract, FFmpeg through PyAV in faster-whisper, Pillow) process untrusted input in worker processes. A vulnerability in one is a vulnerability here; pin and update them (Dependabot is not configured for this repository) and keep workers unprivileged and resource-limited (the compose file sets memory and CPU limits; the images run as a non-root user).
- Tombstones are permanent; anyone with the key can grow them.
- `npm audit` reports 11 moderate advisories in the web build tooling (development dependencies, not shipped in the bundle); they are not addressed.
- `/docs` (Swagger UI) is served without the CSP because it needs inline script and a CDN; disable it in a hardened deployment.
- Model files are downloaded at image build time from Hugging Face; the images are not signed and the model files are not checksum-verified beyond what the libraries do.

## History note
The original prototype hard-coded a MySQL `root` password in `app.js`. That database no longer exists, so the credential is dead, but the string had been public in the git history; the history was rewritten locally to remove it. The configuration is now environment-only.
