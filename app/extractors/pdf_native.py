"""Deterministic native-PDF extraction via pdfplumber.

Strategy:
  1. Pull tables page-by-page; map first text cell -> label, numeric cells -> values.
  2. Also scan free text lines of the form "Label .......  1,234,567" (dot leaders,
     multiple spaces) to catch figures that aren't in a detected table.
  3. Detect scanned/image PDFs (little or no extractable text) and raise a clear
     signal so the pipeline flags the file for OCR instead of silently returning nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from ..models import RawItem

# a line like:  "Total net assets ..........  12,345,678"  or  "NAV    12,345,678"
_LINE_KV = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9 &/,'\-\(\)\.%]{2,70}?)"
    r"[\s\.\:]{2,}"
    r"(?P<value>\(?[-–]?[\$€£¥]?\s?[0-9][0-9,\.]*\)?\s?%?(?:\s?(?:k|m|mm|bn|million|billion))?)\s*$",
    re.I)

_NUMISH = re.compile(r"[0-9]")


class ScannedPdfError(Exception):
    """Raised when a PDF appears to be a scan with no extractable text layer."""


def _looks_numeric(s: str) -> bool:
    return bool(s) and bool(_NUMISH.search(s))


def extract_pdf(path: Path) -> tuple[list[RawItem], str]:
    raw_items: list[RawItem] = []
    text_parts: list[str] = []
    total_chars = 0

    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            total_chars += len(page_text.strip())
            if page_text:
                text_parts.append(page_text)

            # --- tables ---
            try:
                tables = page.extract_tables()
            except Exception:
                tables = []
            for tno, table in enumerate(tables, start=1):
                for rno, row in enumerate(table, start=1):
                    if not row:
                        continue
                    cells = [(c or "").strip() for c in row]
                    # label = first non-empty text cell that isn't purely numeric
                    label = None
                    label_idx = -1
                    for i, c in enumerate(cells):
                        if c and not _looks_numeric(c):
                            label, label_idx = c, i
                            break
                    if label is None:
                        continue
                    for i in range(label_idx + 1, len(cells)):
                        val = cells[i]
                        if _looks_numeric(val):
                            raw_items.append(RawItem(
                                label=label, value_raw=val,
                                provenance=f"p{pno} table{tno} r{rno}c{i+1}"))

            # --- free-text key/value lines (dot leaders / aligned columns) ---
            for ln in page_text.splitlines():
                m = _LINE_KV.match(ln.strip())
                if m:
                    raw_items.append(RawItem(
                        label=m.group("label").strip(" .:-"),
                        value_raw=m.group("value").strip(),
                        provenance=f"p{pno} text"))

    # scanned detection: essentially no text across the doc
    if total_chars < max(40, 15 * n_pages):
        raise ScannedPdfError(
            f"Only {total_chars} chars of text across {n_pages} page(s) — "
            "this looks like a scanned/image PDF and needs OCR.")

    return raw_items, "\n".join(text_parts)
