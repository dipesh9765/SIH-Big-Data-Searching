from docsearch.analysis import STOP_WORDS, query_terms, term_frequencies, tokenize


def test_stems_inflections_to_one_term() -> None:
    assert query_terms("running runs run") == ["run"]


def test_drops_stop_words_and_keeps_order_of_distinct_terms() -> None:
    assert query_terms("The quick brown fox and the lazy dog") == [
        "quick",
        "brown",
        "fox",
        "lazi",
        "dog",
    ]


def test_is_case_insensitive_and_unicode_aware() -> None:
    assert query_terms("ÉCOLE École") == ["école"]


def test_apostrophes_stay_inside_words_and_stop_forms_are_dropped() -> None:
    assert "don't" in STOP_WORDS
    assert query_terms("don't stop o'clock") == ["stop", "o'clock"]
    assert query_terms("don" + chr(0x2019) + "t") == []  # typographic apostrophe is normalised


def test_underscores_and_punctuation_split_tokens() -> None:
    assert query_terms("snake_case, kebab-case;CSV") == ["snake", "case", "kebab", "csv"]


def test_overlong_tokens_are_ignored() -> None:
    assert query_terms("a" * 41 + " ok") == ["ok"]


def test_offsets_point_at_the_source_word() -> None:
    text = "Dogs  chase cats"
    spans = {t.term: text[t.start : t.end] for t in tokenize(text)}
    assert spans == {"dog": "Dogs", "chase": "chase", "cat": "cats"}


def test_term_frequencies_count_after_stemming() -> None:
    counts, total = term_frequencies("run runs running the walk")
    assert counts == {"run": 3, "walk": 1}
    assert total == 4


def test_empty_and_symbol_only_text() -> None:
    assert tokenize("") == []
    assert tokenize("!!! ??? ---") == []
    assert term_frequencies("the and of") == ({}, 0)
