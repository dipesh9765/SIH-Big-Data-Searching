-- PostgreSQL schema for the block-postings index (see docs/adr/0008-postgres-postings-storage.md).
-- Idempotent: safe to run on every start.
--
-- Write path:  documents  ->  postings_delta  (small, row per posting, written at ingest)
-- Compactor:   postings_delta  ->  postings_blocks  (compressed, 128 postings per row)
-- Read path:   postings_blocks + postings_delta, read in one REPEATABLE READ snapshot.
--
-- No foreign keys on the posting tables: they are written in bulk and are rebuilt by the compactor,
-- and a constraint check per posting row costs more than the integrity it buys here.

CREATE TABLE IF NOT EXISTS documents (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title          text        NOT NULL,
    source_name    text        NOT NULL,
    media_type     text        NOT NULL,
    lang           text        NOT NULL DEFAULT '',
    content        text        NOT NULL,
    content_sha256 bytea       NOT NULL UNIQUE,   -- identical text is indexed once
    length         integer     NOT NULL CHECK (length > 0),   -- indexed tokens, |d| in BM25
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS terms (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    term     text    NOT NULL UNIQUE,              -- analysed (stemmed, lower-cased)
    doc_freq integer NOT NULL DEFAULT 0            -- documents in *compacted* blocks only
);

CREATE TABLE IF NOT EXISTS postings_blocks (
    term_id   bigint   NOT NULL,
    block_no  integer  NOT NULL,                   -- 0.. in ascending document-id order
    first_doc bigint   NOT NULL,
    last_doc  bigint   NOT NULL,
    count     smallint NOT NULL,                   -- postings in the block, 1..128
    max_tf    integer  NOT NULL,                   -- bound for block-max pruning
    min_len   integer  NOT NULL,                   -- shortest document in the block
    payload   bytea    NOT NULL,                   -- varint deltas + varint term frequencies
    PRIMARY KEY (term_id, block_no)
);

CREATE TABLE IF NOT EXISTS postings_delta (
    term_id bigint  NOT NULL,
    doc_id  bigint  NOT NULL,
    tf      integer NOT NULL,
    PRIMARY KEY (term_id, doc_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value bigint NOT NULL
);

INSERT INTO meta (key, value) VALUES ('doc_count', 0), ('total_length', 0), ('generation', 0)
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------------------------
-- Ingestion: original files live in the object store; these tables track them and their work.

CREATE TABLE IF NOT EXISTS assets (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    object_key  text        NOT NULL UNIQUE,       -- content-addressed: derived from sha256 only
    sha256      bytea       NOT NULL UNIQUE,       -- of the uploaded bytes: identical files are stored once
    source_name text        NOT NULL,
    title       text        NOT NULL,
    size        bigint      NOT NULL,
    status      text        NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'processing', 'indexed', 'failed')),
    document_id bigint,                            -- set when indexed (may be shared: identical text)
    error       text,                              -- user-visible reason when failed
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingest_jobs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id     bigint      NOT NULL,
    kind         text        NOT NULL DEFAULT 'text',   -- which worker pool handles it
    state        text        NOT NULL DEFAULT 'queued'
                 CHECK (state IN ('queued', 'running', 'done', 'dead')),
    attempts     integer     NOT NULL DEFAULT 0,
    max_attempts integer     NOT NULL DEFAULT 5,
    run_after    timestamptz NOT NULL DEFAULT now(),    -- backoff: not before this time
    locked_by    text,
    locked_until timestamptz,                           -- lease; a crashed worker's job becomes claimable
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_jobs_claim ON ingest_jobs (kind, run_after)
    WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ingest_jobs_asset ON ingest_jobs (asset_id);

-- Added with OCR: mean word confidence (0-100) when the text came from OCR, so poor scans can be found.
ALTER TABLE assets ADD COLUMN IF NOT EXISTS ocr_confidence real;

-- Added with speech-to-text: a document can be one *unit* of a larger asset (a stretch of a
-- recording), so a search hit can point at a moment. Ordinary documents leave these NULL.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS asset_id  bigint;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS start_ms  integer;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS end_ms    integer;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS unit_kind text NOT NULL DEFAULT 'document';
CREATE INDEX IF NOT EXISTS documents_asset ON documents (asset_id) WHERE asset_id IS NOT NULL;

ALTER TABLE assets ADD COLUMN IF NOT EXISTS units       integer NOT NULL DEFAULT 1;
ALTER TABLE assets ADD COLUMN IF NOT EXISTS duration_ms integer;
ALTER TABLE assets ADD COLUMN IF NOT EXISTS language    text;

-- Deletion: a document is tombstoned (deleted_at) and excluded from every search at once; a later
-- purge removes its postings and clears its text (purged_at). The tombstone is never removed, so
-- a posting the purge fails to find can still never be returned.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS purged_at  timestamptz;
CREATE INDEX IF NOT EXISTS documents_deleted ON documents (id) WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS documents_unpurged ON documents (id)
    WHERE deleted_at IS NOT NULL AND purged_at IS NULL;
INSERT INTO meta (key, value) VALUES ('deletions', 0) ON CONFLICT (key) DO NOTHING;
