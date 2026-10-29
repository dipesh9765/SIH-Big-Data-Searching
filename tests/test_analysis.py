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


# ------------------------------------------------------------------ Devanagari (Hindi, Marathi)


def test_devanagari_words_are_not_split_at_vowel_signs() -> None:
    # Python's \w ignores combining marks; the old tokenizer turned "किताबें" into क / त / ब.
    assert [t.term for t in tokenize("किताबें")] == ["किताब"]
    assert [t.term for t in tokenize("भारत की राजधानी")] == ["भारत", "राजधान"]


def test_hindi_inflections_share_a_term() -> None:
    assert query_terms("किताबें") == query_terms("किताब")
    assert query_terms("लड़कियाँ") == query_terms("लड़की")


def test_hindi_and_marathi_stop_words_are_dropped() -> None:
    assert query_terms("का के की में है") == []
    assert query_terms("आहे आणि नाही") == []


def test_precomposed_and_decomposed_nukta_forms_match() -> None:
    assert query_terms("ढ़") == query_terms("ढ़")


def test_devanagari_digits_become_ascii_digits() -> None:
    assert query_terms("१२३") == ["123"]


def test_stemming_never_reduces_a_word_to_a_single_character() -> None:
    assert all(len(term) >= 2 for term in query_terms("नई"))


def test_mixed_script_text_uses_the_right_pipeline_per_token() -> None:
    assert query_terms("running दिल्ली") == ["run", "दिल्ल"]


def test_offsets_still_point_at_the_original_text() -> None:
    text = "भारत की राजधानी"
    spans = [text[t.start : t.end] for t in tokenize(text)]
    assert spans == ["भारत", "राजधानी"]


def test_marks_and_joiners_alone_are_not_tokens() -> None:
    assert tokenize("ा ‍ ́") == []
