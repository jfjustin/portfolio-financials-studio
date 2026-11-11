"""Master workbooks — one file carrying every fund and direct investment.

The team's *Investment Portfolio* workbook (one sheet per fund or company in
the team's layout, an accounting block beside it, a Summary-Cash Flows sheet,
a Summary-Portfolio Value sheet, planning tabs) and the accounting team's
*Equity Investments Summary*. These hold master data — type, round, dates,
cost, value, commitment, accounting fields, dated investments — for many
entities at once. Nothing in them is a quarter's statement, so no figures
are recorded from them: each entity's details are filled, dated investments
are added, and the file is kept as a submission so it can be re-read later.

Sheets that are none of these (an "Investments-AOP" planning tab, notes) are
left alone — a master workbook never adds an entity from a stray heading.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .. import db
from ..models import parse_number
from .base import classify_document

# the accounting team's "Equity Investments Summary" block, beside each entity
_ACCOUNTING = {
    "entity type": "entity_type", "method of accounting": "accounting_method",
    "gl account": "gl_account", "company code": "company_code",
    "ownership": "ownership_pct", "initial investment date": "initial_investment_date",
    "terms": "terms", "total commitment": "commitment",
    "remaining commitment": "future_draw", "total funding": "drawn_to_date",
    "total distributions": "distribution_to_date", "investment balance": "investment_balance",
    "total equity income loss": None,                 # derived, never entered
}
_ACCOUNTING_HEADING = re.compile(r"equity investments? summary", re.I)
_FUND_ONLY = {"commitment", "fund_size", "future_draw", "drawn_to_date", "distribution_to_date",
              "region_sector", "investment_balance", "stats_count", "stats_cost", "stats_value"}
_M = Decimal(1_000_000)


def _key(text: Any) -> str:
    from ..portfolio import _holding_key
    return _holding_key(text)


def _text(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return str(v).strip()


def _money(v: Any, scale: Decimal) -> Optional[str]:
    """A money cell as whole currency units. Entity sheets are in $M; the
    accounting block in whole units; a "$M" figure of 100,000 or more was
    typed in whole units already."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        n = Decimal(str(v))
    else:
        n = parse_number(_text(v))[0]
        if n is None:
            return None
        if re.search(r"\d\s*(m|mm|million)\b", _text(v), re.I):
            scale = Decimal(1)              # "150m" already carries its unit
    if scale != 1 and abs(n) >= 100_000:
        scale = Decimal(1)
    return str(n * scale)


def _entity_sheet(ws) -> Optional[dict[str, Any]]:
    """One entity's sheet in the team's layout: label | value pairs down the
    left ("Investment Name" first), the accounting block beside them, a
    "Latest Stats" row under its headers, and "Invested" rows. None when the
    sheet is not laid out like that."""
    from ..portfolio import COMPANY_FIELDS, FUND_FIELDS, _holding_columns
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if not any(_key(c) == "investment name" for r in rows[:6] for c in r[:4] if c):
        return None
    labels = {**_holding_columns("fund"), **_holding_columns("direct_investment")}
    labels.update({k: v for k, v in _ACCOUNTING.items()})
    labels.update({"investments": "security", "total received proceeds": "exit_proceeds",
                   "total proceeds on escrow": "exit_escrow", "commitment": "commitment",
                   "future draw": "future_draw"})
    money = {k for k, _l, t in FUND_FIELDS + COMPANY_FIELDS if t == "money"} | {"commitment"}
    acct_col: Optional[int] = None
    for r in rows[:6]:
        for i, c in enumerate(r):
            if isinstance(c, str) and _ACCOUNTING_HEADING.search(c):
                acct_col = i
    values: dict[str, str] = {}
    seen: set[str] = set()
    calls: list[dict[str, Any]] = []
    stats_headers: Optional[list[str]] = None
    for n, r in enumerate(rows):
        first = _text(r[0]) if r else ""
        if first.lower() == "invested" and len(r) >= 4:
            d = _text(r[1])
            amount = _money(r[3], _M)
            if d and amount:
                calls.append({"date": d, "description": _text(r[2]).strip(" -"),
                              "amount": amount, "total_raise": _money(r[4], _M) if len(r) > 4 else None,
                              "pre_money": _money(r[5], _M) if len(r) > 5 else None})
            continue
        if first.lower() == "latest stats" and stats_headers:
            for h, v in zip(stats_headers[1:], r[1:]):
                k = {"date": "stats_as_of", "of investments": "stats_count",
                     "cost basis": "stats_cost", "value": "stats_value"}.get(_key(h))
                if k and v not in (None, ""):
                    values.setdefault(k, _money(v, _M) if k in ("stats_cost", "stats_value")
                                      else _text(v))
            continue
        if len(r) > 2 and _key(r[1]) == "date":
            # a header row: the Latest Stats headers, or the Invested table's
            if any(_key(c) == "cost basis" for c in r if c):
                stats_headers = [_text(c) for c in r]
            continue
        for i, c in enumerate(r[:-1]):
            if not isinstance(c, str):
                continue
            k = labels.get(_key(c))
            if k is None or k == "name" or k in seen:
                continue
            v = r[i + 1]
            if v in (None, "", "-") or (isinstance(v, str) and _key(v) in labels):
                continue                       # a label beside a label is a header, not a value
            in_acct = acct_col is not None and i >= acct_col
            if k == "ownership_pct" and in_acct and isinstance(v, (int, float)) and 0 < v < 1:
                v = v * 100                        # 0.025 formatted as 2.50%
            text = (_money(v, Decimal(1) if in_acct else _M) if k in money else _text(v))
            if text in (None, ""):
                continue
            if k == "distribution_to_date":
                text = str(abs(Decimal(text)))     # printed as a negative in the block
            values[k] = text
            seen.add(k)
    if "display_name" not in values and "investment_name" not in values:
        return None
    is_fund = bool(_FUND_ONLY & set(values)) or values.get("investment_type", "").upper() in ("LP", "FUND")
    classified = classify_document(ws.title)
    if classified["entity_kind"] in ("fund", "direct_investment"):
        is_fund = classified["entity_kind"] == "fund"
    entity = classified["entity_name"] or ws.title.strip()
    commitment = values.pop("commitment", None)
    return {"entity": entity, "kind": "fund" if is_fund else "direct_investment",
            "values": values, "commitment": commitment, "calls": calls, "sheet": ws.title}


def read_master(path: Path) -> Optional[dict[str, Any]]:
    """{"records": [...], "sheets": [...], "skipped": [...]} when the workbook
    is a master workbook, else None."""
    import openpyxl
    from ..portfolio import _header_kind, read_holdings
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:
        return None
    records: list[dict[str, Any]] = []
    sheets: list[str] = []
    skipped: list[str] = []
    summary_rows: list[list[Any]] = []
    for ws in wb.worksheets:
        rec = _entity_sheet(ws)
        if rec:
            records.append(rec)
            sheets.append(ws.title)
            continue
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if any(_header_kind(r) for r in rows if any(c not in (None, "") for c in r)):
            summary_rows += [r for r in rows if any(c not in (None, "") for c in r)]
            sheets.append(ws.title)
        else:
            skipped.append(ws.title)
    if not records and not summary_rows:
        return None
    if records or len(sheets) >= 1:
        # a lone Summary-Portfolio Value sheet counts too: it names many entities
        pass
    by_entity = {r["entity"].lower(): r for r in records}
    if summary_rows:
        reading = read_holdings(summary_rows)
        for h in reading["records"]:
            rec = by_entity.get(h["name"].lower())
            if rec is None:
                rec = {"entity": h["name"], "kind": h["kind"], "values": {}, "commitment": None,
                       "calls": [], "sheet": "summary"}
                records.append(rec)
                by_entity[h["name"].lower()] = rec
            for k, v in h["body"].items():          # the entity's own sheet wins
                rec["values"].setdefault(k, v)
            if rec["commitment"] is None and h.get("commitment"):
                rec["commitment"] = h["commitment"]
        skipped += [f"row {e['row']} ({e['company']}): {e['message']}" for e in reading["errors"]]
    return {"records": records, "sheets": sheets, "skipped": skipped}


def apply_master(reading: dict[str, Any], user: str = "local") -> dict[str, Any]:
    """Write every record: details (merged; empty cells leave what is on file),
    the fund's commitment, and dated investments not already on file."""
    from .. import terms
    from ..portfolio import coerce_details
    existing = {e.lower(): e for e in db.all_details()}
    for f in db.get_facts():
        existing.setdefault(str(f["entity_name"]).lower(), f["entity_name"])
    done, errors, n_calls = [], [], 0
    for rec in reading["records"]:
        entity = existing.get(rec["entity"].lower(), rec["entity"])
        try:
            values = coerce_details(rec["values"])
        except ValueError as e:
            errors.append(f"{entity}: {e}")
            continue
        db.set_details(entity, {**values, "kind": rec["kind"]}, user=user)
        if rec.get("commitment"):
            prof = terms.fund_profile(entity)
            terms.set_fund_profile(entity, investor_aliases=prof.get("investor_aliases") or "",
                                   notes=prof.get("notes") or "", commitment=str(rec["commitment"]),
                                   user=user)
        have = {(c["call_date"], Decimal(c["amount"])) for c in db.get_capital_calls(entity)}
        for c in rec.get("calls", []):
            from ..portfolio import parse_date
            d = parse_date(c["date"])
            if d is None or (d, Decimal(c["amount"])) in have:
                continue
            db.add_capital_call(entity, d, Decimal(c["amount"]), c.get("description") or "",
                                total_raise=Decimal(c["total_raise"]) if c.get("total_raise") else None,
                                pre_money=Decimal(c["pre_money"]) if c.get("pre_money") else None,
                                user=user)
            n_calls += 1
        done.append({"entity": entity, "kind": rec["kind"], "fields": sorted(values),
                     "sheet": rec.get("sheet", "")})
    return {"updated": done, "errors": errors, "calls_added": n_calls}


def period_from_name(filename: str) -> str:
    """"Investment Portfolio Jun'26.xlsx" -> "2026-Q2"; "" when the name has no date."""
    from .base import detect_statement_dates, quarter_label
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*'?(\d{2}|\d{4})\b",
                  filename, re.I)
    if m:
        month = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                 "dec").index(m.group(1).lower()[:3]) + 1
        year = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
        return quarter_label(date(year, month, 1))
    dates = detect_statement_dates("", filename)
    return dates.quarter or ""
