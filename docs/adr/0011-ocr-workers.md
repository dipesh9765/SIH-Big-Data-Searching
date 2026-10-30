# 11. OCR in a separate worker pool, text layer first

Status: accepted (scale-out work, `feature/scale-v2`)

## Decision
- **Separate pool.** Images are queued as `kind='ocr'` at upload; PDFs start as `text` jobs and, if most pages have no text layer (fewer than 80% of pages with at least 25 characters), the text worker *reroutes* the job to `ocr` (`JobQueue.reroute`, fenced like every other state change, attempt counter reset, not counted as a failure). Recognition takes seconds of CPU per page, so it never shares a queue or a process with millisecond text jobs.
- **Text layer first.** In an OCR job, a PDF page that has a text layer keeps it (exact and free); only pages without one are rendered (200 dpi, longest side capped at 5000 px) and recognised. A mixed document costs only its scanned pages.
- **Tesseract through pytesseract, pages rendered with pypdfium2** (Apache-2.0/BSD; PyMuPDF was avoided because of its AGPL licence). Languages `eng+hin+mar` by default (`DOCSEARCH_OCR_LANGS`). `OMP_THREAD_LIMIT=1` in the image: scale by running more workers, not by letting Tesseract oversubscribe cores.
- **Untrusted input limits:** pixel count is checked from the image header before decoding (60 M pixels), at most 200 pages (`DOCSEARCH_OCR_MAX_PAGES`), 120 s timeout per page, extracted text capped like any other extraction. Failures (corrupt image, too large, timeout, encrypted PDF) are permanent with a readable message.
- **Long jobs keep their lease.** The page loop calls `queue.extend` after each page, so a 100-page scan is not taken over by another worker mid-way.
- **Confidence is kept.** The mean word confidence (0-100) is stored in `assets.ocr_confidence` and returned by `GET /v1/assets/{id}`, so poor scans can be found and re-processed.
- **Fail at start, not per job.** An OCR worker checks that Tesseract and every requested language pack are installed and exits with status 2 if not, instead of claiming jobs it cannot do.
- **Where it runs.** Tesseract is not installed on the development machine, so OCR runs in `deploy/Dockerfile.worker` (service `worker-ocr`, profile `ocr`). The `test` target of that Dockerfile is what the recognition tests run in.

## Measured (in the worker image: Tesseract 5, one thread, synthetic inputs)
Inputs were text *rendered by software* in clean fonts, not photographs or scans, so these show the pipeline works and what it costs, not what accuracy to expect on real documents.
- English, 2 lines at three sizes: exact text, mean confidence 95-96.
- Hindi, 2 lines (Lohit Devanagari, 72 px): all 14 words exact, confidence 94.9. Marathi, 2 lines: all 9 words exact, confidence 95.1.
- Dense A4-sized page (36 lines, 1654 px wide): 2.3 s per page; 324 of 324 keyword occurrences found.
- Image-only 5-page PDF of short pages: 0.9 s total.
End to end through real processes (HTTP service, text worker on the host, OCR worker in Docker, SeaweedFS, PostgreSQL): a PNG, a Hindi PNG, a scanned 2-page PDF and a mixed PDF all became searchable (including a Devanagari query), with confidences 94-96.

## Not measured / limits
- No real scans: skew, noise, photographs of paper, handwriting, tables, multi-column layouts, low resolution. Expect lower accuracy there; no deskew, denoise or binarisation beyond grayscale + autocontrast is done.
- No per-page language detection: every page is read with all three languages loaded, which is slower and can mis-recognise a script. Mixed Latin/Devanagari pages work with the combined model; accuracy on them was not measured.
- Throughput is one page at a time per worker (~2 s for a dense page); the pool scales by adding workers, not by parallelism inside one.
- Word positions are not kept, so OCR hits cannot be highlighted on the page image.
- Marathi has no stemmer of its own (ADR/analysis notes); OCR output is indexed with the Hindi stemmer.
