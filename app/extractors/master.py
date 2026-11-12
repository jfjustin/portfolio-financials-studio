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


# --------------------------------------------------------------------------
# The accounting team's Equity Investments Summary workbook
# --------------------------------------------------------------------------
# its "Investments Summary" table, per kind of entity
_SUMMARY_COLS = {
    "investment balance": "balance", "gross investment funding": "funding",
    "return of capital other cash proceeds": "returned", "return of capital": "returned",
    "cumulative impairment": "impairment", "total commitment": "commitment",
    "remaining commitment": "future_draw", "ownership": "ownership_pct",
    "initial investment date": "initial_investment_date",
}
_SECTION_FUNDS = re.compile(r"^funds?$", re.I)
_SECTION_CLOSED = re.compile(r"^closed investments$", re.I)
_SECTION_SKIP = re.compile(r"^other$", re.I)
_SECTION_NOTES = re.compile(r"note receivable|held-to-maturity|\bhtm\b", re.I)


def _num(v: Any) -> Optional[Decimal]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return Decimal(str(v))
    t = _text(v)
    if not t or t.startswith("<") or t.lower() in ("n/a", "na", "-"):
        return None
    return parse_number(t)[0]


def _amt(n: Optional[Decimal]) -> Optional[str]:
    """A whole-unit amount to the cent; a float's rounding residue is nil."""
    if n is None:
        return None
    q = n.quantize(Decimal("0.01"))
    if q == 0:
        return "0"
    t = format(q, "f")                     # never "5E+6": that reads back as 56
    return t[:-3] if t.endswith(".00") else t


def _pct(v: Any) -> Optional[str]:
    """An ownership cell: 0.0751 (a fraction) or 7.51 -> "7.51"; "< 1%" -> None."""
    n = _num(v)
    if n is None:
        return None
    return str(n * 100 if n < 1 else n)


def _summary_table(ws) -> Optional[dict[str, Any]]:
    """The "Investments Summary" sheet: one row per entity under section
    headings (Equity Investments, Funds, Note Receivable, CLOSED INVESTMENTS),
    a header row naming the columns above each block. Returns
    {"records": [...], "period": "2026-Q2"} or None."""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    header_idx = [i for i, r in enumerate(rows)
                  if any(_key(c) == "investment balance" for c in r if isinstance(c, str))
                  and any(_key(c).startswith("gross investment") for c in r if isinstance(c, str))]
    if not header_idx:
        return None
    period = ""
    for r in rows[:4]:
        for c in r:
            if hasattr(c, "isoformat"):
                from .base import quarter_label
                period = quarter_label(c.date() if hasattr(c, "date") else c)
    cols: dict[int, str] = {}
    name_col = 1
    section, closed, notes = "", False, False
    records: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        texts = [(j, c) for j, c in enumerate(r) if isinstance(c, str) and c.strip()]
        if i in header_idx:
            cols = {}
            for j, c in texts:
                k = _key(c)
                if k == "investment":
                    name_col = j
                for pat, field in _SUMMARY_COLS.items():
                    if k == pat or (pat == "ownership" and k.startswith("ownership")):
                        cols[j] = field
                        break
            continue
        if not cols:
            continue
        nums = [c for c in r if isinstance(c, (int, float)) and not isinstance(c, bool)]
        if len(texts) == 1 and not nums:                       # a section heading
            title = texts[0][1].strip()
            if _SECTION_CLOSED.match(title):
                closed = True
            section = title
            notes = bool(_SECTION_NOTES.search(title))
            continue
        name = _text(r[name_col]) if name_col < len(r) else ""
        if not name or re.match(r"total\b|check\b", name, re.I):
            continue
        if _SECTION_SKIP.match(section) or (closed and notes):
            continue          # "Other", and closed notes: converted notes of companies listed above
        classified = classify_document(name)
        entity = classified["entity_name"] or re.sub(r"\s*\(.*\)\s*$", "", name).strip()
        if entity.lower() in seen:
            continue          # the first row for an entity is its current position
        is_fund = classified["entity_kind"] == "fund" or bool(_SECTION_FUNDS.match(section))
        got: dict[str, Any] = {}
        for j, field in cols.items():
            v = r[j] if j < len(r) else None
            if v in (None, ""):
                continue
            got[field] = v
        values: dict[str, str] = {}
        if is_fund:
            pairs = (("balance", "investment_balance"), ("funding", "drawn_to_date"),
                     ("commitment", "commitment"), ("future_draw", "future_draw"))
        else:
            pairs = (("balance", "book_value"), ("funding", "total_invested"))
        for src, dst in pairs:
            n = _num(got.get(src))
            if n is not None:
                values[dst] = _amt(n)
        returned = _num(got.get("returned"))
        if is_fund and returned is not None:
            values["distribution_to_date"] = _amt(abs(returned))
        elif not is_fund and returned and closed and abs(returned) >= 1:
            values["exit_proceeds"] = _amt(abs(returned))
        impairment = _num(got.get("impairment"))
        if not is_fund and impairment and abs(impairment) >= 1:
            values["impairment"] = _amt(abs(impairment))
        else:
            impairment = None
        pct = _pct(got.get("ownership_pct"))
        if pct is not None:
            values["ownership_pct"] = pct
        from ..portfolio import parse_date
        d = got.get("initial_investment_date")
        iso = parse_date(_text(d)) if d not in (None, "") else None
        if iso:                                  # "Pre 2014" is not a date: left out
            values["initial_investment_date"] = iso
            if not is_fund:
                values["last_investment_date"] = iso
        if not is_fund:
            values["holding_status"] = ("Exit" if closed else "Impaired" if impairment else "Current")
            if closed:
                values.setdefault("operating_status", "Inactive")
        if not values:
            continue
        commitment = values.pop("commitment", None)
        rec = {"entity": entity, "kind": "fund" if is_fund else "direct_investment",
               "values": values, "commitment": commitment, "calls": [], "sheet": ws.title}
        seen[entity.lower()] = rec
        records.append(rec)
    return {"records": records, "period": period} if records else None


_TAB_LABELS = {
    "entity type": "entity_type", "method of accounting": "accounting_method",
    "gl account": "gl_account", "company code": "company_code",
    "initial investment date": "initial_investment_date", "terms": "terms",
    "total commitment": "commitment", "remaining commitment": "future_draw",
    "investment balance": "investment_balance",
}
_OWNERSHIP_DATED = re.compile(r"ownership\s*(?:-\s*)?(.*)$", re.I)


def _accounting_tabs(ws) -> list[dict[str, Any]]:
    """The backup tab(s) behind the summary: a block per entity — its name,
    then label | value rows (Entity Type … Terms, Total Commitment), a
    "Funding | Amount | Investment Date" table (funds, notes) or an
    "Investment | … | Cash Outlay | … | Investment Date" table of securities
    (companies). Two entities may share one tab."""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    nonempty = [i for i, r in enumerate(rows) if any(c not in (None, "") for c in r)]
    starts = []
    for pos, i in enumerate(nonempty[:-1]):
        r, nxt = rows[i], rows[nonempty[pos + 1]]
        if (isinstance(r[0], str) and r[0].strip() and all(c in (None, "") for c in r[1:])
                and isinstance(nxt[0], str) and _key(nxt[0]) in ("entity type", "method of accounting")):
            starts.append(i)
    out = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(rows)
        block = rows[start:end]
        title = block[0][0].strip()
        if len(title) < 3 and len(starts) == 1:
            title = ws.title.strip()           # a stray mark in A1: the tab is named after its entity
        classified = classify_document(title)
        entity = classified["entity_name"] or title
        values: dict[str, str] = {}
        own: list[tuple[str, str]] = []          # (date text, value) for "% Ownership - 12/31/2025"
        calls: list[dict[str, Any]] = []
        securities: list[tuple[str, str]] = []  # (date, security) for the latest one held
        table: Optional[dict[str, int]] = None
        for r in block[1:]:
            a = r[0]
            if isinstance(a, str) and a.strip():
                k = _key(a)
                m = _OWNERSHIP_DATED.match(k)
                if k.startswith("ownership") and len(r) > 1:
                    pct = _pct(r[1])
                    if pct is not None:
                        own.append((m.group(1) if m else "", pct))
                    continue
                if k in _TAB_LABELS and len(r) > 1 and r[1] not in (None, ""):
                    field = _TAB_LABELS[k]
                    v = r[1]
                    if field in ("commitment", "future_draw", "investment_balance"):
                        num = _num(v)
                        if num is None:
                            continue
                        v = _amt(num)
                    else:
                        v = _text(v)
                        if _text(v).lower() in ("n/a", "na"):
                            continue
                    values.setdefault(field, v)
                    continue
                if k == "funding" and any(_key(c) == "investment date" for c in r if isinstance(c, str)):
                    table = {"amount": 1, "date": next(j for j, c in enumerate(r)
                                                       if isinstance(c, str) and _key(c) == "investment date")}
                    continue
                if k == "investment" and any(_key(c) == "investment date" for c in r if isinstance(c, str)):
                    heads = {_key(c): j for j, c in enumerate(r) if isinstance(c, str)}
                    outlay = next((j for h, j in heads.items() if h.startswith("cash outlay")), None)
                    if outlay is not None:
                        table = {"amount": outlay, "date": heads["investment date"], "securities": True}
                    continue
                if re.match(r"total\b", k):
                    table = None
                    continue
            if table and isinstance(a, str) and a.strip():
                amount = _num(r[table["amount"]] if table["amount"] < len(r) else None)
                d = _text(r[table["date"]]) if table["date"] < len(r) else ""
                if amount is None or amount <= 0 or not d:
                    continue
                # a sale, acquisition, mark-to-market or repayment line is not money put in
                if table.get("securities") and (
                        re.search(r"\bsale\b|sold|proceeds|acquisition|mtm|mark|repay|conver|"
                                  r"distribut|write", a, re.I)
                        or (_num(r[1]) is not None and _num(r[1]) < 0)):
                    continue
                amount = Decimal(_amt(amount))
                desc = re.sub(rf"^{re.escape(entity)}\s*", "", a.strip(), flags=re.I) or a.strip()
                desc = re.sub(rf"^{re.escape(title)}\s*", "", desc, flags=re.I) or desc
                calls.append({"date": d, "description": desc, "amount": str(amount),
                              "total_raise": None, "pre_money": None})
                if table.get("securities"):
                    securities.append((d, desc))
        if own:
            dated = [(t, v) for t, v in own if re.search(r"\d", t)]
            values["ownership_pct"] = (max(dated, key=lambda tv: _text(tv[0]))[1] if dated
                                       else next((v for t, v in own if not t), own[-1][1]))
        if securities and "security" not in values:
            values["security"] = max(securities)[1]
        is_fund = classified["entity_kind"] == "fund" or (
            classified["entity_kind"] == "unknown" and "commitment" in values)
        if not is_fund:
            values.pop("investment_balance", None)      # a company's balance is its book value
        commitment = values.pop("commitment", None)
        out.append({"entity": entity, "kind": "fund" if is_fund else "direct_investment",
                    "values": values, "commitment": commitment, "calls": calls, "sheet": ws.title})
    return out


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
    period = ""
    tab_records: list[dict[str, Any]] = []
    for ws in wb.worksheets:
        rec = _entity_sheet(ws)
        if rec:
            records.append(rec)
            sheets.append(ws.title)
            continue
        table = _summary_table(ws)
        if table:
            records += table["records"]
            period = period or table["period"]
            sheets.append(ws.title)
            continue
        tabs = _accounting_tabs(ws)
        if tabs:
            tab_records += tabs
            sheets.append(ws.title)
            continue
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if any(_header_kind(r) for r in rows if any(c not in (None, "") for c in r)):
            summary_rows += [r for r in rows if any(c not in (None, "") for c in r)]
            sheets.append(ws.title)
        else:
            skipped.append(ws.title)
    if not records and not summary_rows and not tab_records:
        return None
    by_entity = {r["entity"].lower(): r for r in records}
    has_summary = any(r["sheet"] != "summary" and not r.get("calls") and r["kind"]
                      for r in records) and bool(period)
    for t in tab_records:                 # the summary row is the position; the tab adds detail
        rec = by_entity.get(t["entity"].lower())
        if rec is None:
            if has_summary:
                # a backup tab with no row in the summary (a converted note, a closed
                # position the summary leaves out) is detail without a position: skipped
                skipped.append(f"{t['sheet']} ({t['entity']}: not in the Investments Summary)")
                continue
            records.append(t)
            by_entity[t["entity"].lower()] = t
            continue
        for k, v in t["values"].items():
            rec["values"].setdefault(k, v)
        rec["commitment"] = rec["commitment"] or t["commitment"]
        rec["calls"] = rec["calls"] or t["calls"]
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
    return {"records": records, "sheets": sheets, "skipped": skipped, "period": period}


def apply_master(reading: dict[str, Any], user: str = "local", source: str = "") -> dict[str, Any]:
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
        # the workbook's quarter, and which fields it set: a statement for a
        # later quarter supersedes those fields (see portfolio.workbook_dated)
        prior = db.get_details(entity)
        stamped = {**values, "kind": rec["kind"]}
        if reading.get("period"):
            stamped["workbook_as_of"] = reading["period"]
            stamped["workbook_fields"] = sorted(set(prior.get("workbook_fields") or [])
                                                | {k for k, v in values.items() if v is not None})
        db.set_details(entity, stamped, user=user)
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
                                source=source or "master workbook",
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
