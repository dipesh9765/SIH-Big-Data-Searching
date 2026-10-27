"""SQLite-backed inverted index with BM25 ranking.

Every write runs in one `BEGIN IMMEDIATE` transaction that updates documents, postings, per-term
document frequency and the corpus statistics together, so a crash can never leave the index
half-updated. Each operation opens its own connection (cheap for SQLite, and it keeps the class
safe to share between the threads of a web server); WAL mode lets readers run during a write.
"""

from __future__ import annotations

import hashlib
import heapq
import math
import sqlite3
import weakref
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Literal

from docsearch.analysis import query_terms, term_frequencies, tokenize

BM25_K1 = 1.2  # term-frequency saturation
BM25_B = 0.75  # document-length normalisation
SNIPPET_CHARS = 220
_SQL_CHUNK = 500  # stays far below SQLite's bound-variable limit

Mode = Literal["all", "any"]


class EmptyDocumentError(ValueError):
    """The text contains nothing indexable (empty, all stop words, or symbols only)."""


@dataclass(frozen=True, slots=True)
class Document:
    id: int
    title: str
    source_name: str
    media_type: str
    length: int
    created_at: str


@dataclass(frozen=True, slots=True)
class Added:
    document: Document
    duplicate: bool


@dataclass(frozen=True, slots=True)
class Hit:
    document: Document
    score: float
    snippet: str
    highlights: list[tuple[int, int]]  # [start, end) offsets into `snippet`


@dataclass(frozen=True, slots=True)
class SearchResult:
    total: int
    hits: list[Hit]


@dataclass(frozen=True, slots=True)
class Stats:
    documents: int
    terms: int
    postings: int
    average_length: float


def bm25_idf(doc_count: int, doc_freq: int) -> float:
    """Robertson-Spärck Jones idf with the +1 inside the log so it is never negative."""
    return math.log(1.0 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))


def bm25_term_score(idf: float, tf: int, length: int, average_length: float) -> float:
    norm = 1.0 - BM25_B + BM25_B * length / average_length
    return idf * tf * (BM25_K1 + 1.0) / (tf + BM25_K1 * norm)


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        title=row["title"],
        source_name=row["source_name"],
        media_type=row["media_type"],
        length=row["length"],
        created_at=row["created_at"],
    )


class Index:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One idle connection stays open for the life of the index. In WAL mode SQLite checkpoints
        # and deletes the WAL file whenever the *last* connection closes; with a connection per
        # operation that cost ~7 ms per write (profiled), most of the indexing time.
        self._keepalive: sqlite3.Connection | None = self._open()
        weakref.finalize(self, self._keepalive.close)
        self._keepalive.execute("PRAGMA journal_mode=WAL")
        self._keepalive.executescript(
            resources.files("docsearch").joinpath("schema.sql").read_text("utf-8")
        )

    def close(self) -> None:
        """Release the idle connection (checkpoints the WAL). Safe to call more than once."""
        if self._keepalive is not None:
            self._keepalive.close()
            self._keepalive = None

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ------------------------------------------------------------------ writes

    def add(self, *, title: str, source_name: str, media_type: str, text: str) -> Added:
        """Index `text`. Identical text (same SHA-256) is stored once and reported as duplicate."""
        counts, length = term_frequencies(text)
        if length == 0:
            raise EmptyDocumentError("document contains no indexable text")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._write() as conn:
            existing = conn.execute(
                "SELECT * FROM documents WHERE content_sha256 = ?", (digest,)
            ).fetchone()
            if existing is not None:
                return Added(_row_to_document(existing), duplicate=True)
            cursor = conn.execute(
                "INSERT INTO documents (title, source_name, media_type, content, content_sha256,"
                " length, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    title,
                    source_name,
                    media_type,
                    text,
                    digest,
                    length,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
            doc_id = cursor.lastrowid
            assert doc_id is not None
            terms = [(term,) for term in counts]
            conn.executemany("INSERT OR IGNORE INTO terms (term) VALUES (?)", terms)
            conn.executemany("UPDATE terms SET doc_freq = doc_freq + 1 WHERE term = ?", terms)
            conn.executemany(
                "INSERT INTO postings (term_id, doc_id, tf)"
                " SELECT id, ?, ? FROM terms WHERE term = ?",
                [(doc_id, tf, term) for term, tf in counts.items()],
            )
            self._bump_stats(conn, documents=1, length=length)
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return Added(_row_to_document(row), duplicate=False)

    def delete(self, doc_id: int) -> bool:
        """Remove a document and its postings; returns False if it did not exist."""
        with self._write() as conn:
            row = conn.execute("SELECT length FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE terms SET doc_freq = doc_freq - 1"
                " WHERE id IN (SELECT term_id FROM postings WHERE doc_id = ?)",
                (doc_id,),
            )
            conn.execute("DELETE FROM postings WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM terms WHERE doc_freq <= 0")
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self._bump_stats(conn, documents=-1, length=-row["length"])
        return True

    @staticmethod
    def _bump_stats(conn: sqlite3.Connection, *, documents: int, length: int) -> None:
        for key, delta in (("doc_count", documents), ("total_length", length)):
            conn.execute(
                "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT) WHERE key = ?",
                (delta, key),
            )

    # ------------------------------------------------------------------- reads

    def get(self, doc_id: int) -> Document | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return None if row is None else _row_to_document(row)

    def list_documents(self, *, limit: int = 50, offset: int = 0) -> list[Document]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM documents ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [_row_to_document(row) for row in rows]

    def stats(self) -> Stats:
        with self._connect() as conn:
            docs, total = self._corpus(conn)
            terms = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
            postings = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        return Stats(docs, terms, postings, total / docs if docs else 0.0)

    @staticmethod
    def _corpus(conn: sqlite3.Connection) -> tuple[int, int]:
        rows = conn.execute(
            "SELECT key, CAST(value AS INTEGER) AS v FROM meta"
            " WHERE key IN ('doc_count', 'total_length')"
        ).fetchall()
        values = {row["key"]: row["v"] for row in rows}
        return values["doc_count"], values["total_length"]

    def search(
        self, query: str, *, mode: Mode = "all", limit: int = 10, offset: int = 0
    ) -> SearchResult:
        """Rank documents for `query` with BM25.

        mode="all" requires every query term (AND); mode="any" requires at least one (OR).
        Ties are broken by document id so results are deterministic.
        """
        terms = query_terms(query)
        if not terms:
            return SearchResult(0, [])
        with self._connect() as conn:
            doc_count, total_length = self._corpus(conn)
            if doc_count == 0:
                return SearchResult(0, [])
            average_length = total_length / doc_count
            known = self._term_rows(conn, terms)
            if mode == "all" and len(known) < len(terms):
                return SearchResult(0, [])  # a required term is not in the index at all
            scores: dict[int, float] = {}
            matched: Counter[int] = Counter()
            for term_id, doc_freq in known:
                idf = bm25_idf(doc_count, doc_freq)
                postings = conn.execute(
                    "SELECT p.doc_id, p.tf, d.length FROM postings p"
                    " JOIN documents d ON d.id = p.doc_id WHERE p.term_id = ?",
                    (term_id,),
                )
                for doc_id, tf, length in postings:
                    scores[doc_id] = scores.get(doc_id, 0.0) + bm25_term_score(
                        idf, tf, length, average_length
                    )
                    matched[doc_id] += 1
            if mode == "all":
                scores = {d: s for d, s in scores.items() if matched[d] == len(terms)}
            top = heapq.nsmallest(offset + limit, scores.items(), key=lambda kv: (-kv[1], kv[0]))
            page = top[offset:]
            hits = [self._hit(conn, doc_id, score, set(terms)) for doc_id, score in page]
        return SearchResult(len(scores), hits)

    @staticmethod
    def _term_rows(conn: sqlite3.Connection, terms: Sequence[str]) -> list[tuple[int, int]]:
        rows: list[tuple[int, int]] = []
        for start in range(0, len(terms), _SQL_CHUNK):
            chunk = terms[start : start + _SQL_CHUNK]
            marks = ",".join("?" * len(chunk))
            cursor = conn.execute(
                f"SELECT id, doc_freq FROM terms WHERE term IN ({marks})", tuple(chunk)
            )
            rows.extend((row["id"], row["doc_freq"]) for row in cursor)
        return rows

    def _hit(self, conn: sqlite3.Connection, doc_id: int, score: float, terms: set[str]) -> Hit:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        snippet, highlights = make_snippet(row["content"], terms)
        return Hit(_row_to_document(row), score, snippet, highlights)


def make_snippet(
    text: str, terms: set[str], width: int = SNIPPET_CHARS
) -> tuple[str, list[tuple[int, int]]]:
    """A window of `text` around the densest cluster of query terms, with match offsets.

    Returns plain text plus offsets (not HTML) so a client can highlight without trusting markup.
    """
    matches = [(t.start, t.end) for t in tokenize(text) if t.term in terms]
    if not matches:
        return text[:width].strip(), []
    # Slide over matches and keep the window start that covers the most of them.
    best_start, best_count, right = matches[0][0], 0, 0
    for left, (start, _) in enumerate(matches):
        while right < len(matches) and matches[right][1] - start <= width:
            right += 1
        if right - left > best_count:
            best_start, best_count = start, right - left
    begin = max(0, best_start - width // 5)
    end = min(len(text), begin + width)
    # Prefer to cut on whitespace so words are not sliced in half.
    if begin > 0 and (space := text.find(" ", begin, best_start)) != -1:
        begin = space + 1
    if end < len(text) and (space := text.rfind(" ", best_start, end)) != -1:
        end = space
    window = text[begin:end]
    highlights = [(s - begin, e - begin) for s, e in matches if s >= begin and e <= end]
    prefix = "… " if begin > 0 else ""
    suffix = " …" if end < len(text) else ""
    shift = len(prefix)
    return (
        f"{prefix}{window}{suffix}",
        [(s + shift, e + shift) for s, e in highlights],
    )
