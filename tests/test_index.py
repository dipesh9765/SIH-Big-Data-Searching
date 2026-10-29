import math
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from docsearch.analysis import term_frequencies
from docsearch.engine.scoring import BM25_B, BM25_K1, bm25_idf, bm25_term_score
from docsearch.index import EmptyDocumentError, Index, make_snippet


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    with Index(tmp_path / "idx.db") as idx:
        yield idx


def add(index: Index, text: str, title: str = "t") -> int:
    return index.add(
        title=title, source_name=f"{title}.txt", media_type="text/plain", text=text
    ).document.id


def test_idf_matches_the_formula_and_is_never_negative() -> None:
    assert bm25_idf(10, 2) == pytest.approx(math.log(1 + 8.5 / 2.5))
    assert bm25_idf(1, 1) > 0  # a term in every document still has a positive weight


def test_term_score_matches_hand_calculation() -> None:
    # tf=3, |d|=8, avgdl=10 -> norm = 0.25 + 0.75 * 0.8 = 0.85
    expected = 2.0 * 3 * (BM25_K1 + 1) / (3 + BM25_K1 * (1 - BM25_B + BM25_B * 0.8))
    assert bm25_term_score(2.0, 3, 8, 10.0) == pytest.approx(expected)


def test_search_score_equals_hand_computed_bm25(index: Index) -> None:
    add(index, "apple apple banana", "a")  # length 3
    add(index, "banana cherry", "b")  # length 2
    add(index, "cherry cherry cherry cherry", "c")  # length 4
    result = index.search("apple")
    avg = (3 + 2 + 4) / 3
    assert result.total == 1
    assert result.hits[0].score == pytest.approx(bm25_term_score(bm25_idf(3, 1), 2, 3, avg))


def test_matches_a_brute_force_scorer(index: Index) -> None:
    texts = [
        "the cat sat on the mat",
        "cats and dogs are pets and cats purr",
        "dogs chase cats around the garden",
        "gardens need water and sun",
        "a dog is a loyal pet",
    ]
    ids = [add(index, t, f"d{i}") for i, t in enumerate(texts)]
    docs = {i: term_frequencies(t) for i, t in zip(ids, texts, strict=True)}
    n = len(docs)
    avg = sum(length for _, length in docs.values()) / n
    for query in ("cat", "cat dog", "garden water", "dog pet"):
        terms = list(term_frequencies(query)[0])
        expected = {}
        for doc_id, (counts, length) in docs.items():
            if not any(t in counts for t in terms):
                continue
            expected[doc_id] = sum(
                bm25_term_score(
                    bm25_idf(n, sum(1 for c, _ in docs.values() if t in c)), counts[t], length, avg
                )
                for t in terms
                if t in counts
            )
        got = {h.document.id: h.score for h in index.search(query, mode="any", limit=50).hits}
        assert got.keys() == expected.keys()
        for doc_id, score in expected.items():
            assert got[doc_id] == pytest.approx(score)


def test_ranks_more_relevant_and_shorter_document_first(index: Index) -> None:
    filler = " ".join(f"filler{i}" for i in range(50))
    long_id = add(index, f"python {filler}", "long")
    short_id = add(index, "python tutorial", "short")
    hits = index.search("python").hits
    assert [h.document.id for h in hits] == [short_id, long_id]


def test_all_mode_requires_every_term_any_mode_requires_one(index: Index) -> None:
    both = add(index, "red green", "both")
    only_red = add(index, "red blue", "red")
    assert [h.document.id for h in index.search("red green", mode="all").hits] == [both]
    any_ids = {h.document.id for h in index.search("red green", mode="any").hits}
    assert any_ids == {both, only_red}
    assert index.search("red unseenword", mode="all").total == 0
    assert index.search("red unseenword", mode="any").total == 2


def test_stemmed_query_matches_inflected_text(index: Index) -> None:
    doc = add(index, "She was running quickly")
    assert [h.document.id for h in index.search("runs").hits] == [doc]


def test_pagination_and_deterministic_ties(index: Index) -> None:
    ids = [add(index, f"shared word{i}", f"d{i}") for i in range(5)]
    first = index.search("shared", limit=2)
    second = index.search("shared", limit=2, offset=2)
    assert first.total == 5
    ranked = [h.document.id for h in first.hits + second.hits]
    assert ranked == ids[:4]  # equal scores fall back to ascending id


def test_empty_and_stop_word_queries_return_nothing(index: Index) -> None:
    add(index, "hello world")
    assert index.search("").total == 0
    assert index.search("the and of").total == 0
    assert Index.search(index, "hello").total == 1


def test_search_on_empty_index(index: Index) -> None:
    assert index.search("anything").hits == []


def test_duplicate_text_is_indexed_once(index: Index) -> None:
    first = index.add(
        title="a", source_name="a.txt", media_type="text/plain", text="same text here"
    )
    second = index.add(
        title="b", source_name="b.txt", media_type="text/plain", text="same text here"
    )
    assert not first.duplicate
    assert second.duplicate
    assert second.document.id == first.document.id
    assert index.stats().documents == 1


def test_unindexable_text_is_rejected(index: Index) -> None:
    with pytest.raises(EmptyDocumentError):
        index.add(title="x", source_name="x", media_type="text/plain", text="the of and !!!")
    assert index.stats().documents == 0


def test_delete_removes_document_postings_and_orphan_terms(index: Index) -> None:
    keep = add(index, "alpha beta")
    gone = add(index, "beta gamma")
    assert index.delete(gone) is True
    assert index.delete(gone) is False
    assert index.get(gone) is None
    assert index.search("gamma").total == 0
    assert [h.document.id for h in index.search("beta").hits] == [keep]
    stats = index.stats()
    assert (stats.documents, stats.terms, stats.postings) == (1, 2, 2)  # alpha, beta only


def test_statistics_stay_consistent_after_mixed_operations(index: Index) -> None:
    ids = [add(index, f"term{i} common words here", f"d{i}") for i in range(6)]
    for doc_id in ids[::2]:
        index.delete(doc_id)
    with closing(sqlite3.connect(index.path)) as conn:
        doc_count, total = conn.execute(
            "SELECT (SELECT value FROM meta WHERE key='doc_count'),"
            " (SELECT value FROM meta WHERE key='total_length')"
        ).fetchone()
        actual_total = conn.execute("SELECT COALESCE(SUM(length), 0) FROM documents").fetchone()[0]
        bad_df = conn.execute(
            "SELECT COUNT(*) FROM terms t WHERE t.doc_freq !="
            " (SELECT COUNT(*) FROM postings p WHERE p.term_id = t.id)"
        ).fetchone()[0]
    assert (int(doc_count), int(total)) == (3, actual_total)
    assert bad_df == 0


def test_data_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    doc = add(Index(path), "durable persistence test")
    reopened = Index(path)
    assert [h.document.id for h in reopened.search("durable").hits] == [doc]


def test_failed_write_rolls_back_everything(index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(Index, "_bump_stats", staticmethod(boom))
    with pytest.raises(RuntimeError):
        add(index, "will not be stored")
    monkeypatch.undo()
    assert index.stats() == index.stats().__class__(0, 0, 0, 0.0)
    assert index.list_documents() == []


def test_concurrent_writers_do_not_lose_documents(index: Index) -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(10):
                add(index, f"worker{n} document{i} shared", f"w{n}-{i}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert index.stats().documents == 40
    assert index.search("shared", limit=100).total == 40


def test_list_documents_is_newest_first_and_paged(index: Index) -> None:
    ids = [add(index, f"unique{i} text", f"d{i}") for i in range(4)]
    assert [d.id for d in index.list_documents(limit=2)] == [ids[3], ids[2]]
    assert [d.id for d in index.list_documents(limit=2, offset=2)] == [ids[1], ids[0]]


def test_snippet_centres_on_matches_and_reports_offsets() -> None:
    text = "intro " * 40 + "the needle is here " + "outro " * 40
    snippet, highlights = make_snippet(text, {"needl"})
    assert "needle" in snippet
    assert snippet.startswith("… ")
    assert snippet.endswith(" …")
    assert [snippet[s:e] for s, e in highlights] == ["needle"]


def test_snippet_prefers_the_densest_cluster() -> None:
    text = "apple " + "x " * 200 + "apple pie apple tart apple"
    snippet, highlights = make_snippet(text, {"appl"})
    assert len(highlights) == 3
    assert "pie" in snippet


def test_snippet_without_matches_falls_back_to_the_start() -> None:
    snippet, highlights = make_snippet("   start of the document " + "z" * 500, {"nothing"})
    assert snippet.startswith("start of")
    assert highlights == []


def test_hindi_documents_are_found_by_inflected_queries_and_highlighted(index: Index) -> None:
    add(index, "भारत की राजधानी नई दिल्ली है। वहाँ बहुत सी किताबें मिलती हैं।", "hi")
    add(index, "Mumbai is the financial capital of India", "en")
    result = index.search("किताब")
    assert [h.document.title for h in result.hits] == ["hi"]
    hit = result.hits[0]
    assert [hit.snippet[s:e] for s, e in hit.highlights] == ["किताबें"]
    # A mixed-script query reaches each document through its own pipeline.
    assert {h.document.title for h in index.search("capital राजधानी", mode="any").hits} == {
        "hi",
        "en",
    }
