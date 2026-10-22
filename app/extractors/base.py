"""Shared extraction helpers: period normalization, entity-name guessing."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
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


def _safe_date(y: int, mo: int, d: int) -> Optional[date]:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _mdy(a: int, b: int, y: int) -> Optional[date]:
    """Read "a/b/yyyy" as US month/day, falling back to day/month when only
    that reading is a real date (e.g. "31/03/2026")."""
    return _safe_date(y, a, b) or _safe_date(y, b, a)


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
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d.isoformat()

    # m/d/y (US) or d/m/y ending in 4-digit year
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b", t)
    if m:
        d = _mdy(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d.isoformat()

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


# --------------------------------------------------------------------------
# Statement quarter inference
#
# Capital-account statements are often year-to-date and carry several dates
# that are NOT the period: a "beginning ... as of January 1" opening date, a
# journal-voucher booking month ("MONTH : 2026-06-28"), a posting date. We infer
# the calendar quarter the statement reports on from the dates' roles, never
# just the first date we see.
# --------------------------------------------------------------------------
_DATE_RX = re.compile(
    r"\b(?P<iso>20\d{2})[-/.](?P<iso_m>\d{1,2})[-/.](?P<iso_d>\d{1,2})\b"
    r"|\b(?P<a>\d{1,2})[-/.](?P<b>\d{1,2})[-/.](?P<y>20\d{2})\b"
    r"|\b(?P<mon>[a-z]{3,9})\.?\s+(?P<md>\d{1,2}),?\s+(?P<my>20\d{2})\b",
    re.I)
_QUARTER_RX = re.compile(r"\bq([1-4])[\s\-]*(20\d{2})\b|\b(20\d{2})[\s\-]*q([1-4])\b", re.I)
_BOOKING_CTX = re.compile(r"(month|date posted|posting date|posted|prepared|printed)\s*:?\s*$", re.I)
_START_CTX = re.compile(r"(beginning[^\n]{0,80}?\bas of|\bfrom|\bsince|\bopening[^\n]{0,40}?\bas of)\s*$",
                        re.I)
_END_CTX = re.compile(r"(\bended|\bending[^\n]{0,80}?\bas of|\bthrough|\bperiod end(?:ed|ing)?:?)\s*$",
                      re.I)


@dataclass
class StatementDates:
    """Dates a statement carries, by role. `quarter` is "YYYY-Qn"."""
    quarter: Optional[str] = None
    statement_start: Optional[date] = None  # coverage start the document states
    statement_end: Optional[date] = None    # results date the document states
    booking_date: Optional[date] = None     # e.g. a JV voucher's booking month


def _iter_dates(text: str):
    """Yield (date, preceding_context, following_context) for every date in text."""
    for m in _DATE_RX.finditer(text):
        if m.group("iso"):
            d = _safe_date(int(m.group("iso")), int(m.group("iso_m")), int(m.group("iso_d")))
        elif m.group("y"):
            d = _mdy(int(m.group("a")), int(m.group("b")), int(m.group("y")))
        else:
            mon = _MONTHS.get(m.group("mon").lower())
            d = _safe_date(int(m.group("my")), mon, int(m.group("md"))) if mon else None
        if d:
            yield d, text[max(0, m.start() - 90):m.start()], text[m.end():m.end() + 12]


def quarter_label(d: date) -> str:
    return f"{d.year:04d}-Q{_q_from_month(d.month)}"


def quarter_bounds(label: str) -> tuple[date, date]:
    """("2026-Q2") -> (2026-04-01, 2026-06-30)."""
    y, q = int(label[:4]), int(label[-1])
    start = date(y, 3 * q - 2, 1)
    nxt = date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)
    return start, nxt - timedelta(days=1)


def previous_quarter(label: str) -> str:
    y, q = int(label[:4]), int(label[-1])
    return f"{y - 1:04d}-Q4" if q == 1 else f"{y:04d}-Q{q - 1}"


def next_quarter(label: str) -> str:
    y, q = int(label[:4]), int(label[-1])
    return f"{y + 1:04d}-Q1" if q == 4 else f"{y:04d}-Q{q + 1}"


def is_quarter_label(period: Optional[str]) -> bool:
    return bool(period) and re.fullmatch(r"20\d{2}-Q[1-4]", period) is not None


def detect_statement_dates(text: str, filename: str) -> StatementDates:
    """Infer which calendar quarter a statement reports on.

    Priority: an explicit "Q2 2026" in the filename, then a date in the filename
    ("03-31-2026 results"), then a results date in the text ("for the period
    ended June 30, 2026"), then the first date that isn't a booking or opening
    date. A year-to-date statement ending June 30 is therefore Q2, not "H1"/FY.
    """
    out = StatementDates()
    fname = re.sub(r"[_]+", " ", Path(filename).stem)

    m = _QUARTER_RX.search(fname)
    explicit_q = None
    if m:
        q, y = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        explicit_q = f"{int(y):04d}-Q{q}"

    fname_dates = [d for d, _pre, _post in _iter_dates(fname)]

    text_end: Optional[date] = None
    fallback: Optional[date] = None
    for d, pre, post in _iter_dates(text or ""):
        if _BOOKING_CTX.search(pre):
            out.booking_date = out.booking_date or d
        elif _START_CTX.search(pre):
            out.statement_start = out.statement_start or d
        elif _END_CTX.search(pre) or re.match(r"\s*results\b", post, re.I):
            text_end = text_end or d
        elif fallback is None:
            fallback = d

    out.statement_end = (fname_dates[0] if fname_dates else None) or text_end or fallback
    if explicit_q:
        out.quarter = explicit_q
    elif out.statement_end:
        out.quarter = quarter_label(out.statement_end)
    return out


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
