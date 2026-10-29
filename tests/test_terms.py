import random
import string

import pytest

from docsearch.engine.terms import (
    MAX_EDITS,
    TermDictionary,
    edit_distance,
    max_edits_for,
)

# ------------------------------------------------------------------------------ distance


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("kitten", "kitten", 0),
        ("kitten", "sitten", 1),  # substitution
        ("kitten", "kittn", 1),  # deletion
        ("kitten", "kittens", 1),  # insertion
        ("kitten", "ktiten", 1),  # adjacent swap counts once
        ("kitten", "sittin", 2),
        ("", "ab", 2),
        ("ab", "", 2),
        ("", "", 0),
        ("किताब", "किताबे", 1),
    ],
)
def test_edit_distance_values(a: str, b: str, expected: int) -> None:
    assert edit_distance(a, b, limit=5) == expected


def test_edit_distance_respects_the_limit() -> None:
    assert edit_distance("kitten", "sitting", limit=2) is None
    assert edit_distance("kitten", "sitting", limit=3) == 3
    assert edit_distance("a", "abcdef", limit=2) is None  # length gap alone exceeds it


def _reference_osa(a: str, b: str) -> int:
    """Plain full-matrix optimal string alignment, as an independent oracle."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def test_edit_distance_matches_the_full_matrix_oracle() -> None:
    rng = random.Random(5)
    for _ in range(2000):
        a = "".join(rng.choices("abc", k=rng.randint(0, 7)))
        b = "".join(rng.choices("abc", k=rng.randint(0, 7)))
        true = _reference_osa(a, b)
        for limit in (0, 1, 2, 3):
            expected = true if true <= limit else None
            assert edit_distance(a, b, limit) == expected, (a, b, limit)


def test_typo_budget_grows_with_length() -> None:
    assert [max_edits_for("a" * n) for n in (1, 3, 4, 7, 8, 20)] == [0, 0, 1, 1, MAX_EDITS, 2]


# ----------------------------------------------------------------------------- dictionary


@pytest.fixture
def dictionary() -> TermDictionary:
    return TermDictionary(
        {
            "search": 50,
            "searches": 5,
            "seat": 9,
            "season": 20,
            "engine": 30,
            "index": 40,
            "indexes": 2,
        }
    )


def test_complete_ranks_by_frequency_then_alphabet(dictionary: TermDictionary) -> None:
    assert [s.term for s in dictionary.complete("sea")] == ["search", "season", "seat", "searches"]
    assert [s.term for s in dictionary.complete("sea", limit=2)] == ["search", "season"]
    assert dictionary.complete("zzz") == []
    assert dictionary.complete("") == []
    assert dictionary.complete("sea", limit=0) == []


def test_fuzzy_finds_typos_and_ranks_by_distance_then_frequency(dictionary: TermDictionary) -> None:
    assert dictionary.fuzzy("serch")[0].term == "search"  # one deletion
    assert dictionary.fuzzy("saerch")[0].term == "search"  # transposition
    assert dictionary.fuzzy("search")[0].distance == 0
    top = dictionary.fuzzy("indx")
    assert top[0].term == "index"
    assert all(s.distance <= 1 for s in top)


def test_fuzzy_leaves_short_words_alone(dictionary: TermDictionary) -> None:
    assert dictionary.fuzzy("sat") == []  # budget 0 for 3 letters: "seat" is not offered


def test_fuzzy_unknown_word_with_nothing_close(dictionary: TermDictionary) -> None:
    assert dictionary.fuzzy("qqqqqqqq") == []


def test_fuzzy_has_full_recall_against_brute_force() -> None:
    rng = random.Random(21)
    words = {
        "".join(rng.choices(string.ascii_lowercase[:6], k=rng.randint(3, 10))): rng.randint(1, 99)
        for _ in range(400)
    }
    dictionary = TermDictionary(words)
    for _ in range(300):
        query = "".join(rng.choices(string.ascii_lowercase[:6], k=rng.randint(2, 10)))
        budget = max_edits_for(query)
        expected = {
            w for w in words if budget > 0 and edit_distance(query, w, budget) is not None
        } | ({query} if query in words else set())
        got = {s.term for s in dictionary.fuzzy(query, limit=10_000)}
        assert got == expected, (query, got ^ expected)


def test_len_and_doc_freq(dictionary: TermDictionary) -> None:
    assert len(dictionary) == 7
    assert dictionary.doc_freq("search") == 50
    assert dictionary.doc_freq("nope") == 0
    assert TermDictionary.from_pairs([("a", 1)]).doc_freq("a") == 1
