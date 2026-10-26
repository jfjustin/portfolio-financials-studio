"""Optional local OCR (Tesseract) for scanned PDFs.

Used when no vision-capable model is configured. Needs the `tesseract` binary
(macOS: `brew install tesseract`; Debian/Ubuntu: `apt-get install tesseract-ocr`)
and `pip install pytesseract`. Absent either, scanned pages are reported as
needing OCR instead of being silently skipped.
"""
from __future__ import annotations

import io
import shutil
from pathlib import Path

from .pdf_native import render_page_png


def tesseract_available() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False
    return True


def ocr_pages(path: Path, page_numbers: list[int]) -> dict[int, str]:
    """{page_no: text} for the given 1-based pages."""
    import pytesseract
    from PIL import Image
    out = {}
    for n in page_numbers:
        img = Image.open(io.BytesIO(render_page_png(path, n, resolution=300)))
        # psm 6: a uniform block of text — keeps statement rows on one line
        out[n] = pytesseract.image_to_string(img, config="--psm 6")
    return out
