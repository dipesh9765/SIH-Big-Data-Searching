-- docsearch storage schema, version 1.
-- Applied by docsearch.index.Index when the database is empty. SQLite, WAL mode, foreign keys on.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

-- One row per indexed document. `content` is the extracted text (the original upload is not kept).
-- `length` is the number of indexed tokens (after stop-word removal) and is the |d| of BM25.
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    source_name    TEXT    NOT NULL,
    media_type     TEXT    NOT NULL,
    content        TEXT    NOT NULL,
    content_sha256 TEXT    NOT NULL UNIQUE,   -- identical text is indexed once
    length         INTEGER NOT NULL CHECK (length > 0),
    created_at     TEXT    NOT NULL           -- ISO 8601, UTC
);

-- The vocabulary. `doc_freq` is the number of documents containing the term (the df of BM25),
-- maintained in the same transaction as the postings so it can never drift.
CREATE TABLE IF NOT EXISTS terms (
    id       INTEGER PRIMARY KEY,
    term     TEXT    NOT NULL UNIQUE,         -- analysed (lower-cased, stemmed) form
    doc_freq INTEGER NOT NULL DEFAULT 0 CHECK (doc_freq >= 0)
);

-- The inverted index: for each (term, document) the term frequency.
-- Clustered on (term_id, doc_id) so one term's postings are a single contiguous range scan.
CREATE TABLE IF NOT EXISTS postings (
    term_id INTEGER NOT NULL REFERENCES terms (id)     ON DELETE CASCADE,
    doc_id  INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    tf      INTEGER NOT NULL CHECK (tf > 0),
    PRIMARY KEY (term_id, doc_id)
) WITHOUT ROWID;

-- Lets deletion find a document's terms without scanning the whole table.
CREATE INDEX IF NOT EXISTS postings_by_doc ON postings (doc_id);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO meta (key, value) VALUES ('doc_count', '0');
INSERT OR IGNORE INTO meta (key, value) VALUES ('total_length', '0');
