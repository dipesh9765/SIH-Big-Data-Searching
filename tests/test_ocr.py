"""OCR: limits and routing logic run anywhere; recognition tests need the tesseract binary with the
English, Hindi and Marathi language data (the worker image has them; see deploy/Dockerfile.worker).
"""

from __future__ import annotations

import pytest
from PIL import Image

from docsearch.extract import (
    ExtractionError,
    NeedsOcrError,
    extract_text,
    service_extensions,
)
from docsearch.ocr import OcrConfig, check_ready, ocr_image, ocr_pdf, tesseract_available
from tests.conftest import make_pdf
from tests.ocr_helpers import (
    devanagari_font,
    image_only_pdf,
    merge_pdfs,
    png,
    render_text,
    tiff,
)

needs_tesseract = pytest.mark.skipif(not tesseract_available(), reason="tesseract not installed")


# --------------------------------------------------------------- anywhere (no tesseract needed)


def test_a_pdf_with_a_text_layer_is_not_sent_to_ocr() -> None:
    result = extract_text(
        "a.pdf", make_pdf("born digital text layer present here"), detect_scanned=True
    )
    assert "born digital" in result.text


def test_a_very_short_born_digital_pdf_is_not_mistaken_for_a_scan() -> None:
    # "earthquake" is one word on one page: short, but a real text layer.
    assert extract_text("a.pdf", make_pdf("earthquake"), detect_scanned=True).text == "earthquake"


def test_an_image_only_pdf_needs_ocr() -> None:
    scanned = image_only_pdf(render_text(["scanned page"]))
    with pytest.raises(NeedsOcrError):
        extract_text("scan.pdf", scanned, detect_scanned=True)
    assert (
        extract_text("scan.pdf", scanned).text.strip() == ""
    )  # without the flag: empty, as before


def test_mostly_blank_pages_in_a_text_pdf_still_route_to_ocr() -> None:
    mixed = merge_pdfs(
        make_pdf("page with plenty of real text on it"),
        image_only_pdf(render_text(["scan"])),
        image_only_pdf(render_text(["scan"])),
    )
    with pytest.raises(NeedsOcrError):
        extract_text("m.pdf", mixed, detect_scanned=True)


def test_the_service_accepts_images_whether_or_not_this_process_can_ocr() -> None:
    accepted = service_extensions()
    for extension in (".png", ".jpg", ".tiff", ".webp", ".pdf", ".txt"):
        assert extension in accepted


def test_text_layer_pages_are_used_as_they_are_without_recognition() -> None:
    result = ocr_pdf(make_pdf("Hello text layer that is long enough to count as text"))
    assert "text layer" in result.text
    assert (result.pages, result.ocr_pages) == (1, 0)
    assert result.confidence is None


def test_oversized_images_are_refused_before_decoding_pixels() -> None:
    big = Image.new("L", (3000, 3000), 255)
    with pytest.raises(ExtractionError, match="too large"):
        ocr_image(png(big), OcrConfig(max_pixels=1_000_000))


def test_garbage_is_not_an_image_and_not_a_pdf() -> None:
    with pytest.raises(ExtractionError, match="not a readable image"):
        ocr_image(b"definitely not an image")
    with pytest.raises(ExtractionError, match="PDF"):
        ocr_pdf(b"%PDF-1.4 nope")


def test_page_limit_is_enforced() -> None:
    pages = [make_pdf(f"page number {n} with enough text to count") for n in range(3)]
    document = merge_pdfs(*pages)
    with pytest.raises(ExtractionError, match="limit is 2"):
        ocr_pdf(document, OcrConfig(max_pages=2))


def test_progress_callback_is_called_per_page() -> None:
    document = merge_pdfs(
        *[make_pdf(f"page {n} has a text layer of sufficient length") for n in range(3)]
    )
    seen: list[tuple[int, int]] = []
    ocr_pdf(document, on_page=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_check_ready_names_the_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("docsearch.ocr.tesseract_available", lambda: False)
    with pytest.raises(RuntimeError, match="not installed"):
        check_ready(OcrConfig())


# ------------------------------------------------------------------------ real Tesseract


@needs_tesseract
def test_check_ready_accepts_the_configured_languages_and_rejects_others() -> None:
    check_ready(OcrConfig(languages="eng"))
    with pytest.raises(RuntimeError, match="zzz"):
        check_ready(OcrConfig(languages="eng+zzz"))


@needs_tesseract
def test_english_text_in_an_image_is_recognised_with_a_confidence() -> None:
    result = ocr_image(png(render_text(["Quarterly invoice 4711", "Total due 982 dollars"])))
    text = result.text.lower()
    assert "invoice" in text
    assert "4711" in text
    assert "982" in text
    assert result.confidence is not None
    assert 50 <= result.confidence <= 100


@needs_tesseract
def test_hindi_text_is_recognised() -> None:
    font = devanagari_font(72)
    if font is None:
        pytest.skip("no Devanagari font or no text shaping (raqm) available")
    sentence = "भारत की राजधानी नई दिल्ली है"
    result = ocr_image(png(render_text([sentence], font=font, size=72)))
    # OCR is not exact: require most of the words, not the whole sentence.
    wanted = {"भारत", "राजधानी", "दिल्ली"}
    found = {word for word in wanted if word in result.text}
    assert len(found) >= 2, result.text


@needs_tesseract
def test_a_scanned_pdf_is_recognised_page_by_page() -> None:
    scanned = image_only_pdf(
        render_text(["First page about glaciers"]), render_text(["Second page about volcanoes"])
    )
    result = ocr_pdf(scanned)
    assert (result.pages, result.ocr_pages) == (2, 2)
    assert "glaciers" in result.text.lower()
    assert "volcanoes" in result.text.lower()


@needs_tesseract
def test_a_mixed_pdf_ocrs_only_the_scanned_pages() -> None:
    mixed = merge_pdfs(
        make_pdf("This page has a real text layer about tectonics and earthquakes"),
        image_only_pdf(render_text(["Scanned page about tsunamis"])),
    )
    result = ocr_pdf(mixed)
    assert (result.pages, result.ocr_pages) == (2, 1)
    assert "tectonics" in result.text
    assert "tsunamis" in result.text.lower()


@needs_tesseract
def test_multi_page_tiff_reads_every_frame() -> None:
    document = tiff(render_text(["Frame one apples"]), render_text(["Frame two oranges"]))
    result = ocr_image(document)
    assert result.pages == 2
    assert "apples" in result.text.lower()
    assert "oranges" in result.text.lower()


@needs_tesseract
def test_a_blank_page_yields_empty_text_not_an_error() -> None:
    result = ocr_image(png(Image.new("RGB", (800, 600), "white")))
    assert result.text.strip() == ""


@needs_tesseract
def test_a_page_that_takes_too_long_is_a_clean_error() -> None:
    page = render_text(["slow"] * 6)
    with pytest.raises(ExtractionError, match="OCR failed"):
        ocr_image(png(page), OcrConfig(page_timeout_seconds=0.001))
