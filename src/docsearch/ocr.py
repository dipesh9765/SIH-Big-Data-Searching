"""Optical character recognition for images and scanned PDFs (Tesseract).

Runs in dedicated OCR workers, never in the API process: a page takes seconds of CPU, so it must
not share a process or a queue with fast text jobs. Every limit below exists because the input is
untrusted: image dimensions are checked before decoding, PDFs are capped by page count, each page
render is bounded in pixels, and each Tesseract call has a timeout.

A PDF is processed page by page. A page that already has a text layer keeps it (exact, and free);
only pages without one are rendered and recognised, so a mixed document costs only its scanned
pages. The mean word confidence Tesseract reports is returned so poor scans can be spotted.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from docsearch.extract import MIN_TEXT_LAYER_CHARS, ExtractionError

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


@dataclass(frozen=True, slots=True)
class OcrConfig:
    languages: str = "eng+hin+mar"  # Tesseract language codes joined with "+"
    max_pages: int = 200
    max_pixels: int = 60_000_000  # per image or rendered page
    page_timeout_seconds: float = 120.0
    render_dpi: int = 200
    max_render_side: int = 5000  # longest side of a rendered PDF page, in pixels


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    confidence: float | None  # mean word confidence, 0-100; None when nothing was recognised
    pages: int
    ocr_pages: int  # pages that needed recognition (the rest had a text layer)


def tesseract_available() -> bool:
    try:
        import pytesseract  # noqa: F401, PLC0415
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def check_ready(config: OcrConfig) -> None:
    """Raise `RuntimeError` unless Tesseract and every requested language pack are installed.

    Called when an OCR worker starts, so a misconfigured worker refuses to run instead of failing
    every job it claims.
    """
    if not tesseract_available():
        raise RuntimeError("the tesseract binary or the pytesseract package is not installed")
    import pytesseract  # noqa: PLC0415

    installed = set(pytesseract.get_languages(config=""))
    missing = [code for code in config.languages.split("+") if code not in installed]
    if missing:
        raise RuntimeError(
            f"Tesseract language data missing for: {', '.join(missing)} "
            f"(installed: {', '.join(sorted(installed))})"
        )


def _recognise(image: PILImage, config: OcrConfig) -> tuple[str, float | None]:
    """OCR one page image; returns (text, mean word confidence)."""
    import pytesseract  # noqa: PLC0415
    from PIL import ImageOps  # noqa: PLC0415

    page = ImageOps.exif_transpose(image) or image
    page = ImageOps.autocontrast(page.convert("L"))
    try:
        data = pytesseract.image_to_data(
            page,
            lang=config.languages,
            config="--psm 3",
            output_type=pytesseract.Output.DICT,
            timeout=config.page_timeout_seconds,
        )
    except RuntimeError as exc:  # pytesseract raises RuntimeError("Tesseract process timeout")
        raise ExtractionError(f"OCR failed: {exc}") from exc
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    for index, raw in enumerate(data["text"]):
        word = raw.strip()
        if not word:
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        lines.setdefault(key, []).append(word)
        confidence = float(data["conf"][index])
        if confidence >= 0:
            confidences.append(confidence)
    text = "\n".join(" ".join(words) for words in lines.values())
    return text, (sum(confidences) / len(confidences) if confidences else None)


def _check_size(width: int, height: int, config: OcrConfig) -> None:
    if width * height > config.max_pixels:
        raise ExtractionError(
            f"image is too large to recognise ({width}x{height}; "
            f"the limit is {config.max_pixels:,} pixels)"
        )


def ocr_image(data: bytes, config: OcrConfig | None = None) -> OcrResult:
    """Recognise text in an image file (every frame of a multi-page TIFF, up to the page limit)."""
    config = config or OcrConfig()
    from PIL import Image, ImageSequence, UnidentifiedImageError  # noqa: PLC0415

    texts: list[str] = []
    confidences: list[float] = []
    frames = 0
    try:
        with Image.open(io.BytesIO(data)) as image:
            _check_size(image.width, image.height, config)
            for frame in ImageSequence.Iterator(image):
                frames += 1
                if frames > config.max_pages:
                    raise ExtractionError(f"image has more than {config.max_pages} frames")
                _check_size(frame.width, frame.height, config)
                frame.load()
                text, confidence = _recognise(frame, config)
                texts.append(text)
                if confidence is not None:
                    confidences.append(confidence)
                if image.format != "TIFF":
                    break  # animated GIF/WebP: the first frame is the picture
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ExtractionError("not a readable image") from exc
    mean = sum(confidences) / len(confidences) if confidences else None
    return OcrResult("\n\n".join(t for t in texts if t), mean, frames, frames)


def ocr_pdf(
    data: bytes,
    config: OcrConfig | None = None,
    on_page: Callable[[int, int], None] | None = None,
) -> OcrResult:
    """Text of a PDF: the text layer where a page has one, OCR where it does not.

    `on_page(done, total)` is called after each page, so a worker can renew its job lease while a
    long document is processed.
    """
    config = config or OcrConfig()
    import pypdfium2 as pdfium  # noqa: PLC0415
    from pypdfium2 import PdfiumError  # noqa: PLC0415

    try:
        pdf = pdfium.PdfDocument(data)
    except PdfiumError as exc:
        message = str(exc).lower()
        if "password" in message:
            raise ExtractionError("PDF is encrypted") from exc
        raise ExtractionError(f"unreadable PDF: {exc}") from exc
    try:
        total = len(pdf)
        if total > config.max_pages:
            raise ExtractionError(f"PDF has {total} pages; the OCR limit is {config.max_pages}")
        texts: list[str] = []
        confidences: list[float] = []
        recognised = 0
        for number in range(total):
            page = pdf[number]
            try:
                layer = page.get_textpage().get_text_range().strip()
                if len(layer) >= MIN_TEXT_LAYER_CHARS:
                    texts.append(layer)
                else:
                    width, height = page.get_size()
                    scale = min(config.render_dpi / 72, config.max_render_side / max(width, height))
                    _check_size(int(width * scale), int(height * scale), config)
                    text, confidence = _recognise(page.render(scale=scale).to_pil(), config)
                    recognised += 1
                    if text:
                        texts.append(text)
                    if confidence is not None:
                        confidences.append(confidence)
            finally:
                page.close()
            if on_page is not None:
                on_page(number + 1, total)
    finally:
        pdf.close()
    mean = sum(confidences) / len(confidences) if confidences else None
    return OcrResult("\n\n".join(texts), mean, total, recognised)
