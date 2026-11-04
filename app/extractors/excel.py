"""Deterministic Excel extraction (.xlsx/.xlsm).

Produces raw (label, value, provenance) items plus a text blob used for report-type
and period detection. Handles the two common financial-sheet shapes:
  1. Label column + value column(s)  ("Total Assets" | 1,234,567)
  2. Wide matrices (row labels x period columns). Every figure column is
     emitted with its column header ("Jan 2026", "Total", "Three Months Ended
     March 31, 2026" stacked from two header rows) so the assembler can pick
     the column that covers the reporting period.

We read computed values (data_only) so formulas resolve to numbers, and we NEVER
coerce blanks to zero — a missing cell stays missing so it can be flagged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import openpyxl
from openpyxl.utils import get_column_letter

from ..models import RawItem
from .base import compose_headers, header_row, statement_kind


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def extract_excel(path: Path) -> tuple[list[RawItem], str]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    raw_items: list[RawItem] = []
    text_parts: list[str] = []

    for ws in wb.worksheets:
        text_parts.append(f"# sheet: {ws.title}")
        heading = ""
        # Column headers, keyed by 0-based column index. Header rows above a
        # block of figures are stacked ("Three Months Ended March 31," over
        # "2026"); a later header block (a second table) replaces the columns
        # it names.
        col_headers: dict[int, str] = {}
        header_block: list[dict[int, str]] = []
        in_figures = False
        # which statement this sheet is: its name ("P&L", "1.1 Balance Sheet"),
        # else its title rows ("Statement of Cash Flows")
        kind = statement_kind(ws.title, sheet_name=True)
        sheet_has_figures = False

        for r_idx, row in enumerate(ws.iter_rows(), start=1):
            values = [c.value for c in row]
            # collect text for detection
            row_text = " ".join(
                str(v).strip() for v in values if v not in (None, ""))
            if row_text:
                text_parts.append(row_text)

            hdr = header_row({i: v for i, v in enumerate(values) if v not in (None, "")})
            if hdr is not None:
                if in_figures:
                    header_block, in_figures = [], False
                header_block.append(hdr)
                continue

            # identify label = first text cell; values = numeric cells to the right
            label_idx = None
            for i, v in enumerate(values):
                if isinstance(v, str) and v.strip():
                    label_idx = i
                    break
            if label_idx is None:
                continue

            numeric_idx = [i for i in range(label_idx + 1, len(values))
                           if _is_number(values[i])]
            if not numeric_idx:
                # a text-only row is a heading for the rows below it
                heading = str(values[label_idx]).strip()
                if len(heading) > 80:
                    heading = ""
                if not kind and not sheet_has_figures:
                    kind = statement_kind(row_text)
                continue
            if header_block and not in_figures:
                col_headers.update(compose_headers(header_block))
            in_figures = sheet_has_figures = True

            label = str(values[label_idx]).strip()
            # Emit ALL numeric columns so multi-period sheets aren't truncated;
            # the assembler picks the period's column (else the right-most).
            for i in numeric_idx:
                col_note = col_headers.get(i, "")
                coord = f"{get_column_letter(i + 1)}{r_idx}"
                prov = f"{ws.title}!{coord}"
                if col_note:
                    prov += f" ({col_note})"
                raw_items.append(RawItem(
                    label=label, value_raw=repr_number(values[i]), provenance=prov,
                    section=heading, statement=kind))

    wb.close()
    return raw_items, "\n".join(text_parts)


def repr_number(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
