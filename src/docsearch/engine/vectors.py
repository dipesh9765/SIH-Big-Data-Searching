"""Vector storage and nearest-neighbour search in PostgreSQL (pgvector, HNSW, cosine distance).

Text is stored as one vector per passage (`text_embeddings`); a document's similarity is that of its
best passage. Images are stored per asset (`image_embeddings`). Both need the `vector` extension;
`VectorStore.available` is false when it cannot be installed, and the HTTP layer then reports
semantic search as unavailable instead of failing.

Rankings from different engines are combined with reciprocal rank fusion (`fuse`): it needs only
ranks, so it does not matter that BM25 scores and cosine similarities are on different scales.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import psycopg

from docsearch.engine.pg import PgIndex
from docsearch.semantic import IMAGE_DIM, TEXT_DIM, Vectors

log = logging.getLogger("docsearch.vectors")

RRF_K = (
    60  # the constant from the original RRF paper; larger values flatten the contribution of rank
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS text_embeddings (
    doc_id    bigint  NOT NULL,
    chunk_no  integer NOT NULL,
    embedding public.vector({TEXT_DIM}) NOT NULL,
    PRIMARY KEY (doc_id, chunk_no)
);
CREATE INDEX IF NOT EXISTS text_embeddings_hnsw
    ON text_embeddings USING hnsw (embedding public.vector_cosine_ops);
CREATE TABLE IF NOT EXISTS image_embeddings (
    asset_id  bigint PRIMARY KEY,
    embedding public.vector({IMAGE_DIM}) NOT NULL
);
CREATE INDEX IF NOT EXISTS image_embeddings_hnsw
    ON image_embeddings USING hnsw (embedding public.vector_cosine_ops);
"""


def literal(vector: Vectors) -> str:
    """pgvector's text form, `[0.1,0.2,...]`, passed as a parameter and cast with `::vector`."""
    return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"


def fuse(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal rank fusion: score(id) = sum over rankings of 1 / (k + rank), best first.
    Ties break by ascending id so the result is deterministic."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


class VectorStore:
    def __init__(self, index: PgIndex) -> None:
        self._index = index
        self.available = self._install()

    def _install(self) -> bool:
        try:
            with psycopg.connect(self._index.dsn, autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            with self._index.connection() as conn:
                conn.execute(_SCHEMA)
        except psycopg.Error:
            log.warning("pgvector unavailable: semantic search is disabled", exc_info=True)
            return False
        return True

    # ------------------------------------------------------------------------------ writes

    def add_text(self, doc_id: int, vectors: Vectors) -> None:
        """Store (replace) the passage vectors of a document."""
        with self._index.connection() as conn:
            conn.execute("DELETE FROM text_embeddings WHERE doc_id = %s", (doc_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO text_embeddings (doc_id, chunk_no, embedding)"
                    " VALUES (%s, %s, %s::vector)",
                    [(doc_id, n, literal(v)) for n, v in enumerate(vectors)],
                )

    def add_image(self, asset_id: int, vector: Vectors) -> None:
        with self._index.connection() as conn:
            conn.execute(
                "INSERT INTO image_embeddings (asset_id, embedding) VALUES (%s, %s::vector)"
                " ON CONFLICT (asset_id) DO UPDATE SET embedding = EXCLUDED.embedding",
                (asset_id, literal(vector)),
            )

    def embedded_documents(self, doc_ids: Sequence[int]) -> set[int]:
        if not doc_ids:
            return set()
        with self._index.connection() as conn:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT doc_id FROM text_embeddings WHERE doc_id = ANY(%s)",
                    (list(doc_ids),),
                )
            }

    def has_image(self, asset_id: int) -> bool:
        with self._index.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM image_embeddings WHERE asset_id = %s", (asset_id,)
                ).fetchone()
                is not None
            )

    # ------------------------------------------------------------------------------- reads

    def search_text(self, query: Vectors, limit: int) -> list[tuple[int, float]]:
        """Live documents nearest to `query` as (doc id, cosine similarity), best first.

        The ANN index returns the nearest passages (over-fetched, since several passages can
        belong to one document and deleted documents are dropped afterwards)."""
        fetch = max(limit * 8, 40)
        with self._index.connection() as conn:
            conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(max(fetch, 40)),))
            rows = conn.execute(
                "SELECT doc_id, 1 - (embedding <=> %s::vector) AS similarity FROM text_embeddings"
                " ORDER BY embedding <=> %s::vector LIMIT %s",
                (literal(query), literal(query), fetch),
            ).fetchall()
            best: dict[int, float] = {}
            for doc_id, similarity in rows:
                best[doc_id] = max(best.get(doc_id, -1.0), float(similarity))
            live = {
                r[0]
                for r in conn.execute(
                    "SELECT id FROM documents WHERE id = ANY(%s) AND deleted_at IS NULL",
                    (list(best),),
                )
            }
        ranked = sorted(((d, s) for d, s in best.items() if d in live), key=lambda p: (-p[1], p[0]))
        return ranked[:limit]

    def search_images(self, query: Vectors, limit: int) -> list[tuple[int, float]]:
        """Image assets nearest to a CLIP text `query` as (asset id, similarity), best first."""
        fetch = max(limit * 4, 20)
        with self._index.connection() as conn:
            conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(max(fetch, 40)),))
            rows = conn.execute(
                "SELECT e.asset_id, 1 - (e.embedding <=> %s::vector) AS similarity"
                " FROM image_embeddings e JOIN assets a ON a.id = e.asset_id"
                " ORDER BY e.embedding <=> %s::vector LIMIT %s",
                (literal(query), literal(query), fetch),
            ).fetchall()
        return [(int(a), float(s)) for a, s in rows][:limit]

    def counts(self) -> dict[str, int]:
        with self._index.connection() as conn:
            text = conn.execute("SELECT count(DISTINCT doc_id) FROM text_embeddings").fetchone()
            images = conn.execute("SELECT count(*) FROM image_embeddings").fetchone()
        return {"documents": int(text[0]) if text else 0, "images": int(images[0]) if images else 0}
