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
MIN_TEXT_LAYER_CHARS = 25  # OCR keeps a page's own text layer only if it has at least this many
ROUTING_MIN_CHARS = 5  # non-space characters for a page to count as having text when routing
SCANNED_PAGE_FRACTION = 0.8  # fewer than this share of pages with text: send the PDF to OCR
AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".aac": "audio/aac",
}
VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
}
SPEECH_MEDIA_TYPES = {**AUDIO_MEDIA_TYPES, **VIDEO_MEDIA_TYPES}
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keeps \t \n \r: fine inside text
_FILENAME_CONTROL = re.compile(r"[\x00-\x1f\x7f]")  # names also lose them (log/header injection)


class ExtractionError(Exception):
    """The file could not be read as the type it claims to be."""


class UnsupportedMediaTypeError(ExtractionError):
    """The file extension is not one we can extract text from."""


class NeedsOcrError(ExtractionError):
    """The PDF is (mostly) scanned pages with no text layer; an OCR worker must handle it."""


@dataclass(frozen=True, slots=True)
class Unit:
    """A stretch of an asset indexed as its own document (a transcript segment of a recording)."""

    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class Extracted:
    text: str
    media_type: str
    confidence: float | None = None  # mean OCR word confidence (0-100), when OCR was used
    units: tuple[Unit, ...] = ()  # when set, each unit is indexed separately instead of `text`
    duration_ms: int | None = None  # length of a recording
    language: str | None = None  # language detected in a recording


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


def _pdf_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader  # noqa: PLC0415
    from pypdf.errors import PyPdfError  # noqa: PLC0415

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("PDF is encrypted")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ExtractionError(f"PDF has more than {MAX_PDF_PAGES} pages")
        return [page.extract_text() or "" for page in reader.pages]
    except PyPdfError as exc:
        raise ExtractionError(f"unreadable PDF: {exc}") from exc


def _pdf(data: bytes) -> str:
    return "\n".join(_pdf_pages(data))


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


def clean_text(raw: str) -> str:
    """Strip control characters and enforce the size cap on extracted text."""
    text = _CONTROL.sub(" ", raw)
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractionError(f"extracted text exceeds {MAX_TEXT_CHARS:,} characters")
    return text


def service_extensions() -> list[str]:
    """Extensions the scale-out service accepts: images are always accepted there, because the
    API process only queues them and a separate OCR worker reads them."""
    plain = [ext for ext, (mt, _) in _PARSERS.items() if not mt.startswith("image/")]
    return sorted({*plain, *IMAGE_MEDIA_TYPES, *SPEECH_MEDIA_TYPES})


def media_type_for(filename: str) -> str:
    """Content type to serve a stored file with, from its (already validated) extension."""
    suffix = PurePosixPath(clean_filename(filename).lower()).suffix
    if suffix in _PARSERS:
        return _PARSERS[suffix][0]
    return {**IMAGE_MEDIA_TYPES, **SPEECH_MEDIA_TYPES}.get(suffix, "application/octet-stream")


def extract_text(filename: str, data: bytes, *, detect_scanned: bool = False) -> Extracted:
    """Extract text from `data`, choosing the parser from the extension of `filename`.

    With `detect_scanned`, a PDF whose pages mostly have no text layer raises `NeedsOcrError`
    instead of returning (nearly) empty text, so the caller can route it to an OCR worker.
    """
    suffix = PurePosixPath(clean_filename(filename).lower()).suffix
    entry = _PARSERS.get(suffix)
    if entry is None:
        raise UnsupportedMediaTypeError(
            f"unsupported file type {suffix or '(none)'!r}; "
            f"accepted: {', '.join(supported_extensions())}"
        )
    media_type, parser = entry
    if detect_scanned and suffix == ".pdf":
        pages = _pdf_pages(data)
        with_text = sum(len("".join(page.split())) >= ROUTING_MIN_CHARS for page in pages)
        if pages and with_text < SCANNED_PAGE_FRACTION * len(pages):
            raise NeedsOcrError("the PDF has no text layer; it needs OCR")
        raw = "\n".join(pages)
    else:
        raw = parser(data)
    return Extracted(text=clean_text(raw), media_type=media_type)
