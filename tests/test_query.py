import random
import string

import pytest

from docsearch.query import (
    MAX_NODES,
    AAnd,
    AFilter,
    And,
    ANot,
    AOr,
    APhrase,
    ATerm,
    Filter,
    Not,
    Or,
    Phrase,
    Word,
    analyse,
    parse,
)


def test_plain_words_are_an_implicit_and() -> None:
    assert parse("climate change") == And((Word("climate"), Word("change")))


def test_single_word_and_empty_input() -> None:
    assert parse("solar") == Word("solar")
    assert parse("") is None
    assert parse("   ") is None


def test_phrase() -> None:
    assert parse('"climate change"') == Phrase(("climate", "change"))
    assert parse('"solar"') == Word("solar")
    assert parse('""') is None


def test_unclosed_quote_runs_to_the_end() -> None:
    assert parse('policy "climate change') == And((Word("policy"), Phrase(("climate", "change"))))


def test_or_binds_looser_than_and() -> None:
    assert parse("a b OR c") == Or((And((Word("a"), Word("b"))), Word("c")))


def test_explicit_and_and_grouping() -> None:
    assert parse("(solar OR wind) AND nuclear") == And(
        (Or((Word("solar"), Word("wind"))), Word("nuclear"))
    )


@pytest.mark.parametrize("text", ["coal -nuclear", "coal NOT nuclear"])
def test_exclusion_forms(text: str) -> None:
    assert parse(text) == And((Word("coal"), Not(Word("nuclear"))))


def test_hyphenated_words_are_not_exclusions() -> None:
    assert parse("e-mail") == Word("e-mail")
    assert parse("- ") == Word("-")


def test_filters() -> None:
    assert parse("report type:pdf") == And((Word("report"), Filter("type", "pdf")))
    assert parse("lang:hi") == Filter("lang", "hi")
    assert parse("TYPE:PDF") == Filter("type", "PDF")
    assert parse("after:2025-01-01") == Filter("after", "2025-01-01")


def test_unknown_field_is_searched_as_words() -> None:
    assert parse("title:budget") == And((Word("title"), Word("budget")))
    assert parse("http://x.org") == And((Word("http"), Word("//x.org")))


def test_lowercase_operators_are_words() -> None:
    assert parse("cats and dogs") == And((Word("cats"), Word("and"), Word("dogs")))


@pytest.mark.parametrize(
    "text",
    ["a OR", "OR a", "NOT", "a AND", "(", ")", "((a)", "a))", "- -a", "()", "a OR OR b", '"', "-"],
)
def test_malformed_input_never_raises(text: str) -> None:
    parse(text)  # must not raise; the result is whatever is searchable


def test_dangling_operators_are_dropped() -> None:
    assert parse("a OR") == Word("a")
    assert parse("a AND") == Word("a")
    assert parse("NOT") is None
    assert parse("a OR OR b") == Or((Word("a"), Word("b")))


def test_unbalanced_parentheses_are_tolerated() -> None:
    assert parse("(a OR b") == Or((Word("a"), Word("b")))
    assert parse("a) b") == And((Word("a"), Word("b")))


def test_fuzz_never_raises_and_stays_bounded() -> None:
    rng = random.Random(1234)
    alphabet = string.ascii_letters + string.digits + ' "()-:' + "ANDORT" + "क"
    for _ in range(3000):
        text = "".join(rng.choices(alphabet, k=rng.randint(0, 60)))
        node = parse(text)
        assert _size(node) <= MAX_NODES + 1


def test_hostile_queries_are_bounded() -> None:
    assert _size(parse(" ".join(f"w{n}" for n in range(5000)))) <= MAX_NODES + 1
    parse("(" * 10_000 + "a" + ")" * 10_000)  # nesting is capped, no recursion error
    parse("NOT " * 10_000 + "a")


def _size(node: object) -> int:
    if node is None:
        return 0
    children = getattr(node, "children", None)
    if children is not None:
        return 1 + sum(_size(c) for c in children)
    child = getattr(node, "child", None)
    return 1 + _size(child) if child is not None else 1


# ------------------------------------------------------------------------------- analysis


def test_analyse_applies_the_index_analyzer() -> None:
    assert analyse(parse("running dogs")) == AAnd((ATerm("run"), ATerm("dog")))


def test_stop_words_vanish_and_single_term_phrases_collapse() -> None:
    assert analyse(parse("the")) is None
    assert analyse(parse('"the dogs"')) == ATerm("dog")
    assert analyse(parse('"running the dogs"')) == APhrase(("run", "dog"))


def test_not_of_nothing_is_dropped_not_everything() -> None:
    assert analyse(parse("dogs -the")) == ATerm("dog")


def test_analyse_keeps_structure_and_filters() -> None:
    node = analyse(parse("(cats OR dogs) -fish type:pdf"))
    assert node == AAnd(
        (AOr((ATerm("cat"), ATerm("dog"))), ANot(ATerm("fish")), AFilter("type", "pdf"))
    )


def test_analyse_hindi() -> None:
    assert analyse(parse("किताबें")) == ATerm("किताब")


def test_analyse_none() -> None:
    assert analyse(None) is None
