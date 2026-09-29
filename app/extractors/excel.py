"""Deterministic Excel extraction (.xlsx/.xlsm).

Produces raw (label, value, provenance) items plus a text blob used for report-type
and period detection. Handles the two common financial-sheet shapes:
  1. Label column + value column(s)  ("Total Assets" | 1,234,567)
  2. Wide matrices (row labels x period columns) — takes the right-most numeric.

We read computed values (data_only) so formulas resolve to numbers, and we NEVER
coerce blanks to zero — a missing cell stays missing so it can be flagged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import openpyxl
from openpyxl.utils import get_column_letter

from ..models import RawItem


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def extract_excel(path: Path) -> tuple[list[RawItem], str]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    raw_items: list[RawItem] = []
    text_parts: list[str] = []

    for ws in wb.worksheets:
        text_parts.append(f"# sheet: {ws.title}")
        # Grab column header labels (first non-empty row with mostly text) to
        # annotate wide matrices with their period column. Keyed by 0-based index.
        col_headers: dict[int, str] = {}
        header_scanned = False

        for r_idx, row in enumerate(ws.iter_rows(), start=1):
            values = [c.value for c in row]
            # collect text for detection
            row_text = " ".join(
                str(v).strip() for v in values if v not in (None, ""))
            if row_text:
                text_parts.append(row_text)

            # identify label = first text cell; values = numeric cells to the right
            label_idx = None
            for i, v in enumerate(values):
                if isinstance(v, str) and v.strip():
                    label_idx = i
                    break
            if label_idx is None:
                # possibly a header row of period labels
                if not header_scanned:
                    for i, v in enumerate(values):
                        if isinstance(v, str) and v.strip():
                            col_headers[i] = v.strip()
                continue

            numeric_idx = [i for i in range(label_idx + 1, len(values))
                           if _is_number(values[i])]
            if not numeric_idx:
                continue
            # capture header row once (labels above the first data row)
            if not header_scanned:
                for i, v in enumerate(values):
                    if isinstance(v, str) and v.strip():
                        col_headers[i] = v.strip()
                header_scanned = True

            label = str(values[label_idx]).strip()
            # Emit ALL numeric columns so multi-period sheets aren't truncated;
            # the assembler prefers the right-most (most recent) on conflict.
            for i in numeric_idx:
                col_note = col_headers.get(i, "")
                coord = f"{get_column_letter(i + 1)}{r_idx}"
                prov = f"{ws.title}!{coord}"
                if col_note:
                    prov += f" ({col_note})"
                raw_items.append(RawItem(
                    label=label, value_raw=repr_number(values[i]), provenance=prov))

    wb.close()
    return raw_items, "\n".join(text_parts)


def repr_number(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
