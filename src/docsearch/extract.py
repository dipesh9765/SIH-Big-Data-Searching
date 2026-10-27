"""Turn an uploaded file into plain text.

Extraction works on bytes in memory: nothing is written to disk and the client's file name is only
used to pick a parser, never as a path (the legacy service saved uploads under the client-supplied
name, which allowed path traversal and overwrites).
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

MAX_TEXT_CHARS = 5_000_000  # cap on extracted text so one huge file cannot exhaust memory
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # zip-bomb guard for .docx
MAX_PDF_PAGES = 2_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keeps \t \n \r: fine inside text
_FILENAME_CONTROL = re.compile(r"[\x00-\x1f\x7f]")  # names also lose them (log/header injection)


class ExtractionError(Exception):
    """The file could not be read as the type it claims to be."""


class UnsupportedMediaTypeError(ExtractionError):
    """The file extension is not one we can extract text from."""


@dataclass(frozen=True, slots=True)
class Extracted:
    text: str
    media_type: str


def clean_filename(name: str) -> str:
    """A safe display name: last path component only, control characters removed, bounded."""
    base = PureWindowsPath(PurePosixPath(name).name).name
    base = _FILENAME_CONTROL.sub("", base).strip()
    return (base or "unnamed")[:255]


def _decode(data: bytes) -> str:
    if b"\x00" in data[:8192]:
        raise ExtractionError("file looks binary, not text")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _plain(data: bytes) -> str:
    return _decode(data)


def _csv(data: bytes) -> str:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    lines: list[str] = []
    try:
        lines.extend(
            " ".join(cell for cell in row if cell)
            for row in csv.reader(io.StringIO(text, newline=""), dialect)
        )
    except csv.Error as exc:
        raise ExtractionError(f"invalid CSV: {exc}") from exc
    return "\n".join(line for line in lines if line)


def _docx(data: bytes) -> str:
    import docx  # noqa: PLC0415 - heavy import, only paid when a .docx arrives

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise ExtractionError("document expands to an unreasonable size")
        document = docx.Document(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionError("not a valid .docx file") from exc
    except KeyError as exc:  # a zip that is not an OOXML package
        raise ExtractionError("not a valid .docx file") from exc
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        parts.extend(cell.text for row in table.rows for cell in row.cells)
    return "\n".join(part for part in parts if part)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader  # noqa: PLC0415
    from pypdf.errors import PyPdfError  # noqa: PLC0415

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("PDF is encrypted")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ExtractionError(f"PDF has more than {MAX_PDF_PAGES} pages")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except PyPdfError as exc:
        raise ExtractionError(f"unreadable PDF: {exc}") from exc


def ocr_available() -> bool:
    """True when the optional OCR stack (pytesseract + Pillow + the tesseract binary) is usable."""
    try:
        import PIL  # noqa: F401, PLC0415
        import pytesseract  # noqa: F401, PLC0415
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def _image(data: bytes) -> str:
    if not ocr_available():
        raise UnsupportedMediaTypeError("OCR is not installed on this server")
    import pytesseract  # noqa: PLC0415
    from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return str(pytesseract.image_to_string(image))
    except (UnidentifiedImageError, OSError) as exc:
        raise ExtractionError("not a readable image") from exc


_PARSERS: dict[str, tuple[str, Callable[[bytes], str]]] = {
    ".txt": ("text/plain", _plain),
    ".md": ("text/markdown", _plain),
    ".csv": ("text/csv", _csv),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        _docx,
    ),
    ".pdf": ("application/pdf", _pdf),
    ".png": ("image/png", _image),
    ".jpg": ("image/jpeg", _image),
    ".jpeg": ("image/jpeg", _image),
}


def supported_extensions() -> list[str]:
    """Extensions currently accepted (image types only when OCR is installed)."""
    ocr = ocr_available()
    return sorted(ext for ext, (mt, _) in _PARSERS.items() if ocr or not mt.startswith("image/"))


def extract_text(filename: str, data: bytes) -> Extracted:
    """Extract text from `data`, choosing the parser from the extension of `filename`."""
    suffix = PurePosixPath(clean_filename(filename).lower()).suffix
    entry = _PARSERS.get(suffix)
    if entry is None:
        raise UnsupportedMediaTypeError(
            f"unsupported file type {suffix or '(none)'!r}; "
            f"accepted: {', '.join(supported_extensions())}"
        )
    media_type, parser = entry
    text = _CONTROL.sub(" ", parser(data))
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractionError(f"extracted text exceeds {MAX_TEXT_CHARS:,} characters")
    return Extracted(text=text, media_type=media_type)
