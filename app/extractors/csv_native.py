"""Deterministic CSV extraction.

Mirrors excel.py's label/value heuristic: first text cell in a row is the
label, numeric cells to the right are values. CSV cells are always strings, so
numeric detection goes through models.parse_number() (handles $, commas,
accounting parens, %) rather than a native-type check.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..models import RawItem, parse_number


def _is_numeric(v: str) -> bool:
    return bool(v) and parse_number(v)[0] is not None


def extract_csv(path: Path) -> tuple[list[RawItem], str]:
    raw_items: list[RawItem] = []
    text_parts: list[str] = []
    col_headers: dict[int, str] = {}
    header_scanned = False

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for r_idx, row in enumerate(csv.reader(f), start=1):
            values = [(c or "").strip() for c in row]
            row_text = " ".join(v for v in values if v)
            if row_text:
                text_parts.append(row_text)

            label_idx = None
            for i, v in enumerate(values):
                if v and not _is_numeric(v):
                    label_idx = i
                    break
            if label_idx is None:
                if not header_scanned:
                    for i, v in enumerate(values):
                        if v and not _is_numeric(v):
                            col_headers[i] = v
                continue

            numeric_idx = [i for i in range(label_idx + 1, len(values))
                           if _is_numeric(values[i])]
            if not numeric_idx:
                continue
            if not header_scanned:
                for i, v in enumerate(values):
                    if v and not _is_numeric(v):
                        col_headers[i] = v
                header_scanned = True

            label = values[label_idx]
            for i in numeric_idx:
                col_note = col_headers.get(i, "")
                prov = f"row{r_idx}col{i + 1}"
                if col_note:
                    prov += f" ({col_note})"
                raw_items.append(RawItem(label=label, value_raw=values[i], provenance=prov))

    return raw_items, "\n".join(text_parts)
