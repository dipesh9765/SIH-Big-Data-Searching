# 10. Originals in an S3-compatible object store, content-addressed

Status: accepted (scale-out work, `feature/scale-v2`)

## Decision
- Uploaded files are stored in an object store, never in the database. The database holds the extracted text and metadata; the original is kept so it can be re-processed (new extractor, OCR, re-indexing) and served back.
- The object key is `assets/<first two hex>/<sha256>`: identical uploads share one object, a key never collides with a different file, and no user-supplied text (a file name) reaches a storage path. The store is written *before* the database row, so a crash leaves at most an unreferenced object; the reverse order would leave a row pointing at nothing.
- The code speaks only the S3 API (`ingest/objects.py`, the `minio` client library), behind a small `ObjectStore` protocol with an in-memory implementation for tests.

## Server: SeaweedFS, not MinIO
The plan named MinIO. Its public container images were withdrawn from Docker Hub and quay.io (pulls fail with "repository does not exist" and 401), so the compose file uses SeaweedFS (Apache-2.0), pinned by image digest. Because only the S3 API is used, switching servers (MinIO built from source, Ceph, Garage, AWS S3) is a configuration change (`DOCSEARCH_S3_*`).
SeaweedFS detail found in testing: every bucket becomes a collection with its own volumes, so `-volume.max` must not be small (a limit of 16 was exhausted by a handful of test buckets); the compose file uses automatic limits and a 256 MB volume size.

## Verified
`tests/test_ingest.py::test_s3_store_round_trip_when_a_server_is_available` runs put/get/exists/delete/missing-key against the real server (skipped when none is reachable). End-to-end through the HTTP service and CLI worker in `tests/test_service.py` and a real-process run.

## Limits
- Uploads are read fully into memory (capped by `DOCSEARCH_MAX_UPLOAD_MB`, default 10). Large media needs streaming or pre-signed direct uploads to the store; not built.
- Orphan objects from crashes between the store write and the database insert are never collected.
- No encryption at rest or per-object access control beyond the server's own.
- The development credentials in `deploy/s3.json` and the defaults in `ServiceSettings` are for local use only.
