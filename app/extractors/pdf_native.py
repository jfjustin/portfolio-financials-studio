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
# a right-aligned figure: "Capital distributions (12,345)",
# "Beginning capital account balance as of January 1, 2026 $ 2,345,678".
# US "1,234,567.89" or European "1.234.567,89" grouping, "(1,234)" negatives
_AMOUNT = (r"\(?\s?-?\s?[\$€£]?\s?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?\s?\)?%?")
_CELL = rf"(?:{_AMOUNT}|-|–|—)"   # a figure, or a dash for an empty column
# label, then one or more figure columns ("Total committed 90,000,000 2,500,000")
_LINE_TAIL = re.compile(
    rf"^(?P<label>.*?(?:[A-Za-z\)\$:%]|\bQ[1-4]|\b20\d{{2}}))\s+"
    rf"(?P<values>{_CELL}(?:\s+{_CELL})*)\s*\*?$")
_CELL_RX = re.compile(_CELL)
_BARE_AMOUNT = re.compile(rf"^(?P<value>{_AMOUNT})\s*\*?$")
# "... as of June 30, 2026" — the trailing year is part of a date, not an amount
_ENDS_WITH_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?$", re.I)
_SECTION_HEADER = re.compile(r"^[A-Za-z][^:]{2,60}:$")


class ScannedPdfError(Exception):
    """Raised when a PDF appears to be a scan with no extractable text layer."""


def _looks_numeric(s: str) -> bool:
    return bool(s) and bool(_NUMISH.search(s))


_CONNECTOR_END = re.compile(r"\b(through|of|to|and|the|for|from|in|at|as|on|by|with)$", re.I)


def _looks_like_heading(line: str) -> bool:
    """A short title line ("Unrealised Capital Gains"), not a wrapped label
    ending mid-phrase ("... from Initial Closing Date through") or a date."""
    t = line.strip(" *:")
    return (3 <= len(t) <= 80 and re.match(r"[A-Za-z(]", t) is not None
            and not _CONNECTOR_END.search(t)
            and not re.fullmatch(r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+20\d{2}", t))


def parse_statement_lines(text: str, provenance: str) -> list[RawItem]:
    """Label/value pairs from free text, for statements without dot leaders.

    Handles a single space before the figure, a "$" between label and figure,
    labels that wrap onto the next line with the figure alone on its own line,
    and unlabeled subtotals under a "Section:" header.
    """
    items: list[RawItem] = []
    pending_label = ""   # a label line still waiting for its figure
    section = ""         # last "Operating expenses:"-style header
    heading = ""         # nearest heading above (e.g. "Unrealised Capital Gains")
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
                                     provenance=f"{provenance} text", section=heading))
            pending_label = ""
            continue
        m = _LINE_TAIL.match(ln)
        if m:
            label = m.group("label").rstrip(" $€£:").strip()
            cells = [c.strip() for c in _CELL_RX.findall(m.group("values")) if c.strip()]
            value = cells[-1] if cells else ""
            is_date_year = (len(cells) == 1 and re.fullmatch(r"20\d{2}", value)
                            and _ENDS_WITH_MONTH_DAY.search(label))
            if cells and not is_date_year and re.search(r"[A-Za-z]{3}", label):
                # several figure columns (e.g. Total fund | this investor, or
                # quarter | YTD | inception): keep the right-most, record how
                # many there were so the pick gets reviewed
                cols = f" cols={len(cells)}" if len(cells) > 1 else ""
                if pending_label and _looks_like_heading(pending_label):
                    # a value-less line followed by figure lines was a heading
                    heading = pending_label
                items.append(RawItem(label=label, value_raw=value,
                                     provenance=f"{provenance} text{cols}", section=heading))
                pending_label = ""
                continue
        if _SECTION_HEADER.match(ln):
            section, pending_label = ln.rstrip(":").strip(), ""
            heading = section
        elif re.search(r"[A-Za-z]{3}", ln) and len(ln) <= 120:
            # a value-less label line: either wraps onto a figure on the next line
            # or is a date continuation ("June 30, 2026*") of the previous label
            if not (pending_label and _ENDS_WITH_MONTH_DAY.search(ln.rstrip("* ,") + ",")):
                pending_label = ln.rstrip(" $")
    return items


_AMOUNT_RX = re.compile(rf"^{_AMOUNT}$")


def _statement_blocks(page) -> list[tuple[float, float]]:
    """x-ranges of statements printed side by side on one page, or [] for an
    ordinary page. A block is a region between clear vertical gaps holding
    several "label + figure" lines; regions of bare figures (a block's value
    columns) are merged into the block on their left, so a normal label | value
    page — even with several figure columns — stays one block."""
    try:
        words = page.extract_words(x_tolerance=1.5, y_tolerance=2)
    except Exception:
        return []
    if len(words) < 30:
        return []
    width = int(float(page.width)) + 2
    lines: dict[int, list[dict]] = {}
    for w in words:
        lines.setdefault(int(w["top"] // 3), []).append(w)
    cover = [0] * width
    for ws in lines.values():
        seen = set()
        for w in ws:
            seen.update(range(int(w["x0"]), min(width, int(w["x1"]) + 1)))
        for x in seen:
            cover[x] += 1
    threshold = max(1, int(len(lines) * 0.03))   # a few full-width titles may cross
    lo, hi = int(width * 0.05), int(width * 0.95)
    gaps, x = [], lo
    while x < hi:
        if cover[x] <= threshold:
            start = x
            while x < hi and cover[x] <= threshold:
                x += 1
            if x - start >= 10:
                gaps.append((start + x) / 2)
        x += 1
    if not gaps:
        return []
    edges = [0.0] + gaps + [float(page.width)]
    regions = list(zip(edges[:-1], edges[1:]))

    def kinds(x0: float, x1: float) -> tuple[int, int, int]:
        both = figures = text = 0
        for ws in lines.values():
            inside = [w for w in ws if w["x0"] >= x0 - 0.5 and w["x1"] <= x1 + 0.5]
            if not inside:
                continue
            fig = any(_AMOUNT_RX.match(w["text"]) or w["text"] in ("-", "–", "—")
                      for w in inside)
            alpha = any(re.search(r"[A-Za-z]{3}", w["text"]) for w in inside)
            both += fig and alpha
            figures += fig and not alpha
            text += alpha and not fig
        return both, figures, text

    merged: list[tuple[float, float]] = []
    for x0, x1 in regions:
        both, figures, text = kinds(x0, x1)
        if merged and both == 0 and figures >= max(1, text):
            merged[-1] = (merged[-1][0], x1)        # a value column of the block to its left
        else:
            merged.append((x0, x1))
    merged = [r for r in merged if sum(kinds(*r)) > 0]   # drop empty margins
    statements = [r for r in merged if kinds(*r)[0] >= 4]
    return merged if len(statements) >= 2 else []


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
            # Statements printed side by side on one page are read block by
            # block; read across, their lines interleave into nonsense.
            blocks = _statement_blocks(page)
            if blocks:
                text_parts[-1:] = []   # replace the interleaved page text
                parts = [(f"p{pno} block{k}",
                          page.within_bbox((x0, 0, x1, page.height)).extract_text() or "")
                         for k, (x0, x1) in enumerate(blocks, start=1)]
                text_parts.extend(t for _p, t in parts)
            else:
                parts = [(f"p{pno}", page_text)]
            for prov, part in parts:
                for ln in part.splitlines():
                    m = _LINE_KV.match(ln.strip())
                    if m:
                        raw_items.append(RawItem(
                            label=m.group("label").strip(" .:-"),
                            value_raw=m.group("value").strip(),
                            provenance=f"{prov} text"))
                raw_items.extend(parse_statement_lines(part, prov))

    # scanned detection: essentially no text across the doc
    if total_chars < max(40, 15 * n_pages):
        raise ScannedPdfError(
            f"Only {total_chars} chars of text across {n_pages} page(s) — "
            "this looks like a scanned/image PDF and needs OCR.")

    return raw_items, "\n".join(text_parts)


# --------------------------------------------------------------------------
# Page access for the model step: layout-preserving text (columns stay lined
# up, so "which column is the investor's / the quarter's" is answerable) and
# page images for scanned pages.
# --------------------------------------------------------------------------
def pdf_pages(path: Path) -> list[dict]:
    """[{"page": n, "text": layout text, "has_text": bool}] for every page."""
    out = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            plain = page.extract_text() or ""
            try:
                layout = page.extract_text(layout=True) or plain
            except Exception:
                layout = plain
            # trim the wide left/right padding layout mode adds
            layout = "\n".join(ln.rstrip() for ln in layout.splitlines())
            out.append({"page": pno, "text": layout.strip("\n"),
                        "has_text": len(plain.strip()) >= 40})
    return out


def render_page_png(path: Path, page_no: int, resolution: int = 150) -> bytes:
    """PNG bytes of one page (1-based), for vision models / OCR."""
    import io
    with pdfplumber.open(path) as pdf:
        img = pdf.pages[page_no - 1].to_image(resolution=resolution).original
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
