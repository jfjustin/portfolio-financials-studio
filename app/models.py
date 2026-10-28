"""Pydantic models for the canonical data flowing through the pipeline."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .report_types import REPORT_TYPES, Unit


class FactStatus(str, Enum):
    AUTO = "auto"            # auto-accepted (high confidence, passed validation)
    NEEDS_REVIEW = "needs_review"  # low confidence or failed a check — human must confirm
    CONFIRMED = "confirmed"  # a human confirmed/edited it
    REJECTED = "rejected"    # a human rejected it


class RawItem(BaseModel):
    """A raw label/value pair pulled from a document before canonical mapping."""
    label: str
    value_raw: str
    provenance: str = ""     # e.g. "p3 table2 r5c2" or "Sheet1!B7"
    section: str = ""        # nearest heading above the line ("Unrealised Capital Gains")


class FinancialFact(BaseModel):
    """One normalized data point. This is the atom the whole system trades in."""
    entity_name: str
    entity_kind: str
    report_type: str
    period: str                       # normalized, e.g. "2024-Q4" or "2024-12-31"
    metric_key: str
    metric_label: str
    value: Optional[Decimal] = None
    unit: Unit = Unit.CURRENCY
    currency: str = "USD"
    raw_label: str = ""
    raw_value: str = ""
    provenance: str = ""
    confidence: float = 0.0
    status: FactStatus = FactStatus.NEEDS_REVIEW
    flags: list[str] = Field(default_factory=list)  # validation messages
    # value as read from the document, when the recorded `value` was derived from
    # it (carried forward from the prior quarter, or de-cumulated from YTD)
    reported_value: Optional[Decimal] = None
    # heading above each " + "-joined component of raw_label (format memory)
    raw_sections: list[str] = Field(default_factory=list)

    model_config = {"use_enum_values": True}


class ExtractionResult(BaseModel):
    """Everything an extractor learned about one uploaded file."""
    filename: str
    file_hash: str
    detected_report_type: str
    report_type_confidence: float
    entity_name: str = "UNKNOWN"
    period: str = "UNKNOWN"
    period_start: Optional[str] = None     # calendar bounds of `period` (ISO dates)
    period_end: Optional[str] = None
    statement_start: Optional[str] = None  # coverage start the document states (YTD → Jan 1)
    booking_date: Optional[str] = None     # e.g. a JV voucher's booking month — metadata only
    currency: str = "USD"
    number_format: str = "1,234.56"   # or "1.234,56" (European)
    extractor: str = ""               # which extractor produced this
    facts: list[FinancialFact] = Field(default_factory=list)
    unmapped: list[RawItem] = Field(default_factory=list)  # found but not mapped
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Value parsing — accounting notation, currency symbols, thousands, %, k/m/bn
# --------------------------------------------------------------------------
_CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "¥": "CNY", "rmb": "CNY", "cny": "CNY", "元": "CNY",
    "hk$": "HKD", "hkd": "HKD",
    "sgd": "SGD", "s$": "SGD",
}

_SCALE = {"k": 1_000, "thousand": 1_000,
          "m": 1_000_000, "mm": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
          "bn": 1_000_000_000, "b": 1_000_000_000, "billion": 1_000_000_000}


_CCY_STATED = re.compile(
    r"(?:figures|amounts|expressed|presented|reported|stated)\s+in\s+(?:thousands\s+of\s+)?"
    r"(usd|eur|gbp|chf|jpy|cny|rmb|hkd|sgd|cad|aud|sek|nok|dkk)\b"
    r"|\(\s*(usd|eur|gbp|chf|jpy|cny|rmb|hkd|sgd|cad|aud|sek|nok|dkk)\s*\)"
    r"|\bin\s+(usd|eur|gbp|chf|jpy|cny|rmb|hkd|sgd|cad|aud|sek|nok|dkk)\b", re.I)
_CCY_CODE = re.compile(r"\b(usd|eur|gbp|chf|jpy|cny|rmb|hkd|sgd|cad|aud|sek|nok|dkk)\b", re.I)
_CCY_SIGN = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "CNY"}


def detect_currency(text: str) -> Optional[str]:
    """The currency a document reports in. A stated reporting currency
    ("all figures in €", "(EUR)" column headers, "Expressed in United States
    dollars") outweighs codes that merely appear (e.g. portfolio companies listed
    in other currencies); then the most frequent code or sign wins."""
    t = text or ""
    votes: dict[str, float] = {}

    def vote(code: str, w: float) -> None:
        code = {"RMB": "CNY"}.get(code.upper(), code.upper())
        votes[code] = votes.get(code, 0) + w

    def fx_table(m) -> bool:
        # currencies named in an FX-rate table ("China CNY B/S Rate", "EUR FX Rate
        # USD") aren't what the document reports in
        return re.search(r"\b(rates?|fx)\b", t[max(0, m.start() - 25):m.end() + 25], re.I) is not None

    for m in _CCY_STATED.finditer(t):
        if fx_table(m):
            continue
        stated = m.group(1) or m.group(3)
        vote(next(g for g in m.groups() if g), 5 if stated else 2)   # "(EUR)" headers count less
    for m in re.finditer(r"(?:figures|amounts)\s+in\s+([€$£¥])", t, re.I):
        vote(_CCY_SIGN[m.group(1)], 5)
    if re.search(r"expressed in united states dollars|in u\.?s\.? dollars", t, re.I):
        vote("USD", 5)
    if re.search(r"expressed in euros?\b|in euros?\b", t, re.I):
        vote("EUR", 5)
    for m in _CCY_CODE.finditer(t):
        if not fx_table(m):
            vote(m.group(1), 1)
    for sign, code in _CCY_SIGN.items():
        n = t.count(sign)
        if n:
            vote(code, min(n, 50) * 0.5)
    for alias, code in (("us$", "USD"), ("hk$", "HKD"), ("s$", "SGD"), ("元", "CNY")):
        if alias in t.lower():
            vote(code, 2)
    return max(votes, key=votes.get) if votes else None


_EU_GROUPED = re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+(?:,\d+)?(?![\d.,])")
_US_GROUPED = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d.,])")


def detect_decimal_comma(text: str) -> bool:
    """True when a document writes numbers European-style ("420.318.775",
    "1.234,56") rather than "420,318,775" — decided by which grouping dominates."""
    return len(_EU_GROUPED.findall(text or "")) > len(_US_GROUPED.findall(text or ""))


def parse_number(raw: str, decimal_comma: Optional[bool] = None
                 ) -> tuple[Optional[Decimal], list[str]]:
    """Parse a financial value robustly. Returns (value, notes).

    Handles: $1,234.50 | (1,234) negative | 45% | 1.2m | 3.4bn | – / — (empty) | -
    and European notation (1.234.567 / 1.234,56) — `decimal_comma` is the
    document-level style from detect_decimal_comma(); None = infer per value.
    Returns None when there is genuinely no number (so it can be flagged, not zeroed).
    """
    notes: list[str] = []
    if raw is None:
        return None, ["empty"]
    s = str(raw).strip()
    if s in {"", "-", "–", "—", "n/a", "na", "nil", "none", "N/A"}:
        return None, ["empty_or_dash"]

    negative = False
    # accounting parentheses => negative
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]

    is_percent = "%" in s
    scale = 1
    low = s.lower()
    for token, mult in _SCALE.items():
        if re.search(rf"\d\s*{re.escape(token)}\b", low):
            scale = mult
            break

    # strip everything except digits, separators, sign
    cleaned = re.sub(r"[^0-9.,\-]", "", s)
    # a minus written after a space ("- 1.234") or a leading "-" both mean negative
    if cleaned.startswith("-") and not negative:
        negative, cleaned = True, cleaned.lstrip("-")
    if decimal_comma and not (cleaned.count(",") and cleaned.count(".")):
        # document writes 1.234.567 / 1.234,56: dots group thousands
        if cleaned.count(".") and (cleaned.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", cleaned)):
            cleaned = cleaned.replace(".", "")
        cleaned = cleaned.replace(",", ".")
    elif cleaned.count(".") > 1 and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")   # "420.318.775" can only be grouping
    # handle European "1.234,56" vs US "1,234.56"
    if cleaned.count(",") and cleaned.count("."):
        if cleaned.rfind(",") > cleaned.rfind("."):   # comma is decimal sep
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") and not cleaned.count("."):
        # ambiguous: "1,234" thousands vs "1,23" decimal — assume thousands if 3 trailing
        if re.search(r",\d{3}\b", cleaned):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")

    if cleaned in {"", "-", ".", "-."}:
        return None, ["no_digits"]

    try:
        val = Decimal(cleaned)
    except InvalidOperation:
        return None, [f"unparseable:{raw!r}"]

    if negative:
        val = -val
    if scale != 1:
        val = val * scale
        notes.append(f"scaled_x{scale}")
    if is_percent:
        notes.append("percent")
    return val, notes


# --------------------------------------------------------------------------
# Label keys — how a learned/taught statement line is recognised next time.
# Quarters, years, dates and gain/loss wording change every quarter
# ("Called in Q1, 2026" -> "Called in Q2, 2026"; "Capital Loss" -> "Capital
# Gain"), so they're normalised to placeholders.
# --------------------------------------------------------------------------
_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")


def label_key(label: str) -> str:
    s = (label or "").lower().replace("\u2019", "'")
    s = re.sub(r"\[[^\]]*\]", " ", s)                       # "[note 5]"
    s = re.sub(r"\bhl(?=[a-z])", "in", s)                     # OCR "hlterest"
    s = re.sub(rf"\b\d{{1,2}}[\s\-/.]+{_MON}\.?[\s\-/.,]+(?:20)?\d{{2}}\b", " <date> ", s)
    s = re.sub(rf"\b{_MON}\.?\s+\d{{1,2}},?\s+20\d{{2}}\b", " <date> ", s)
    s = re.sub(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.](?:20)?\d{2}\b", " <date> ", s)
    s = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " <date> ", s)
    s = re.sub(r"\bq[1-4]\b|\b(?:first|second|third|fourth) quarter\b", " <q> ", s)
    s = re.sub(rf"\b{_MON}\b", " <month> ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " <year> ", s)
    s = re.sub(r"\(?\b(?:gains?|loss(?:es)?)\b\)?(?:\s*/\s*\(?\b(?:gains?|loss(?:es)?)\b\)?)?",
               " <gain/loss> ", s)
    s = re.sub(r"(?:<gain/loss>\s*)+", "<gain/loss> ", s)
    s = re.sub(r"[^a-z0-9<>/%& ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# Label -> canonical metric mapping
# --------------------------------------------------------------------------
_SINCE_INCEPTION = re.compile(
    r"closing date|inception|cumulative|life[- ]to[- ]date|\bltd\b|since (?:the )?(?:fund|first)"
    r"|(?<!year )\bto date\b|in previous quarters")


def is_since_inception_label(label: str) -> bool:
    """True for cumulative-since-inception lines, which aren't period activity."""
    return bool(_SINCE_INCEPTION.search(label.lower()))

def map_label_to_metric(report_type_key: str, label: str) -> tuple[Optional[str], float]:
    """Map a raw label to a canonical metric key for the given report type.

    Returns (metric_key or None, confidence). Uses longest-synonym-wins so that
    "total net assets" beats a bare "assets". Exact/whole matches score highest.
    """
    rt = REPORT_TYPES.get(report_type_key)
    if not rt:
        return None, 0.0
    norm = re.sub(r"\s+", " ", label.strip().lower())
    norm = norm.strip(" :.-\t")
    # QuickBooks Online / generic ledger exports phrase subtotals as
    # "Total for X" rather than "Total X" (e.g. "Total for Income",
    # "Total for Expenses") — normalize so the existing "total x" synonyms
    # still match this very common real-world export convention.
    norm = re.sub(r"^total for\s+", "total ", norm)
    # OCR'd / redacted PDFs often render a capital "I" as "hl" ("hlterest income",
    # "hlitial Closing Date").
    norm = re.sub(r"\bhl(?=[a-z])", "in", norm)
    # Since-inception / cumulative lines ("Capital contributions from Initial
    # Closing Date through June 30, 2026") must never land in a period field.
    if is_since_inception_label(norm):
        for m in rt.metrics:
            if m.since_inception and any(syn in norm for syn in m.synonyms):
                return m.key, 0.9
        return None, 0.0
    best_key: Optional[str] = None
    best_score = 0.0
    best_syn_len = 0
    for m in rt.metrics:
        if m.since_inception:
            continue
        for syn in m.synonyms:
            if syn == norm:
                score = 1.0
            elif norm.startswith(syn) or norm.endswith(syn):
                score = 0.9
            elif syn in norm:
                score = 0.75
            else:
                continue
            # prefer the longest matching synonym on ties
            if score > best_score or (score == best_score and len(syn) > best_syn_len):
                best_key, best_score, best_syn_len = m.key, score, len(syn)
    return best_key, round(best_score, 3)
