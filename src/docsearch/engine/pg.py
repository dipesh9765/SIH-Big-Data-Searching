"""PostgreSQL-backed index: ingest into a delta table, compact into compressed blocks, search.

Layout (schema in `pg_schema.sql`, rationale in ADR 0008):

* **Ingest** inserts documents and one row per posting into `postings_delta`. Writers are
  serialised by an advisory lock so document ids become visible in id order, which is what lets the
  compactor append instead of rewrite. Text analysis happens *before* the lock is taken, so many
  worker processes can analyse in parallel and only the short database part is exclusive.
* **Compaction** moves delta rows into `postings_blocks` (128 postings per row, delta + varint
  coded, with per-block score bounds), a chunk of terms per transaction.
* **Search** reads the blocks and the delta of the query terms in one REPEATABLE READ snapshot, so
  a compaction committing mid-query can neither hide nor duplicate a posting, then runs the same
  `engine.search` code that is tested against the SQLite reference.

Not yet implemented (see PROGRESS.md): deletion, lazy per-block fetching (a query currently reads
all blocks of each of its terms), connection pooling.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from importlib import resources

import numpy as np
import numpy.typing as npt
import psycopg
from psycopg import IsolationLevel
from psycopg_pool import ConnectionPool

from docsearch.analysis import query_terms, term_frequencies
from docsearch.engine.codec import BLOCK_SIZE, Block, build_blocks, encode_block, iter_postings
from docsearch.engine.search import Mode, Outcome, Pruning, search
from docsearch.index import Document, EmptyDocumentError, make_snippet

# Advisory locks are (class, hash of the schema name) so indexes in different schemas of one
# database never block each other.
_WRITE_LOCK = 1  # one ingest transaction at a time
_COMPACT_LOCK = 2  # one compactor at a time
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]{0,40}")


@dataclass(frozen=True, slots=True)
class NewDocument:
    title: str
    source_name: str
    media_type: str
    text: str
    lang: str = ""
    # A document may be one stretch (`unit`) of a larger asset, e.g. a minute of a recording.
    asset_id: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    unit_kind: str = "document"
    # Identical *text* is normally stored once. Units of different assets can legitimately say the
    # same words ("thank you"), so they supply a key that includes where they came from.
    dedupe_key: bytes | None = None


@dataclass(frozen=True, slots=True)
class Added:
    document_id: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class MediaRef:
    """Where in an uploaded asset a hit comes from (set for units such as transcript segments)."""

    asset_id: int
    start_ms: int | None
    end_ms: int | None
    unit_kind: str


@dataclass(frozen=True, slots=True)
class PgHit:
    document: Document
    score: float
    snippet: str
    highlights: list[tuple[int, int]]
    media: MediaRef | None = None


@dataclass(frozen=True, slots=True)
class PgSearchResult:
    total: int | None  # None when pruning skipped matches, so the count is unknown
    hits: list[PgHit]
    strategy: str


@dataclass(frozen=True, slots=True)
class CompactionStats:
    terms: int = 0
    postings: int = 0
    skipped: bool = False  # another compactor holds the lock


@dataclass(frozen=True, slots=True)
class PurgeStats:
    documents: int = 0
    postings: int = 0
    skipped: bool = False  # the compactor is running


@dataclass(frozen=True, slots=True)
class PgStats:
    documents: int
    terms: int
    block_postings: int
    delta_postings: int
    average_length: float


class _LengthCache:
    """Token length of every document as a dense array, grown incrementally.

    Safe because ingest is serialised (ids become visible in order): once a row with id N has been
    read, no row with a smaller id can still appear later.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._table: npt.NDArray[np.int64] = np.zeros(1024, dtype=np.int64)
        self._max_id = 0

    def refresh(self, conn: psycopg.Connection) -> npt.NDArray[np.int64]:
        with self._lock:
            rows = conn.execute(
                "SELECT id, length FROM documents WHERE id > %s ORDER BY id", (self._max_id,)
            ).fetchall()
            if rows:
                ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
                lengths = np.fromiter((r[1] for r in rows), dtype=np.int64, count=len(rows))
                top = int(ids[-1])
                if top >= len(self._table):
                    grown = np.zeros(max(top + 1, len(self._table) * 2), dtype=np.int64)
                    grown[: len(self._table)] = self._table
                    self._table = grown
                self._table[ids] = lengths
                self._max_id = top
            return self._table[: self._max_id + 1]


class _Snapshot:
    """A `PostingsSource` over data already fetched inside one database snapshot."""

    def __init__(  # noqa: PLR0913, PLR0917 - a plain bundle of already-fetched snapshot data
        self,
        doc_count: int,
        average_length: float,
        freqs: dict[str, int],
        blocks: dict[str, list[Block]],
        lengths: npt.NDArray[np.int64],
        excluded: frozenset[int],
    ) -> None:
        self.doc_count = doc_count
        self.average_length = average_length
        self.excluded = excluded
        self._freqs = freqs
        self._blocks = blocks
        self._lengths = lengths

    def doc_freq(self, term: str) -> int:
        return self._freqs.get(term, 0)

    def blocks(self, term: str) -> Sequence[Block]:
        return self._blocks.get(term, [])

    def length(self, doc_id: int) -> int:
        return int(self._lengths[doc_id])

    def length_table(self) -> npt.NDArray[np.int64]:
        return self._lengths


def _merge(
    blocks: list[Block], delta: list[tuple[int, int]], lengths: npt.NDArray[np.int64]
) -> list[Block]:
    """Blocks plus uncompacted postings, as one ascending, non-overlapping run of blocks."""
    if not delta:
        return blocks
    if not blocks or delta[0][0] > blocks[-1].last_doc:
        return blocks + build_blocks(delta, lengths)  # the normal case: delta is newer
    merged = sorted([*iter_postings(blocks), *delta])  # out-of-order commit: rare, slow path
    return build_blocks(merged, lengths)


class PgIndex:
    def __init__(self, dsn: str, *, schema: str = "public", pool_size: int = 8) -> None:
        if not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError(f"invalid schema name: {schema!r}")
        self._dsn = dsn
        self._schema = schema
        self._lengths = _LengthCache()
        self._deleted_cache: tuple[int, frozenset[int]] = (-1, frozenset())
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        # Opening a connection costs more than a small query (~10 ms here), so connections are
        # pooled. Every borrower gets a clean, default-isolation connection (see `_read`).
        self._pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=pool_size,
            kwargs={"options": f"-c search_path={schema}"},
            open=True,
        )
        with self._pool.connection() as conn:
            conn.execute(
                resources.files("docsearch.engine").joinpath("pg_schema.sql").read_text("utf-8")
            )

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def schema(self) -> str:
        return self._schema

    def close(self) -> None:
        """Close the connection pool. Safe to call more than once."""
        self._pool.close()

    def __enter__(self) -> PgIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _connect(self) -> AbstractContextManager[psycopg.Connection]:
        return self._pool.connection()

    @contextmanager
    def _read(self) -> Iterator[psycopg.Connection]:
        """A pooled connection in one read-only REPEATABLE READ snapshot, reset afterwards."""
        with self._pool.connection() as conn:
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            try:
                yield conn
            finally:
                conn.rollback()  # end the snapshot before the connection goes back to the pool
                conn.isolation_level = None
                conn.read_only = None

    # ------------------------------------------------------------------------------ ingest

    def add(self, doc: NewDocument) -> Added:
        return self.add_many([doc])[0]

    def add_many(self, docs: Sequence[NewDocument]) -> list[Added]:
        """Index a batch in one transaction. Identical text (SHA-256) is stored once.

        Raises `EmptyDocumentError` (and indexes nothing) if any document has no indexable text.
        """
        results = self._add(docs, skip_empty=False)
        return [added for added in results if added is not None]

    def add_many_skipping_empty(self, docs: Sequence[NewDocument]) -> list[Added | None]:
        """Like `add_many`, but a document with no indexable text yields None instead of failing
        the whole batch. The result lines up with `docs`."""
        return self._add(docs, skip_empty=True)

    def connection(self) -> AbstractContextManager[psycopg.Connection]:
        """A pooled connection (commits on a clean exit, rolls back on an exception)."""
        return self._connect()

    def _add(self, docs: Sequence[NewDocument], *, skip_empty: bool) -> list[Added | None]:
        prepared: list[tuple[NewDocument, dict[str, int], int, bytes]] = []
        slots: list[int | None] = []  # per input document: its position in `prepared`, or None
        for doc in docs:
            frequencies, length = term_frequencies(doc.text)
            if length == 0:
                if not skip_empty:
                    raise EmptyDocumentError(f"document {doc.title!r} contains no indexable text")
                slots.append(None)
                continue
            digest = hashlib.sha256(
                doc.dedupe_key if doc.dedupe_key is not None else doc.text.encode("utf-8")
            ).digest()
            slots.append(len(prepared))
            prepared.append((doc, frequencies, length, digest))
        if not prepared:
            return [None] * len(slots)
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_WRITE_LOCK, self._schema)
            )
            first_seen: dict[bytes, int] = {}
            values: list[object] = []
            fresh = []
            for position, (doc, counts, length, digest) in enumerate(prepared):
                if digest in first_seen:
                    continue  # duplicate inside this batch
                first_seen[digest] = position
                fresh.append((doc, counts, length, digest))
            marks = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(fresh))
            for doc, _, length, digest in fresh:
                values += [
                    doc.title,
                    doc.source_name,
                    doc.media_type,
                    doc.lang,
                    doc.text,
                    digest,
                    length,
                    doc.asset_id,
                    doc.start_ms,
                    doc.end_ms,
                    doc.unit_kind,
                ]
            inserted = conn.execute(
                "INSERT INTO documents (title, source_name, media_type, lang, content,"
                " content_sha256, length, asset_id, start_ms, end_ms, unit_kind)"
                f" VALUES {marks} ON CONFLICT (content_sha256) DO NOTHING"
                " RETURNING id, content_sha256",
                values,
            ).fetchall()
            ids = {bytes(digest): doc_id for doc_id, digest in inserted}
            counts_by_digest = {digest: (counts, length) for _, counts, length, digest in fresh}
            new_docs = [(doc_id, counts_by_digest[digest][0]) for digest, doc_id in ids.items()]
            if new_docs:
                self._write_postings(conn, new_docs)
                added_tokens = sum(counts_by_digest[digest][1] for digest in ids)
                for key, delta in (
                    ("doc_count", len(new_docs)),
                    ("total_length", added_tokens),
                    ("generation", 1),
                ):
                    conn.execute("UPDATE meta SET value = value + %s WHERE key = %s", (delta, key))
            existing = {
                bytes(d): i
                for i, d in conn.execute(
                    "SELECT id, content_sha256 FROM documents WHERE content_sha256 = ANY(%s)",
                    ([digest for _, _, _, digest in prepared],),
                )
            }
        added: list[Added] = []
        for position, (_, _, _, digest) in enumerate(prepared):
            is_first_new = digest in ids and first_seen[digest] == position
            added.append(Added(existing[digest], duplicate=not is_first_new))
        return [None if slot is None else added[slot] for slot in slots]

    @staticmethod
    def _write_postings(
        conn: psycopg.Connection, items: Iterable[tuple[int, dict[str, int]]]
    ) -> None:
        documents = list(items)
        vocabulary = sorted({term for _, counts in documents for term in counts})
        conn.execute(
            "INSERT INTO terms (term) SELECT unnest(%s::text[]) ON CONFLICT (term) DO NOTHING",
            (vocabulary,),
        )
        term_ids = {
            term: term_id
            for term_id, term in conn.execute(
                "SELECT id, term FROM terms WHERE term = ANY(%s)", (vocabulary,)
            )
        }
        with conn.cursor().copy("COPY postings_delta (term_id, doc_id, tf) FROM STDIN") as copy:
            for doc_id, counts in documents:
                for term, tf in counts.items():
                    copy.write_row((term_ids[term], doc_id, tf))

    # -------------------------------------------------------------------------- compaction

    def compact(self, *, term_batch: int = 1000) -> CompactionStats:
        """Fold `postings_delta` into `postings_blocks`; returns what was moved.

        One transaction per chunk of terms, so a crash loses nothing and readers (who use a
        snapshot) never see a half-moved term. Only one compactor runs at a time.
        """
        with psycopg.connect(self._dsn, autocommit=True) as guard:
            locked = guard.execute(
                "SELECT pg_try_advisory_lock(%s, hashtext(%s))", (_COMPACT_LOCK, self._schema)
            ).fetchone()
            if locked is None or not locked[0]:
                return CompactionStats(skipped=True)
            terms = postings = after = 0
            while True:
                with self._connect() as conn:
                    moved_terms, moved_postings, after = self._compact_chunk(
                        conn, term_batch, after
                    )
                if moved_terms == 0:
                    break
                terms += moved_terms
                postings += moved_postings
            return CompactionStats(terms, postings)

    def _compact_chunk(
        self, conn: psycopg.Connection, term_batch: int, after: int
    ) -> tuple[int, int, int]:
        """Move the next `term_batch` terms with id > `after`; returns (terms, postings, last id).

        Keyset paging, not "the first N terms": deleted delta rows linger as dead index entries
        until vacuum, and a scan that restarts from the beginning each time walks over all of them
        (measured: compaction slowed from 80k to 22k postings/s as the delta grew). Terms that
        gain postings behind the cursor while this runs are picked up by the next `compact()`.
        """
        term_ids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT term_id FROM postings_delta WHERE term_id > %s"
                " ORDER BY term_id LIMIT %s",
                (after, term_batch),
            )
        ]
        if not term_ids:
            return 0, 0, after
        rows = conn.execute(
            "DELETE FROM postings_delta WHERE term_id = ANY(%s) RETURNING term_id, doc_id, tf",
            (term_ids,),
        ).fetchall()
        lengths = self._lengths.refresh(conn)
        delta: dict[int, list[tuple[int, int]]] = {}
        for term_id, doc_id, tf in rows:
            delta.setdefault(term_id, []).append((doc_id, tf))
        tails = {
            r[0]: (r[1], Block(r[2], r[3], r[4], r[5], r[6], bytes(r[7])))
            for r in conn.execute(
                "SELECT DISTINCT ON (term_id) term_id, block_no, first_doc, last_doc, count,"
                " max_tf, min_len, payload FROM postings_blocks WHERE term_id = ANY(%s)"
                " ORDER BY term_id, block_no DESC",
                (term_ids,),
            )
        }
        out: list[tuple[int, int, int, int, int, int, int, bytes]] = []
        for term_id, postings in delta.items():
            postings.sort()
            tail = tails.get(term_id)
            if tail is None:
                first_no, merged = 0, postings
            elif postings[0][0] > tail[1].last_doc:
                block_no, block = tail
                if block.count < BLOCK_SIZE:  # top up the partial last block
                    first_no, merged = block_no, [*iter_postings([block]), *postings]
                else:
                    first_no, merged = block_no + 1, postings
            else:  # out-of-order ids (cannot happen with serialised ingest): rewrite the term
                existing = conn.execute(
                    "SELECT first_doc, last_doc, count, max_tf, min_len, payload FROM"
                    " postings_blocks WHERE term_id = %s ORDER BY block_no",
                    (term_id,),
                ).fetchall()
                old = [Block(r[0], r[1], r[2], r[3], r[4], bytes(r[5])) for r in existing]
                first_no, merged = 0, sorted([*iter_postings(old), *postings])
            for number, block in enumerate(build_blocks(merged, lengths), start=first_no):
                out.append(
                    (
                        term_id,
                        number,
                        block.first_doc,
                        block.last_doc,
                        block.count,
                        block.max_tf,
                        block.min_len,
                        block.payload,
                    )
                )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO postings_blocks (term_id, block_no, first_doc, last_doc, count,"
                " max_tf, min_len, payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (term_id, block_no) DO UPDATE SET first_doc = EXCLUDED.first_doc,"
                " last_doc = EXCLUDED.last_doc, count = EXCLUDED.count, max_tf = EXCLUDED.max_tf,"
                " min_len = EXCLUDED.min_len, payload = EXCLUDED.payload",
                out,
            )
        conn.execute(
            "UPDATE terms t SET doc_freq = t.doc_freq + v.n FROM"
            " (SELECT unnest(%s::bigint[]) AS id, unnest(%s::int[]) AS n) v WHERE t.id = v.id",
            (list(delta), [len(p) for p in delta.values()]),
        )
        conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'generation'")
        return len(delta), len(rows), term_ids[-1]

    # ------------------------------------------------------------------------------ search

    def search(
        self,
        query: str,
        *,
        mode: Mode = "all",
        limit: int = 10,
        exact_total: bool = False,
        pruning: Pruning = "auto",
    ) -> PgSearchResult:
        terms = query_terms(query)
        if not terms:
            return PgSearchResult(0, [], "")
        with self._read() as conn:
            snapshot = self._snapshot(conn, terms)
            if snapshot.doc_count == 0:
                return PgSearchResult(0, [], "")
            outcome = search(
                snapshot, terms, mode=mode, k=limit, exact_total=exact_total, pruning=pruning
            )
            hits = self._hits(conn, outcome, set(terms))
        return PgSearchResult(outcome.total, hits, outcome.stats.strategy)

    def _snapshot(self, conn: psycopg.Connection, terms: list[str]) -> _Snapshot:
        meta: dict[str, int] = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        doc_count, total_length = int(meta["doc_count"]), int(meta["total_length"])
        lengths = self._lengths.refresh(conn)
        average = total_length / doc_count if doc_count else 0.0
        info = {
            term: (term_id, df)
            for term_id, term, df in conn.execute(
                "SELECT id, term, doc_freq FROM terms WHERE term = ANY(%s)", (terms,)
            )
        }
        ids = [term_id for term_id, _ in info.values()]
        by_id = {term_id: term for term, (term_id, _) in info.items()}
        delta: dict[str, list[tuple[int, int]]] = {t: [] for t in info}
        for term_id, doc_id, tf in conn.execute(
            "SELECT term_id, doc_id, tf FROM postings_delta WHERE term_id = ANY(%s)"
            " ORDER BY term_id, doc_id",
            (ids,),
        ):
            delta[by_id[term_id]].append((doc_id, tf))
        compacted: dict[str, list[Block]] = {t: [] for t in info}
        for row in conn.execute(
            "SELECT term_id, first_doc, last_doc, count, max_tf, min_len, payload"
            " FROM postings_blocks WHERE term_id = ANY(%s) ORDER BY term_id, block_no",
            (ids,),
        ):
            compacted[by_id[row[0]]].append(
                Block(row[1], row[2], row[3], row[4], row[5], bytes(row[6]))
            )
        freqs = {term: df + len(delta[term]) for term, (_, df) in info.items()}
        blocks = {term: _merge(compacted[term], delta[term], lengths) for term in info}
        excluded = self._deleted_ids(conn, int(meta.get("deletions", 0)))
        return _Snapshot(doc_count, average, freqs, blocks, lengths, excluded)

    @staticmethod
    def _hits(conn: psycopg.Connection, outcome: Outcome, terms: set[str]) -> list[PgHit]:
        if not outcome.hits:
            return []
        rows = {
            r[0]: r
            for r in conn.execute(
                "SELECT id, title, source_name, media_type, length, created_at, content,"
                " asset_id, start_ms, end_ms, unit_kind FROM documents WHERE id = ANY(%s)",
                ([h.doc_id for h in outcome.hits],),
            )
        }
        hits = []
        for scored in outcome.hits:
            row = rows[scored.doc_id]
            snippet, highlights = make_snippet(row[6], terms)
            document = Document(
                id=row[0],
                title=row[1],
                source_name=row[2],
                media_type=row[3],
                length=row[4],
                created_at=row[5].isoformat(timespec="seconds"),
            )
            media = None if row[7] is None else MediaRef(row[7], row[8], row[9], row[10])
            hits.append(PgHit(document, scored.score, snippet, highlights, media))
        return hits

    # ----------------------------------------------------------------------------- deletion

    def _deleted_ids(self, conn: psycopg.Connection, version: int) -> frozenset[int]:
        """Ids of tombstoned documents, cached until the `deletions` counter changes."""
        cached_version, cached = self._deleted_cache
        if cached_version == version:
            return cached
        ids = frozenset(
            r[0] for r in conn.execute("SELECT id FROM documents WHERE deleted_at IS NOT NULL")
        )
        self._deleted_cache = (version, ids)
        return ids

    def delete_documents(self, ids: Sequence[int]) -> int:
        """Remove documents from search immediately. Returns how many were live.

        The tombstone excludes them from every query at once; `purge_deleted` later removes their
        postings and text. Their content hash is replaced so the same text can be indexed again.
        """
        if not ids:
            return 0
        tombstones = [hashlib.sha256(f"deleted:{doc_id}".encode()).digest() for doc_id in ids]
        with self._connect() as conn:
            rows = conn.execute(
                "UPDATE documents d SET deleted_at = now(), content_sha256 = v.hash"
                " FROM (SELECT unnest(%s::bigint[]) AS id, unnest(%s::bytea[]) AS hash) v"
                " WHERE d.id = v.id AND d.deleted_at IS NULL RETURNING d.id, d.length",
                (list(ids), tombstones),
            ).fetchall()
            if rows:
                for key, delta in (
                    ("doc_count", -len(rows)),
                    ("total_length", -sum(r[1] for r in rows)),
                    ("deletions", 1),
                    ("generation", 1),
                ):
                    conn.execute("UPDATE meta SET value = value + %s WHERE key = %s", (delta, key))
        return len(rows)

    def purge_deleted(self, *, batch: int = 200) -> PurgeStats:
        """Remove the postings of deleted documents and clear their text.

        Finds a document's terms by analysing its stored text again, so it depends on the analyzer
        being unchanged since indexing. If it ever is not, a posting can survive; the tombstone
        still keeps it out of results, so the cost is space and a slightly high document
        frequency, never a wrong answer. Runs under the compaction lock (both rewrite blocks).
        """
        with psycopg.connect(self._dsn, autocommit=True) as guard:
            locked = guard.execute(
                "SELECT pg_try_advisory_lock(%s, hashtext(%s))", (_COMPACT_LOCK, self._schema)
            ).fetchone()
            if locked is None or not locked[0]:
                return PurgeStats(skipped=True)
            docs = postings = 0
            while True:
                with self._connect() as conn:
                    done, removed = self._purge_batch(conn, batch)
                if done == 0:
                    return PurgeStats(docs, postings)
                docs += done
                postings += removed

    def _purge_batch(self, conn: psycopg.Connection, batch: int) -> tuple[int, int]:
        rows = conn.execute(
            "SELECT id, content FROM documents WHERE deleted_at IS NOT NULL AND purged_at IS NULL"
            " ORDER BY id LIMIT %s",
            (batch,),
        ).fetchall()
        if not rows:
            return 0, 0
        lengths = self._lengths.refresh(conn)
        vocabulary = sorted({t for _, text in rows for t in term_frequencies(text)[0]})
        term_ids = {
            term: term_id
            for term_id, term in conn.execute(
                "SELECT id, term FROM terms WHERE term = ANY(%s)", (vocabulary,)
            )
        }
        removed = 0
        conn.execute(
            "DELETE FROM postings_delta WHERE doc_id = ANY(%s)", ([doc_id for doc_id, _ in rows],)
        )
        pending: dict[tuple[int, int], set[int]] = {}  # (term id, block no) -> doc ids to drop
        found: dict[tuple[int, int], tuple[Block, int]] = {}
        for doc_id, text in rows:
            for term in term_frequencies(text)[0]:
                term_id = term_ids.get(term)
                if term_id is None:
                    continue
                row = conn.execute(
                    "SELECT block_no, first_doc, last_doc, count, max_tf, min_len, payload"
                    " FROM postings_blocks"
                    " WHERE term_id = %s AND first_doc <= %s AND last_doc >= %s",
                    (term_id, doc_id, doc_id),
                ).fetchone()
                if row is None:
                    continue
                key = (term_id, row[0])
                found[key] = (Block(row[1], row[2], row[3], row[4], row[5], bytes(row[6])), row[0])
                pending.setdefault(key, set()).add(doc_id)
        decrements: dict[int, int] = {}
        for (term_id, block_no), drop in pending.items():
            block, _ = found[(term_id, block_no)]
            ids, tfs = block.decode()
            keep = [(i, t) for i, t in zip(ids, tfs, strict=True) if i not in drop]
            gone = len(ids) - len(keep)
            if gone == 0:
                continue
            removed += gone
            decrements[term_id] = decrements.get(term_id, 0) + gone
            if not keep:
                conn.execute(
                    "DELETE FROM postings_blocks WHERE term_id = %s AND block_no = %s",
                    (term_id, block_no),
                )
                continue
            new = encode_block(
                [i for i, _ in keep], [t for _, t in keep], [int(lengths[i]) for i, _ in keep]
            )
            conn.execute(
                "UPDATE postings_blocks SET first_doc = %s, last_doc = %s, count = %s, max_tf = %s,"
                " min_len = %s, payload = %s WHERE term_id = %s AND block_no = %s",
                (
                    new.first_doc,
                    new.last_doc,
                    new.count,
                    new.max_tf,
                    new.min_len,
                    new.payload,
                    term_id,
                    block_no,
                ),
            )
        if decrements:
            conn.execute(
                "UPDATE terms t SET doc_freq = greatest(t.doc_freq - v.n, 0) FROM"
                " (SELECT unnest(%s::bigint[]) AS id, unnest(%s::int[]) AS n) v WHERE t.id = v.id",
                (list(decrements), list(decrements.values())),
            )
        conn.execute(
            "UPDATE documents SET purged_at = now(), content = '' WHERE id = ANY(%s)",
            ([doc_id for doc_id, _ in rows],),
        )
        conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'generation'")
        return len(rows), removed

    # ---------------------------------------------------------------------------- documents

    _DOCUMENT_COLUMNS = "id, title, source_name, media_type, length, created_at"

    @staticmethod
    def _document(row: tuple[object, ...]) -> Document:
        created = row[5]
        return Document(
            id=int(row[0]),  # type: ignore[call-overload]
            title=str(row[1]),
            source_name=str(row[2]),
            media_type=str(row[3]),
            length=int(row[4]),  # type: ignore[call-overload]
            created_at=created.isoformat(timespec="seconds"),  # type: ignore[attr-defined]
        )

    def get_document(self, doc_id: int) -> Document | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._DOCUMENT_COLUMNS} FROM documents"
                " WHERE id = %s AND deleted_at IS NULL",
                (doc_id,),
            ).fetchone()
        return None if row is None else self._document(row)

    def list_documents(self, *, limit: int = 50, offset: int = 0) -> list[Document]:
        """Newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._DOCUMENT_COLUMNS} FROM documents WHERE deleted_at IS NULL"
                " ORDER BY id DESC"
                " LIMIT %s OFFSET %s",
                (limit, offset),
            ).fetchall()
        return [self._document(row) for row in rows]

    # -------------------------------------------------------------------------- inspection

    def stats(self) -> PgStats:
        with self._connect() as conn:
            meta: dict[str, int] = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            terms = conn.execute("SELECT count(*) FROM terms").fetchone()[0]  # type: ignore[index]
            in_blocks = conn.execute(
                "SELECT coalesce(sum(count), 0) FROM postings_blocks"
            ).fetchone()[0]  # type: ignore[index]
            in_delta = conn.execute("SELECT count(*) FROM postings_delta").fetchone()[0]  # type: ignore[index]
        docs = int(meta["doc_count"])
        return PgStats(
            docs, terms, int(in_blocks), in_delta, meta["total_length"] / docs if docs else 0.0
        )

    def approximate_counts(self) -> dict[str, int]:
        """Cheap gauges for monitoring: exact document/token counters from `meta`, and planner
        estimates for the big tables (a `count(*)` over millions of rows is too slow to scrape)."""
        with self._connect() as conn:
            meta: dict[str, int] = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            estimates: dict[str, int] = dict(
                conn.execute(
                    "SELECT relname, greatest(reltuples, 0)::bigint FROM pg_class"
                    " WHERE relnamespace = to_regnamespace(%s)"
                    " AND relname IN ('terms', 'postings_delta', 'postings_blocks')",
                    (self._schema,),
                ).fetchall()
            )
        return {
            "documents": int(meta["doc_count"]),
            "tokens": int(meta["total_length"]),
            "terms_estimate": int(estimates.get("terms", 0)),
            "delta_postings_estimate": int(estimates.get("postings_delta", 0)),
            "blocks_estimate": int(estimates.get("postings_blocks", 0)),
        }

    def drop_schema(self) -> None:
        """Remove everything this index created (tests use throwaway schemas)."""
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')


__all__ = [
    "Added",
    "CompactionStats",
    "NewDocument",
    "PgHit",
    "PgIndex",
    "PgSearchResult",
    "PgStats",
]
