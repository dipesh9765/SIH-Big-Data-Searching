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
from docsearch.engine.codec import BLOCK_SIZE, Block, build_blocks, iter_postings
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


@dataclass(frozen=True, slots=True)
class Added:
    document_id: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class PgHit:
    document: Document
    score: float
    snippet: str
    highlights: list[tuple[int, int]]


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

    def __init__(
        self,
        doc_count: int,
        average_length: float,
        freqs: dict[str, int],
        blocks: dict[str, list[Block]],
        lengths: npt.NDArray[np.int64],
    ) -> None:
        self.doc_count = doc_count
        self.average_length = average_length
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
        """Index a batch in one transaction. Identical text (SHA-256) is stored once."""
        prepared = []
        for doc in docs:
            counts, length = term_frequencies(doc.text)
            if length == 0:
                raise EmptyDocumentError(f"document {doc.title!r} contains no indexable text")
            digest = hashlib.sha256(doc.text.encode("utf-8")).digest()
            prepared.append((doc, counts, length, digest))
        if not prepared:
            return []
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
            marks = ",".join(["(%s,%s,%s,%s,%s,%s,%s)"] * len(fresh))
            for doc, _, length, digest in fresh:
                values += [
                    doc.title,
                    doc.source_name,
                    doc.media_type,
                    doc.lang,
                    doc.text,
                    digest,
                    length,
                ]
            inserted = conn.execute(
                "INSERT INTO documents (title, source_name, media_type, lang, content,"
                f" content_sha256, length) VALUES {marks} ON CONFLICT (content_sha256) DO NOTHING"
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
        results = []
        for position, (_, _, _, digest) in enumerate(prepared):
            is_first_new = digest in ids and first_seen[digest] == position
            results.append(Added(existing[digest], duplicate=not is_first_new))
        return results

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
        return _Snapshot(doc_count, average, freqs, blocks, lengths)

    @staticmethod
    def _hits(conn: psycopg.Connection, outcome: Outcome, terms: set[str]) -> list[PgHit]:
        if not outcome.hits:
            return []
        rows = {
            r[0]: r
            for r in conn.execute(
                "SELECT id, title, source_name, media_type, length, created_at, content"
                " FROM documents WHERE id = ANY(%s)",
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
            hits.append(PgHit(document, scored.score, snippet, highlights))
        return hits

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
