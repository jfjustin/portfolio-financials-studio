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

# Statement layouts where pdfplumber puts a single space between the label and
# a right-aligned figure: "Capital distributions (73,202)",
# "Beginning capital account balance as of January 1, 2026 $ 1,175,980".
_AMOUNT = (r"\(?-?\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?%?")
_LINE_TAIL = re.compile(rf"^(?P<label>.*?[A-Za-z\)\$:%]|.*?\b20\d{{2}})\s+(?P<value>{_AMOUNT})\s*\*?$")
_BARE_AMOUNT = re.compile(rf"^(?P<value>{_AMOUNT})\s*\*?$")
# "... as of June 30, 2026" — the trailing year is part of a date, not an amount
_ENDS_WITH_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?$", re.I)
_SECTION_HEADER = re.compile(r"^[A-Za-z][^:]{2,60}:$")


class ScannedPdfError(Exception):
    """Raised when a PDF appears to be a scan with no extractable text layer."""


def _looks_numeric(s: str) -> bool:
    return bool(s) and bool(_NUMISH.search(s))


def parse_statement_lines(text: str, provenance: str) -> list[RawItem]:
    """Label/value pairs from free text, for statements without dot leaders.

    Handles a single space before the figure, a "$" between label and figure,
    labels that wrap onto the next line with the figure alone on its own line,
    and unlabeled subtotals under a "Section:" header.
    """
    items: list[RawItem] = []
    pending_label = ""   # a label line still waiting for its figure
    section = ""         # last "Operating expenses:"-style header
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if _LINE_KV.match(ln):
            pending_label = ""
            continue  # handled by the dot-leader pass
        bare = _BARE_AMOUNT.match(ln)
        if bare:
            label = pending_label or (f"{section} (subtotal)" if section else "")
            if label:
                items.append(RawItem(label=label, value_raw=bare.group("value").strip(),
                                     provenance=f"{provenance} text"))
            pending_label = ""
            continue
        m = _LINE_TAIL.match(ln)
        if m:
            label = m.group("label").rstrip(" $:").strip()
            value = m.group("value").strip()
            is_date_year = re.fullmatch(r"20\d{2}", value) and _ENDS_WITH_MONTH_DAY.search(label)
            if not is_date_year and re.search(r"[A-Za-z]{3}", label):
                items.append(RawItem(label=label, value_raw=value,
                                     provenance=f"{provenance} text"))
                pending_label = ""
                continue
        if _SECTION_HEADER.match(ln):
            section, pending_label = ln.rstrip(":").strip(), ""
        elif re.search(r"[A-Za-z]{3}", ln) and len(ln) <= 120:
            # a value-less label line: either wraps onto a figure on the next line
            # or is a date continuation ("June 30, 2026*") of the previous label
            if not (pending_label and _ENDS_WITH_MONTH_DAY.search(ln.rstrip("* ,") + ",")):
                pending_label = ln.rstrip(" $")
    return items


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
            raw_items.extend(parse_statement_lines(page_text, f"p{pno}"))

    # scanned detection: essentially no text across the doc
    if total_chars < max(40, 15 * n_pages):
        raise ScannedPdfError(
            f"Only {total_chars} chars of text across {n_pages} page(s) — "
            "this looks like a scanned/image PDF and needs OCR.")

    return raw_items, "\n".join(text_parts)
