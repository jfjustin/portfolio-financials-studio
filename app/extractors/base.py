"""Shared extraction helpers: period normalization, entity-name guessing."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

_MONTHS = {
    "january": 12, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
# fix january above (dict literal had 12 by mistake-proofing); set correctly:
_MONTHS["january"] = 1


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _q_from_month(m: int) -> int:
    return (m - 1) // 3 + 1


def normalize_period(text: str) -> Optional[str]:
    """Extract and normalize a reporting period from free text.

    Returns "2024-Q4", "2024-12-31", or "2024-FY" — or None if not found.
    Deterministic and conservative: when unsure it returns None so the human sets it.
    """
    if not text:
        return None
    t = text.lower()

    # explicit ISO date
    m = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # d/m/y or m/d/y ending in 4-digit year
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b", t)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        mo, d = (b, a) if a <= 12 and b > 12 else (a, b) if a <= 12 else (b, a)
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-{min(d,28):02d}" if d else f"{y:04d}-{mo:02d}"

    # "December 31, 2024" / "31 December 2024"
    m = re.search(r"\b([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})\b", t)
    if m and m.group(1) in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})\s+([a-z]{3,9})\.?\s+(20\d{2})\b", t)
    if m and m.group(2) in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"

    # "Q4 2024" / "2024 Q4" / "fourth quarter 2024"
    m = re.search(r"\bq([1-4])[\s\-]*?(20\d{2})\b", t) or \
        re.search(r"\b(20\d{2})[\s\-]*?q([1-4])\b", t)
    if m:
        g1, g2 = m.group(1), m.group(2)
        q, y = (g1, g2) if g1 in "1234" else (g2, g1)
        return f"{int(y):04d}-Q{q}"

    # "FY2024" / "fiscal year 2024" / "year ended ... 2024"
    m = re.search(r"\b(?:fy|fiscal year|financial year|year ended.*?)\s*(20\d{2})\b", t)
    if m:
        return f"{int(m.group(1)):04d}-FY"

    # bare month + year
    m = re.search(r"\b([a-z]{3,9})\.?\s+(20\d{2})\b", t)
    if m and m.group(1) in _MONTHS:
        return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1)]:02d}"

    return None


_ENTITY_HINT = re.compile(
    r"(fund|partners|capital|ventures?|holdings?|jv|joint venture|llc|l\.?p\.?|"
    r"limited|ltd|inc|co\.?|company|group|technologies?|corp)", re.I)


def classify_document(filename: str) -> dict[str, Optional[str]]:
    """Classify an investment document from its filename.

    Ported from the fund-intelligence classifier. Detects the entity
    (Fund N / Company X / Note X), its kind, and the document type from common
    naming conventions. Returns {entity_name, entity_kind, document_type}; any
    field may be None when it can't be determined.
    """
    normalized = re.sub(r"[_\-]+", " ", filename.lower())
    fund = re.search(r"\bfund\s*(\d+[a-z]?)\b", normalized)
    company = re.search(r"\bcompany\s+(0?\d*[a-z]|[a-z])\b", normalized)
    note = re.search(r"\bnote\s+(\d+[a-z])\b", normalized)

    if fund:
        entity_name = f"Fund {fund.group(1).upper()}"
        entity_kind = "fund"
    elif company:
        entity_name = f"Company {company.group(1).upper()}"
        entity_kind = "direct_investment"
    elif note:
        entity_name = f"Note {note.group(1).upper()}"
        entity_kind = "direct_investment"
    else:
        entity_name = None
        entity_kind = "unknown"

    doc_type_patterns = [
        (r"pcap|capital\s+account|partner.?s\s+capital", "Partner Capital Statement"),
        (r"capital\s+call|contribution", "Capital Call"),
        (r"summary\s+of\s+investments|investment\s+schedule", "Investment Schedule"),
        (r"balance\s+sheet", "Balance Sheet"),
        (r"cash\s+flow", "Cash Flow Statement"),
        (r"p[& ]?l|profit|income\s+statement", "Income Statement"),
        (r"equity\s+income|equity\s+in\s+earnings", "JV Equity Income / (Loss)"),
        (r"financial|statement|report|results", "Financial Statements"),
    ]
    document_type = "Investment Document"
    for pat, label in doc_type_patterns:
        if re.search(pat, normalized):
            document_type = label
            break

    return {"entity_name": entity_name, "entity_kind": entity_kind,
            "document_type": document_type}


def guess_entity_name(lines: list[str], filename: str = "") -> str:
    """Best-effort entity name from the top lines of a document or the filename."""
    for ln in lines[:12]:
        s = ln.strip()
        if 3 <= len(s) <= 80 and _ENTITY_HINT.search(s) and not re.search(
                r"\b(statement|report|balance|income|financial|schedule|as of|"
                r"period|quarter|unaudited|audited)\b", s, re.I):
            return re.sub(r"\s+", " ", s).strip(" :.-")
    # fall back to filename (strip dates/type words)
    stem = Path(filename).stem
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\b(20\d{2}|q[1-4]|fy|financials?|report|jv|fund)\b", "", stem, flags=re.I)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem.title() if stem else "UNKNOWN"
