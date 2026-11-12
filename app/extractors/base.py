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
    r"|\b(?P<mon>[a-z]{3,9})\.?\s+(?P<md>\d{1,2}),?\s+(?P<my>20\d{2})\b"
    # "31 March 2026", "31-Mar-26", "31 Mar. 2026" — but not the "1-March 31" of
    # "January 1-March 31, 2026", where the two digits are a day, not a year
    r"|\b(?P<dd>\d{1,2})[\s\-]+(?P<mon2>[a-z]{3,9})\.?[\s\-]+"
    r"(?P<yy>20\d{2}\b|\d{2}\b(?!,?\s*20\d{2}))",
    re.I)
_QUARTER_RX = re.compile(r"\bq([1-4])[\s,\-]*(20\d{2})\b|\b(20\d{2})[\s,\-]*q([1-4])\b", re.I)
_BOOKING_CTX = re.compile(r"(month|date posted|posting date|posted|prepared|printed)\s*:?\s*$", re.I)
_START_CTX = re.compile(r"(beginning[^\n]{0,80}?\bas of|\bfrom|\bsince|\bopening[^\n]{0,40}?\bas of)\s*$",
                        re.I)
_END_CTX = re.compile(r"(\bended|\bending[^\n]{0,80}?\bas of|\bthrough|\bperiod end(?:ed|ing)?:?)\s*$",
                      re.I)


@dataclass
class StatementDates:
    """Dates a statement carries, by role. `quarter` is "YYYY-Qn"."""
    quarter: Optional[str] = None
    explicit_quarter: bool = False          # "Q1 2026" named in the filename
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
        elif m.group("mon"):
            mon = _MONTHS.get(m.group("mon").lower())
            d = _safe_date(int(m.group("my")), mon, int(m.group("md"))) if mon else None
        else:
            mon = _MONTHS.get(m.group("mon2").lower())
            yy = int(m.group("yy"))
            d = _safe_date(yy if yy > 99 else 2000 + yy, mon, int(m.group("dd"))) if mon else None
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
        out.quarter, out.explicit_quarter = explicit_q, True
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
        # a planning line ("Follow-on 1 Company Q EU/ Devices H1 2026 1 25") is
        # not a name: too many words, or a half-year / quarter and figures in it
        if len(s.split()) > 5 or re.search(r"\b(h[12]|q[1-4])\s*'?\d{2,4}\b", s, re.I):
            continue
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


# --------------------------------------------------------------------------
# Financial statements: which statement a sheet/page is, and which figure
# column covers the reporting period
# --------------------------------------------------------------------------
_STATEMENT_TITLES = (
    ("cash_flow", re.compile(r"cash\s*flows?", re.I)),
    ("balance_sheet", re.compile(r"balance\s*sheets?|financial\s+(?:position|condition)", re.I)),
    ("income_statement", re.compile(
        r"profit\s*(?:and|&)\s*loss|income\s+statements?|statements?\s+of\s+"
        r"(?:consolidated\s+)?(?:comprehensive\s+)?(?:operations|income|loss)", re.I)),
)
_SHEET_ABBREV = (("cash_flow", re.compile(r"^\W*cf\W*$", re.I)),
                 ("balance_sheet", re.compile(r"^\W*bs\W*$", re.I)),
                 ("income_statement", re.compile(r"^\W*(?:p\s*&\s*l|pl|is)\W*$", re.I)))


def statement_kind(title: str, *, sheet_name: bool = False) -> str:
    """"balance_sheet" | "income_statement" | "cash_flow" for a statement title
    ("Statement of Cash Flows", "1.1 Balance Sheet", a "P&L" sheet), else ""."""
    t = title or ""
    if sheet_name:
        for kind, rx in _SHEET_ABBREV:
            if rx.match(t.strip()):
                return kind
    best: Optional[tuple[int, str]] = None
    for kind, rx in _STATEMENT_TITLES:
        m = rx.search(t)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), kind)
    return best[1] if best else ""


_HEADERISH = re.compile(
    r"\b(?:19|20)\d{2}\b|'\d{2}\b|\bq[1-4]\b|quarter|months?\b|\bytd\b|year|total|budget|actual|"
    r"forecast|variance|period|ended|ending|as of|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|"
    r"nov|dec)[a-z]*\b", re.I)


def header_row(cells: dict[int, object]) -> Optional[dict[int, str]]:
    """{column: text} when a row of cells is a column-header row ("Jan 2026 |
    Feb 2026 | Total", "Three Months Ended March 31, | 2026", "Q1'26 | Budget |
    Actual"), else None. Bare years count as header text; any other figure
    makes it a data row. A text-only row in the label column is a heading."""
    texts: dict[int, str] = {}
    years = 0
    for i, v in cells.items():
        if v in (None, ""):
            continue
        if hasattr(v, "isoformat"):                      # a date header cell
            texts[i] = v.isoformat()[:10]
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            if float(v).is_integer() and 1900 <= v <= 2100:
                texts[i] = str(int(v))
                years += 1
            else:
                return None
        else:
            s = str(v).strip()
            if re.fullmatch(r"[$€£(]*-?[\d,.]+[)%]*", s) and not re.fullmatch(r"(?:19|20)\d{2}", s):
                return None                               # a figure: a data row
            if re.fullmatch(r"(?:19|20)\d{2}", s):
                years += 1
            texts[i] = s
    if not texts:
        return None
    first = min(texts)
    words = {i: t for i, t in texts.items() if not re.fullmatch(r"(?:19|20)\d{2}", t)}
    beyond = {i: t for i, t in words.items() if i > first}
    if not beyond and first in words and (first == 0 or years):
        return None              # a heading, or a label with year-like figures
    if not any(_HEADERISH.search(t) for t in (beyond or texts).values()):
        return None
    return texts


def compose_headers(rows: list[dict[int, str]]) -> dict[int, str]:
    """Stack header rows into one header per column. A spanning header ("Q1'26"
    over Budget | Actual, "Three Months Ended" over its year) carries right
    until the next header cell in its row."""
    if not rows:
        return {}
    last = max(max(r) for r in rows if r)
    out: dict[int, list[str]] = {}
    for r in rows:
        cols = sorted(r)
        for k, c in enumerate(cols):
            end = cols[k + 1] if k + 1 < len(cols) else last + 1
            for i in range(c, end):
                out.setdefault(i, []).append(r[c])
    return {i: " ".join(t for t in parts if t).strip() for i, parts in out.items()}


_MONTH_RX = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\b", re.I)
_FULL_PERIOD = re.compile(r"\btotal\b|three\s+months|3\s+months|\bquarter|\bqtd\b|"
                          r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*"
                          r"(?:\d{1,2},?\s*)?(?:-|–|to|through)\s*"
                          r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)
_NOT_ACTUAL = re.compile(r"budget|forecast|\bplan\b|projection|variance|\bvar\b|%|"
                         r"prior|\bpy\b|last\s+year|change", re.I)


def column_fit(header: str, period: str, *, balance: bool) -> int:
    """How well a figure column's header fits the reporting period (higher is
    better; 0 = no opinion). Flows want the column covering the whole period
    ("Total", "Three Months Ended March 31, 2026", "Q1'26 Actual"), never one
    month of it or another year; balances want the period-end column
    ("Mar 2026" of Jan | Feb | Mar). Budget, variance and prior-year columns
    are never the actuals."""
    h = (header or "").strip()
    if not h or not period:
        return 0
    score = 0
    if _NOT_ACTUAL.search(h):
        score -= 12
    years = set(re.findall(r"\b((?:19|20)\d{2})\b", h)) | {
        "20" + y for y in re.findall(r"'(\d{2})\b", h)}
    pyear = period[:4]
    if years:
        score += 3 if pyear in years else -5
    q = re.search(r"-Q([1-4])$", period)
    hq = re.findall(r"\bq([1-4])\b|q([1-4])'", h, re.I)
    hq = {a or b for a, b in hq}
    if q and hq:
        score += 3 if q.group(1) in hq else -5
    months = [m.lower()[:3] for m in _MONTH_RX.findall(h)]
    end_month = None
    if q:
        end_month = ("mar", "jun", "sep", "dec")[int(q.group(1)) - 1]
    elif re.fullmatch(r"20\d{2}-\d{2}(?:-\d{2})?", period):
        end_month = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                     "nov", "dec")[int(period[5:7]) - 1]
    if balance:
        if end_month and end_month in months:
            score += 2
    else:
        if _FULL_PERIOD.search(h):
            score += 2
        elif q and len(months) == 1 and not re.search(r"ended|ending|through", h, re.I):
            score -= 3                     # one month of the quarter
    return score


def months_in_period(period: str, start: Optional[str] = None,
                     end: Optional[str] = None) -> Optional[int]:
    """Calendar months a period covers: a quarter 3, a month 1, a year 12."""
    if start and end:
        try:
            s, e = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
            n = (e.year - s.year) * 12 + e.month - s.month + 1
            if n >= 1:
                return n
        except ValueError:
            pass
    p = period or ""
    if re.fullmatch(r"20\d{2}-Q[1-4]", p):
        return 3
    if re.fullmatch(r"20\d{2}-H[12]", p):
        return 6
    if re.fullmatch(r"20\d{2}-\d{2}", p):
        return 1
    if re.fullmatch(r"20\d{2}(?:-FY)?", p):
        return 12
    return None
