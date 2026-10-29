"""The block engine must rank exactly like the SQLite reference index.

The SQLite `Index` scores every posting of every query term, so it is the oracle: if block-max
pruning ever drops a document that belongs in the top-k, or orders ties differently, these tests
fail. Corpora are generated from a seeded Zipf-like vocabulary so common and rare terms both occur.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from docsearch.analysis import query_terms
from docsearch.engine.codec import (
    BLOCK_SIZE,
    build_blocks,
    decode_varints,
    encode_block,
    encode_varints,
    iter_postings,
)
from docsearch.engine.memory import MemoryIndex
from docsearch.engine.search import Mode, search
from docsearch.index import Index

# ------------------------------------------------------------------------------ codec


@pytest.mark.parametrize("value", [0, 1, 127, 128, 16383, 16384, 2**31, 2**62])
def test_varint_round_trip(value: int) -> None:
    data = encode_varints([value])
    assert decode_varints(bytes(data), 1) == ([value], len(data))


def test_varint_small_values_take_one_byte() -> None:
    assert len(encode_varints(range(128))) == 128


def test_varint_rejects_negative_and_truncated_input() -> None:
    with pytest.raises(ValueError, match="unsigned"):
        encode_varints([-1])
    with pytest.raises(ValueError, match="truncated"):
        decode_varints(bytes([0x80]), 1)
    with pytest.raises(ValueError, match="too long"):
        decode_varints(bytes([0xFF] * 12), 1)


def test_block_round_trip_and_metadata() -> None:
    block = encode_block([3, 9, 10, 500], [1, 4, 2, 1], [20, 5, 8, 30])
    assert (block.first_doc, block.last_doc, block.count) == (3, 500, 4)
    assert (block.max_tf, block.min_len) == (4, 5)
    assert block.decode() == ([3, 9, 10, 500], [1, 4, 2, 1])


def test_block_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="strictly ascending"):
        encode_block([2, 2], [1, 1], [1, 1])
    with pytest.raises(ValueError, match="at least 1"):
        encode_block([1], [0], [1])
    with pytest.raises(ValueError, match="1\\.\\."):
        encode_block([], [], [])
    with pytest.raises(ValueError, match="same length"):
        encode_block([1, 2], [1], [1, 1])


def test_corrupt_block_is_detected() -> None:
    block = encode_block([1, 2, 3], [1, 1, 1], [1, 1, 1])
    lying = type(block)(1, 99, 3, 1, 1, block.payload)
    with pytest.raises(ValueError, match="does not match"):
        lying.decode()
    trailing = type(block)(1, 3, 3, 1, 1, block.payload + b"\x01")
    with pytest.raises(ValueError, match="trailing"):
        trailing.decode()


def test_build_blocks_splits_at_block_size_and_preserves_order() -> None:
    postings = [(i * 3, (i % 5) + 1) for i in range(BLOCK_SIZE * 2 + 7)]
    blocks = build_blocks(postings)
    assert [b.count for b in blocks] == [BLOCK_SIZE, BLOCK_SIZE, 7]
    assert list(iter_postings(blocks)) == postings


# --------------------------------------------------------------------------- fixtures


def make_corpus(seed: int, documents: int, vocabulary: int = 300) -> list[str]:
    rng = random.Random(seed)
    words = [f"w{n}x" for n in range(vocabulary)]
    weights = [1.0 / (rank + 1) for rank in range(vocabulary)]
    corpus = []
    for _ in range(documents):
        length = rng.randint(3, 60)
        corpus.append(" ".join(rng.choices(words, weights, k=length)))
    return corpus


def build(tmp_path: Path, corpus: list[str]) -> tuple[Index, MemoryIndex]:
    reference = Index(tmp_path / "ref.db")
    memory = MemoryIndex()
    for position, text in enumerate(corpus):
        added = reference.add(
            title=str(position), source_name="t.txt", media_type="text/plain", text=text
        )
        if not added.duplicate:
            memory.add(added.document.id, text)
    memory.finish()
    return reference, memory


def assert_same_ranking(
    reference: Index, memory: MemoryIndex, query: str, mode: Mode, k: int
) -> None:
    terms = query_terms(query)
    expected = reference.search(query, mode=mode, limit=k)
    got = search(memory, terms, mode=mode, k=k)
    assert [h.doc_id for h in got.hits] == [h.document.id for h in expected.hits], query
    for mine, theirs in zip(got.hits, expected.hits, strict=True):
        assert mine.score == pytest.approx(theirs.score, rel=1e-9), query


_MODES: tuple[Mode, Mode] = ("any", "all")

# ---------------------------------------------------------------- differential tests


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("mode", ["any", "all"])
def test_matches_reference_on_random_queries(tmp_path: Path, seed: int, mode: Mode) -> None:
    corpus = make_corpus(seed, documents=700)  # several blocks per common term
    reference, memory = build(tmp_path, corpus)
    rng = random.Random(seed + 100)
    vocab = [f"w{n}x" for n in range(300)]
    weights = [1.0 / (rank + 1) for rank in range(300)]
    for _ in range(60):
        query = " ".join(rng.choices(vocab, weights, k=rng.randint(1, 4)))
        for k in (1, 5, 10, 50):
            assert_same_ranking(reference, memory, query, mode, k)
    reference.close()


def test_exact_total_matches_reference(tmp_path: Path) -> None:
    reference, memory = build(tmp_path, make_corpus(7, documents=500))
    for query in ("w0x w1x", "w5x", "w2x w9x w40x"):
        for mode in _MODES:
            terms = query_terms(query)
            expected = reference.search(query, mode=mode, limit=5)
            got = search(memory, terms, mode=mode, k=5, exact_total=True)
            assert got.total == expected.total
            assert [h.doc_id for h in got.hits] == [h.document.id for h in expected.hits]
    reference.close()


def test_unknown_terms(tmp_path: Path) -> None:
    reference, memory = build(tmp_path, make_corpus(4, documents=50))
    assert search(memory, ["nope"], mode="any").hits == []
    assert search(memory, ["w0x", "nope"], mode="all").hits == []
    # OR ignores the unknown term and still ranks on the known one.
    assert_same_ranking(reference, memory, "w0x nope", "any", 5)
    assert search(memory, [], mode="any").hits == []
    reference.close()


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        search(MemoryIndex(), ["a"], k=0)


def test_empty_index_returns_nothing() -> None:
    assert search(MemoryIndex(), ["a"]).hits == []


def test_pruning_skips_work_but_not_results(tmp_path: Path) -> None:
    """The point of block-max: same answer, fewer postings scored."""
    corpus = make_corpus(11, documents=3000, vocabulary=200)
    reference, memory = build(tmp_path, corpus)
    terms = query_terms("w0x w1x w2x")
    pruned = search(memory, terms, mode="any", k=10)
    exhaustive = search(memory, terms, mode="any", k=10, exact_total=True)
    assert [h.doc_id for h in pruned.hits] == [h.doc_id for h in exhaustive.hits]
    assert pruned.stats.postings_scored < exhaustive.stats.postings_scored
    assert exhaustive.stats.postings_scored == exhaustive.total
    reference.close()


def test_ties_break_by_ascending_document_id() -> None:
    memory = MemoryIndex()
    for doc_id in range(1, 400):
        memory.add(doc_id, "alpha beta")  # identical text: identical scores
    memory.finish()
    hits = search(memory, ["alpha"], mode="any", k=5).hits
    assert [h.doc_id for h in hits] == [1, 2, 3, 4, 5]
    hits = search(memory, ["alpha", "beta"], mode="all", k=5).hits
    assert [h.doc_id for h in hits] == [1, 2, 3, 4, 5]


def test_memory_index_rejects_duplicate_ids_and_skips_empty_documents() -> None:
    memory = MemoryIndex()
    memory.add(1, "hello world")
    memory.add(2, "the of and")  # only stop words: nothing indexable
    with pytest.raises(ValueError, match="already indexed"):
        memory.add(1, "different words")
    memory.finish()
    assert memory.doc_count == 1
    assert memory.payload_bytes() > 0
    assert memory.posting_count() == 2
