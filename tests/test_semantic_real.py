"""The real embedding models (container only: they live in the worker image, offline).

The sets here are tiny on purpose: they show the models and the plumbing work together, they are
not a retrieval benchmark.
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator

import numpy as np
import pytest
from PIL import Image, ImageDraw

from docsearch.engine.pg import PgIndex
from docsearch.engine.vectors import VectorStore
from docsearch.ingest.objects import MemoryStore
from docsearch.ingest.pipeline import submit
from docsearch.ingest.worker import Worker
from docsearch.semantic import FastEmbedder

CACHE = os.environ.get("DOCSEARCH_EMBED_CACHE", "")
pytestmark = pytest.mark.skipif(
    not CACHE or not os.path.isdir(CACHE), reason="embedding models are not installed here"
)

DOCS = {
    "glaciers": "Himalayan glaciers are retreating rapidly because of rising temperatures.",
    "tax": "How to file your annual income tax return before the deadline.",
    "wind": "Offshore wind farms generate electricity from strong sea breezes.",
    "monsoon": "Heavy monsoon rainfall caused flooding across several districts this week.",
    "vaccine": "The new vaccine was approved after large clinical trials showed it was safe.",
    "cricket": "The cricket team won the match by five wickets in the final over.",
    "volcano": "A volcanic eruption sent a cloud of ash high into the atmosphere.",
    "bank": "The central bank raised interest rates to control inflation.",
}
QUERIES = [
    ("hi", "हिमालय के ग्लेशियर पिघल रहे हैं", "glaciers"),
    ("hi", "भारी बारिश से बाढ़ आई", "monsoon"),
    ("hi", "क्रिकेट मैच में जीत", "cricket"),
    ("mr", "ज्वालामुखीचा उद्रेक आणि राख", "volcano"),
    ("mr", "व्याजदर वाढवले महागाई नियंत्रणासाठी", "bank"),
    ("en", "ice sheets melting in the mountains", "glaciers"),
    ("en", "paying taxes online", "tax"),
    ("en", "clean energy from the ocean", "wind"),
    ("en", "is the shot safe", "vaccine"),
]


@pytest.fixture(scope="module")
def embedder() -> FastEmbedder:
    return FastEmbedder(CACHE)


def test_vectors_are_normalised_and_the_right_size(embedder: FastEmbedder) -> None:
    vectors = embedder.embed_texts(["hello world", "नमस्ते दुनिया"])
    assert vectors.shape == (2, 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)


def test_hindi_marathi_and_english_queries_find_english_passages(embedder: FastEmbedder) -> None:
    keys = list(DOCS)
    matrix = embedder.embed_texts(list(DOCS.values()))
    first = third = 0
    per_language: dict[str, list[bool]] = {}
    for language, query, wanted in QUERIES:
        order = [keys[i] for i in np.argsort(-(matrix @ embedder.embed_query(query)))]
        first += order[0] == wanted
        third += wanted in order[:3]
        per_language.setdefault(language, []).append(order[0] == wanted)
    assert first >= 7, f"top-1 {first}/{len(QUERIES)}"
    assert third >= 8, f"top-3 {third}/{len(QUERIES)}"
    assert sum(per_language["hi"]) >= 2  # cross-lingual: Hindi asks, English answers


def picture(draw) -> bytes:  # type: ignore[no-untyped-def]
    image = Image.new("RGB", (224, 224), "white")
    draw(ImageDraw.Draw(image))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


PICTURES = {
    "red circle": picture(lambda d: d.ellipse((40, 40, 184, 184), fill="red")),
    "blue square": picture(lambda d: d.rectangle((40, 40, 184, 184), fill="blue")),
    "green triangle": picture(
        lambda d: d.polygon([(112, 30), (30, 190), (194, 190)], fill="green")
    ),
    "yellow stripes": picture(
        lambda d: [d.rectangle((0, y, 224, y + 20), fill="yellow") for y in range(0, 224, 40)]
    ),
}


def test_pictures_are_found_by_describing_them(embedder: FastEmbedder) -> None:
    names = list(PICTURES)
    matrix = embedder.embed_images(list(PICTURES.values()))
    assert matrix.shape == (4, 512)
    for name in names:
        best = names[int(np.argmax(matrix @ embedder.embed_image_query(f"a {name}")))]
        assert best == name


psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get(
    "DOCSEARCH_TEST_DSN", "postgresql://docsearch:docsearch_dev@127.0.0.1:5433/docsearch"
)


@pytest.fixture
def index() -> Iterator[PgIndex]:
    pg = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    try:
        yield pg
    finally:
        pg.close()
        pg.drop_schema()


def test_a_hindi_question_retrieves_an_english_upload_end_to_end(
    index: PgIndex, embedder: FastEmbedder
) -> None:
    vectors = VectorStore(index)
    store = MemoryStore()
    for name, text in DOCS.items():
        submit(index, store, name=f"{name}.txt", data=text.encode())
    Worker(index, store, worker_id="t", batch_size=20, enqueue_embeddings=True).run_once()
    Worker(
        index, store, worker_id="e", kinds=("embed",), batch_size=20, embedder=embedder
    ).run_once()
    assert vectors.counts()["documents"] == len(DOCS)
    found = vectors.search_text(embedder.embed_query("हिमालय के ग्लेशियर पिघल रहे हैं"), 3)
    top = index.hits_for(found[:1], set())[0]
    assert top.document.source_name == "glaciers.txt"
