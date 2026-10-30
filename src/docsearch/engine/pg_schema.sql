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
