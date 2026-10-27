import io
import zipfile

import pytest

from docsearch.extract import (
    MAX_TEXT_CHARS,
    ExtractionError,
    UnsupportedMediaTypeError,
    clean_filename,
    extract_text,
    ocr_available,
    supported_extensions,
)
from tests.conftest import make_docx, make_pdf


def test_plain_text_and_markdown() -> None:
    assert extract_text("a.txt", b"hello world").text == "hello world"
    assert extract_text("README.MD", b"# Title").media_type == "text/markdown"


def test_utf8_bom_is_stripped_and_cp1252_is_the_fallback() -> None:
    assert extract_text("a.txt", "\ufeffcaf\u00e9".encode()).text == "café"
    assert extract_text("a.txt", "café".encode("cp1252")).text == "café"


def test_binary_data_in_a_text_file_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="binary"):
        extract_text("a.txt", b"abc\x00def")


def test_csv_cells_become_searchable_rows() -> None:
    data = b"name,city\nAda,London\nGrace,New York\n"
    assert extract_text("people.csv", data).text == "name city\nAda London\nGrace New York"


def test_csv_with_semicolons_and_quotes() -> None:
    text = extract_text("a.csv", b'name;note\n"Smith; J";"said ""hi"""\n').text
    assert "Smith; J" in text
    assert 'said "hi"' in text


def test_docx_paragraphs_and_tables() -> None:
    data = make_docx("First paragraph", "Second", table=[["cellA", "cellB"], ["cellC", "cellD"]])
    text = extract_text("report.docx", data).text
    assert "First paragraph" in text
    assert "cellD" in text


def test_invalid_docx_is_a_clean_error() -> None:
    with pytest.raises(ExtractionError, match="docx"):
        extract_text("a.docx", b"this is not a zip")
    not_ooxml = io.BytesIO()
    with zipfile.ZipFile(not_ooxml, "w") as archive:
        archive.writestr("hello.txt", "hi")
    with pytest.raises(ExtractionError, match="docx"):
        extract_text("a.docx", not_ooxml.getvalue())


def test_docx_zip_bomb_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("docsearch.extract.MAX_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(ExtractionError, match="unreasonable size"):
        extract_text("a.docx", make_docx("some text " * 50))


def test_pdf_text_is_extracted() -> None:
    assert "quarterly revenue" in extract_text("r.pdf", make_pdf("quarterly revenue")).text


def test_invalid_pdf_is_a_clean_error() -> None:
    with pytest.raises(ExtractionError):
        extract_text("a.pdf", b"%PDF-1.4 garbage")


def test_pdf_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("docsearch.extract.MAX_PDF_PAGES", 0)
    with pytest.raises(ExtractionError, match="pages"):
        extract_text("a.pdf", make_pdf("x"))


def test_unknown_extension_lists_what_is_accepted() -> None:
    with pytest.raises(UnsupportedMediaTypeError, match=r"\.exe.*\.pdf"):
        extract_text("virus.exe", b"MZ")
    with pytest.raises(UnsupportedMediaTypeError, match="none"):
        extract_text("noextension", b"data")


def test_text_length_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("docsearch.extract.MAX_TEXT_CHARS", 10)
    assert MAX_TEXT_CHARS == 5_000_000
    with pytest.raises(ExtractionError, match="exceeds"):
        extract_text("a.txt", b"x" * 11)


def test_control_characters_are_scrubbed() -> None:
    assert extract_text("a.txt", b"a\x07b\x1bc").text == "a b c"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\evil.txt", "evil.txt"),
        ("a/b\\c.txt", "c.txt"),
        ("na\x00me\n.txt", "name.txt"),
        ("", "unnamed"),
        ("..", ".."),
        ("x" * 400 + ".txt", ("x" * 400 + ".txt")[:255]),
    ],
)
def test_clean_filename_never_yields_a_path(raw: str, expected: str) -> None:
    cleaned = clean_filename(raw)
    assert cleaned == expected
    assert "/" not in cleaned
    assert "\\" not in cleaned


def test_images_are_rejected_without_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("docsearch.extract.ocr_available", lambda: False)
    assert ".png" not in supported_extensions()
    with pytest.raises(UnsupportedMediaTypeError, match="OCR"):
        extract_text("scan.png", b"\x89PNG")


@pytest.mark.ocr
@pytest.mark.skipif(not ocr_available(), reason="tesseract / pytesseract / Pillow not installed")
def test_ocr_reads_rendered_text() -> None:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (400, 100), "white")
    ImageDraw.Draw(image).text(
        (10, 30), "invoice 4711", fill="black", font=ImageFont.load_default(36)
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    assert "4711" in extract_text("scan.png", buffer.getvalue()).text
    with pytest.raises(ExtractionError, match="image"):
        extract_text("bad.png", b"not an image")


def test_undecodable_bytes_are_replaced_not_fatal() -> None:
    assert extract_text("a.txt", b"ok \x81\x8d end").text.startswith("ok ")


def test_single_column_csv_still_parses() -> None:
    assert extract_text("a.csv", b"alpha\nbeta\n").text == "alpha\nbeta"


def test_csv_field_over_the_parser_limit_is_a_clean_error() -> None:
    with pytest.raises(ExtractionError, match="CSV"):
        extract_text("a.csv", b"a,b\n" + b"x" * 200_000 + b",y\n")


def test_encrypted_pdf_is_refused() -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(make_pdf("secret"))).pages:
        writer.add_page(page)
    writer.encrypt("pw")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ExtractionError, match="encrypted"):
        extract_text("a.pdf", buffer.getvalue())
