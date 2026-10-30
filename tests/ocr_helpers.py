"""Build scanned-looking inputs for OCR tests: rendered text images and image-only PDFs."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

DEVANAGARI_FONTS = [
    "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "C:/Windows/Fonts/Nirmala.ttf",
    "C:/Windows/Fonts/mangal.ttf",
]


def devanagari_font(size: int = 64) -> ImageFont.FreeTypeFont | None:
    """A font that can draw Devanagari, with complex-text shaping available, or None."""
    if not features.check("raqm"):
        return None
    for path in DEVANAGARI_FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return None


def render_text(
    lines: list[str],
    *,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None,
    size: int = 64,
    width: int = 1600,
) -> Image.Image:
    """Black text on white, one line per entry, large enough for OCR to read reliably."""
    font = font or ImageFont.load_default(size)
    height = int(size * 1.9 * len(lines)) + 60
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for number, line in enumerate(lines):
        draw.text((40, 30 + int(number * size * 1.9)), line, fill="black", font=font)
    return image


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def image_only_pdf(*pages: Image.Image) -> bytes:
    """A PDF whose pages are pictures with no text layer, like a scanner produces."""
    buffer = io.BytesIO()
    first, *rest = pages
    first.save(buffer, format="PDF", save_all=True, append_images=list(rest), resolution=150.0)
    return buffer.getvalue()


def merge_pdfs(*documents: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for document in documents:
        for page in PdfReader(io.BytesIO(document)).pages:
            writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def tiff(*images: Image.Image) -> bytes:
    buffer = io.BytesIO()
    first, *rest = images
    first.save(buffer, format="TIFF", save_all=True, append_images=list(rest))
    return buffer.getvalue()
