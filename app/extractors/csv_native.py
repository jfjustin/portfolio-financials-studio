"""Deterministic CSV extraction.

Mirrors excel.py's label/value heuristic: first text cell in a row is the
label, numeric cells to the right are values. CSV cells are always strings, so
numeric detection goes through models.parse_number() (handles $, commas,
accounting parens, %) rather than a native-type check.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from ..models import RawItem, parse_number
from .base import compose_headers, header_row, statement_kind


_FIGURE = re.compile(r"[\s$€£¥(\-–]*\d[\d,.\s]*\)?\s*(?:%|x|months?|mos?\.?|k|mm?|bn)?\s*", re.I)


def _is_numeric(v: str) -> bool:
    """A figure cell ("$70", "($1,209)", "8 Months", "12.5%") — not a header
    such as "Q1'26" whose digits only look like a number."""
    return bool(v) and bool(_FIGURE.fullmatch(v)) and parse_number(v)[0] is not None


def extract_csv(path: Path) -> tuple[list[RawItem], str]:
    raw_items: list[RawItem] = []
    text_parts: list[str] = []
    col_headers: dict[int, str] = {}
    header_block: list[dict[int, str]] = []
    in_figures = False
    heading = ""
    kind = ""

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for r_idx, row in enumerate(csv.reader(f), start=1):
            values = [(c or "").strip() for c in row]
            row_text = " ".join(v for v in values if v)
            if row_text:
                text_parts.append(row_text)

            cells = {i: v for i, v in enumerate(values) if v}
            hdr = None if any(_is_numeric(v) and not re.fullmatch(r"(?:19|20)\d{2}", v)
                              for v in cells.values()) else header_row(cells)
            if hdr is not None:
                if in_figures:
                    header_block, in_figures = [], False
                header_block.append(hdr)
                if not kind:
                    kind = statement_kind(row_text)
                continue

            label_idx = None
            for i, v in enumerate(values):
                if v and not _is_numeric(v):
                    label_idx = i
                    break
            if label_idx is None:
                continue

            numeric_idx = [i for i in range(label_idx + 1, len(values))
                           if _is_numeric(values[i])]
            if not numeric_idx:
                heading = values[label_idx][:80]
                if not kind and not in_figures:
                    kind = statement_kind(row_text)
                continue
            if header_block and not in_figures:
                col_headers.update(compose_headers(header_block))
            in_figures = True

            label = values[label_idx]
            for i in numeric_idx:
                col_note = col_headers.get(i, "")
                prov = f"row{r_idx}col{i + 1}"
                if col_note:
                    prov += f" ({col_note})"
                raw_items.append(RawItem(label=label, value_raw=values[i], provenance=prov,
                                         section=heading, statement=kind))

    return raw_items, "\n".join(text_parts)
