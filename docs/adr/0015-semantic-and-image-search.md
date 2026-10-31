# 15. Semantic and image search: pgvector, multilingual passages, CLIP, rank fusion

Status: accepted (scale-out work, `feature/scale-v2`)

## Decision
- **Store vectors where the rest of the data is.** pgvector (HNSW, cosine distance) in the same PostgreSQL: `text_embeddings(doc_id, chunk_no, vector(384))` and `image_embeddings(asset_id, vector(512))`. No second datastore, and deletes, transactions and backups stay in one place. Opt-in (`DOCSEARCH_SEMANTIC=1`); without it nothing changes and no embed jobs are created.
- **Text:** `paraphrase-multilingual-MiniLM-L12-v2` through fastembed (ONNX, CPU, 384-d, trained on 50+ languages, so a Hindi or Marathi query can retrieve an English passage). Documents are split into passages of about 700 characters (sentence/danda boundaries), at most 16 per document, so the start of a long document represents it; a document's similarity is that of its best passage.
- **Images:** CLIP ViT-B/32. The vision tower embeds pictures at ingest, the text tower embeds a query; both live in one 512-d space, so `GET /v1/search/images?q=a red circle` finds pictures by description. CLIP's text tower is English-centric: non-English image queries are not supported.
- **Ranking:** `GET /v1/search?ranking=keyword|semantic|hybrid`. `hybrid` fuses the BM25 list and the vector list with reciprocal rank fusion (`1/(60+rank)` summed), which needs only ranks, so the two scales need no calibration.
- **Work happens in jobs.** After an upload is indexed, an `embed` job is queued (kind `embed`, its own worker pool). Embedding a transcript embeds every unit; embedding an image stores its vector. A failed or dead embed job **never changes the upload's status**: the upload stays keyword-searchable. A picture with no readable text, which OCR alone would reject, is kept as `indexed` (zero text units) when semantic search is on, because it can be found by its embedding. Jobs are idempotent and only fill what is missing.
- **Models ship in the image** (`deploy/Dockerfile.worker`, offline at run time). Query-time embedding runs in the API process (lazily loaded), so the API needs the same packages and model cache.
- **Deletes:** vector results are filtered against live documents; `delete_asset` removes the upload's pending jobs.

## Measured (real models, in the worker image; tiny sets — plumbing checks, not a benchmark)
- 9 queries (3 Hindi, 2 Marathi, 4 English paraphrases) over 8 English passages: top-1 correct 9/9, top-3 9/9 (similarities 0.43-0.78). The tests require at least 7/9 and 8/9.
- Image: 4 generated pictures (red circle, blue square, green triangle, yellow stripes) found by describing them: 5/5 correct including a black cross; a query for something not in the set ("a photo of a dog") scores lowest (0.24 vs 0.30-0.35 for matches), so a similarity threshold would be needed to say "no result".
- Query embedding: first call includes model loading (~8 s for 8 passages including load), then milliseconds.

## Limits
- Eight passages and five pictures prove the wiring and show the models are usable across Hindi/Marathi/English; they say nothing about quality or recall on a real corpus. Evaluate on labelled queries before trusting it.
- Top-k search over the HNSW index is approximate; deleted and low-ranked passages can displace results at the edges (the store over-fetches 8x and filters).
- Embedding is one job per upload and sequential inside it; throughput is bounded by CPU (the text model embeds on the order of tens of passages per second per core). A 1M-document backfill needs many embed workers and a backfill command, which is not built.
- Videos are embedded through their transcript only (no frame sampling); scanned PDFs through their OCR text (page images are not embedded).
- CLIP is a poor reader of text in images; OCR remains the way to search writing inside pictures.
