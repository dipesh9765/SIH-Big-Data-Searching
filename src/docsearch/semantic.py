"""Text and image embeddings for semantic search.

* Text: `paraphrase-multilingual-MiniLM-L12-v2` (384-d, trained on 50+ languages including Hindi and
  Marathi), so a query in one language can find documents in another.
* Images: CLIP ViT-B/32. The *vision* tower embeds images, the *text* tower embeds a query, and both
  land in one 512-d space, so words can find pictures. CLIP's text tower is English-centric.

All vectors are L2-normalised float32, so cosine similarity is a dot product. Models run on CPU
through ONNX (fastembed) and are loaded on first use; the worker image ships them, so nothing is
downloaded at run time.
"""

from __future__ import annotations

import hashlib
import io
import re
import threading
from collections.abc import Sequence
from typing import Any, ClassVar, Protocol, cast

import numpy as np
import numpy.typing as npt

TEXT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CLIP_TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
CLIP_IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"
TEXT_DIM = 384
IMAGE_DIM = 512

CHUNK_CHARS = 700  # a passage this long fits the text model's 128-token window in most languages
MAX_CHUNKS = 16  # per document: the first ~11,000 characters are embedded

Vectors = npt.NDArray[np.float32]


class Embedder(Protocol):
    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        """Passage embeddings, shape (n, TEXT_DIM)."""
        ...

    def embed_query(self, text: str) -> Vectors:
        """One query embedding, shape (TEXT_DIM,)."""
        ...

    def embed_images(self, images: Sequence[bytes]) -> Vectors:
        """Embeddings of encoded images (PNG, JPEG, ...), shape (n, IMAGE_DIM)."""
        ...

    def embed_image_query(self, text: str) -> Vectors:
        """A text query in the image space (CLIP text tower), shape (IMAGE_DIM,)."""
        ...


def chunk_text(text: str, size: int = CHUNK_CHARS, limit: int = MAX_CHUNKS) -> list[str]:
    """Split `text` into passages of about `size` characters, breaking at sentence ends or spaces,
    at most `limit` of them (the start of a long document represents it)."""
    text = re.sub(r"\s+", " ", text).strip()
    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < limit:
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind(". ", start, end), text.rfind("। ", start, end))
            if cut <= start + size // 2:
                cut = text.rfind(" ", start, end)
            if cut > start:
                end = cut + 1
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]


def normalise(vectors: npt.NDArray[np.floating]) -> Vectors:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return np.asarray(vectors / np.maximum(norms, 1e-12), dtype=np.float32)


class FastEmbedder:
    """The real models (fastembed/ONNX), loaded on first use."""

    def __init__(self, cache_dir: str | None = None) -> None:
        self._cache = cache_dir or None
        self._lock = threading.Lock()
        self._models: dict[str, Any] = {}

    def _model(self, name: str, *, image: bool = False) -> Any:
        with self._lock:
            if name not in self._models:
                from fastembed import ImageEmbedding, TextEmbedding  # noqa: PLC0415

                cls = ImageEmbedding if image else TextEmbedding
                self._models[name] = cls(name, cache_dir=self._cache)
            return self._models[name]

    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        return normalise(np.array(list(self._model(TEXT_MODEL).embed(list(texts)))))

    def embed_query(self, text: str) -> Vectors:
        return cast(Vectors, self.embed_texts([text])[0])

    def embed_images(self, images: Sequence[bytes]) -> Vectors:
        from PIL import Image  # noqa: PLC0415

        pictures = [Image.open(io.BytesIO(data)).convert("RGB") for data in images]
        model = self._model(CLIP_IMAGE_MODEL, image=True)
        return normalise(np.array(list(model.embed(pictures))))

    def embed_image_query(self, text: str) -> Vectors:
        model = self._model(CLIP_TEXT_MODEL)
        return cast(Vectors, normalise(np.array(list(model.embed([text]))))[0])


class HashingEmbedder:
    """A deterministic stand-in for tests: character-trigram hashing for text, colour histograms
    for images. It has no semantics; it exists so storage, ranking and the pipeline can be tested
    without downloading models. Texts that share words are close, which is enough for that."""

    COLOURS: ClassVar[dict[str, tuple[int, int, int]]] = {
        "red": (255, 0, 0),
        "green": (0, 255, 0),
        "blue": (0, 0, 255),
        "yellow": (255, 255, 0),
        "black": (0, 0, 0),
        "white": (255, 255, 255),
    }

    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        out = np.zeros((len(texts), TEXT_DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            padded = f"  {text.casefold()}  "
            for i in range(len(padded) - 2):
                digest = hashlib.blake2b(padded[i : i + 3].encode(), digest_size=4).digest()
                out[row, int.from_bytes(digest, "big") % TEXT_DIM] += 1.0
        return normalise(out)

    def embed_query(self, text: str) -> Vectors:
        return cast(Vectors, self.embed_texts([text])[0])

    def _colour_vector(self, rgb: tuple[float, float, float]) -> Vectors:
        vector = np.full(IMAGE_DIM, 0.01, dtype=np.float32)
        for index, anchor in enumerate(self.COLOURS.values()):
            distance = sum((a - b) ** 2 for a, b in zip(rgb, anchor, strict=True)) ** 0.5
            vector[index] = max(0.0, 1.0 - distance / 441.7)
        return normalise(vector)

    def embed_images(self, images: Sequence[bytes]) -> Vectors:
        from PIL import Image, ImageStat  # noqa: PLC0415

        rows = []
        for data in images:
            mean = ImageStat.Stat(Image.open(io.BytesIO(data)).convert("RGB")).mean
            rows.append(self._colour_vector((mean[0], mean[1], mean[2])))
        return np.array(rows, dtype=np.float32)

    def embed_image_query(self, text: str) -> Vectors:
        for word in text.casefold().split():
            if word in self.COLOURS:
                r, g, b = self.COLOURS[word]
                return self._colour_vector((float(r), float(g), float(b)))
        return normalise(np.full(IMAGE_DIM, 0.01, dtype=np.float32))
