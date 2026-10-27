# ADR 0004: Keep extracted text, not the uploaded file

**Status:** accepted

## Context
The prototype saved every upload to `public/uploads/<client-supplied name>` and served that directory statically: a path-traversal and overwrite risk, an unbounded disk growth path, and a way to host arbitrary files.

## Decision
Extraction happens in memory from the uploaded bytes. The client's file name is sanitised and stored only as display metadata (`source_name`); it is never used as a path. Only the extracted text, title and statistics are persisted. Uploads are capped (default 10 MB), types are an allowlist by extension, text is capped at 5 M characters, `.docx` uncompressed size and PDF page count are bounded, and identical text is stored once (SHA-256).

## Consequences
- Nothing a user uploads can be fetched back or executed from the server.
- The original document cannot be re-downloaded or re-extracted with a better parser later; a deployment that needs that should add object storage and keep only a reference here.
- Parser bugs are the remaining risk surface (see SECURITY.md).
