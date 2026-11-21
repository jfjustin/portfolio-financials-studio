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
            "values": values, "commitment": commitment, "calls": calls, "sheet": ws.title,
            "doc": "portfolio"}


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
    "impairment date": "impairment_date", "disposition date": "disposition_date",
    "note conversion repayment": "converted",
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
        if _SECTION_SKIP.match(section):
            continue          # "Other"
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
        if closed and notes and abs(_num(got.get("converted")) or 0) >= 1:
            continue          # a closed note converted into (or repaid by) a position listed above
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
            gone = got.get("disposition_date")
            gone = parse_date(_text(gone)) if gone not in (None, "") else None
            # a closed position sold or otherwise disposed of is an exit; one written
            # off with no disposition stays an impaired investment
            values["holding_status"] = (("Exit" if gone or not impairment else "Impaired") if closed
                                        else "Impaired" if impairment else "Current")
            if closed and gone:
                values["exit_date"] = gone
            hit = got.get("impairment_date")
            hit = parse_date(_text(hit)) if hit not in (None, "") and impairment else None
            if hit:
                values["impairment_date"] = hit
            if closed:
                values.setdefault("operating_status", "Inactive")
        if not values:
            continue
        if name != entity and not re.fullmatch(r"\(\s*(?:pre\s*)?\d{4}\s*\)", name[len(entity):].strip(), re.I):
            values["display_name"] = name     # e.g. "Company Y (Company X Acq.)": the note says what it is
        commitment = values.pop("commitment", None)
        rec = {"entity": entity, "kind": "fund" if is_fund else "direct_investment",
               "values": values, "commitment": commitment, "calls": [], "sheet": ws.title,
               "doc": "equity"}
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


_CCY = re.compile(r"^(?:amount\s*\(\s*)?([a-z]{3})\s*\)?$", re.I)


def _ccy_header(r: list[Any]) -> Optional[dict[str, Any]]:
    """A "<label> | EUR | FX Rate | USD | (Investment Date)" header: the native
    currency, and which columns hold the native amount, the USD amount and the
    date. The rate is never read from the FX column (EUR is quoted as USD per
    euro, TWD as TWD per dollar); it is always USD ÷ native, row by row."""
    texts = {j: str(c).strip() for j, c in enumerate(r) if isinstance(c, str) and c.strip()}
    fx = next((j for j, t in texts.items() if _key(t) == "fx rate"), None)
    if fx is None:
        return None
    native = usd = None
    for j, t in texts.items():
        m = _CCY.match(t)
        code = m.group(1).upper() if m else ""
        if code == "USD" and j > fx:
            usd = j
        elif code and code != "USD" and j < fx:
            native = (j, code)
    if native is None or usd is None:
        return None
    date_col = next((j for j, t in texts.items() if _key(t) == "investment date"), None)
    return {"native": native[0], "ccy": native[1], "usd": usd, "date": date_col,
            "label": _key(r[0]) if isinstance(r[0], str) else ""}


def _accounting_tabs(ws) -> list[dict[str, Any]]:
    """The backup tab(s) behind the summary: a block per entity — its name,
    then label | value rows (Entity Type … Terms, Total Commitment), a
    "Funding | Amount | Investment Date" table (funds, notes) or an
    "Investment | … | Cash Outlay | … | Investment Date" table of securities
    (companies). Two entities may share one tab.

    A non-USD entity's tab prints "EUR | FX Rate | USD" columns: the native
    amounts are kept, each transaction's own rate (USD ÷ native) is recorded,
    and the quarterly "Equity Income" rows give a rate for each quarter end.
    Dated events (calls, value changes, income, distributions) are kept for
    the entity's history."""
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
        native: dict[str, str] = {}              # figures in the entity's own currency (non-USD tabs)
        own: list[tuple[str, str]] = []          # (date text, value) for "% Ownership - 12/31/2025"
        calls: list[dict[str, Any]] = []
        securities: list[tuple[str, str]] = []  # (date, security) for the latest one held
        events: list[dict[str, Any]] = []       # dated history: call / value / income / distribution
        rates: dict[str, str] = {}              # quarter -> USD per unit of the entity's currency
        table: Optional[dict[str, Any]] = None
        ccy: Optional[str] = None               # the entity's currency, once a tab table names it
        hdr: Optional[dict[str, Any]] = None    # the current EUR | FX Rate | USD header
        section: list[dict[str, Any]] = []      # the calls of the latest Funding table (a note)

        def rate_from(row: list[Any], h: dict[str, Any]) -> Optional[Decimal]:
            a_, u_ = _num(row[h["native"]]) if h["native"] < len(row) else None, \
                _num(row[h["usd"]]) if h["usd"] < len(row) else None
            return (u_ / a_) if a_ and u_ and a_ != 0 and u_ / a_ > 0 else None

        for r in block[1:]:
            a = r[0]
            h = _ccy_header(r)
            if h:
                ccy, hdr = h["ccy"], h
                table = None
                if h["label"] == "funding" and h["date"] is not None:
                    table = {"amount": h["native"], "date": h["date"], "usd": h["usd"]}
                    section = []
                elif h["label"].startswith("equity income"):
                    table = {"income": True, "amount": h["native"], "usd": h["usd"]}
                elif "distribution" in h["label"]:
                    table = {"distributions": True, "amount": h["native"], "usd": h["usd"]}
                continue
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
                        usd_cell = _num(r[hdr["usd"]]) if hdr and len(r) > hdr["usd"] else None
                        if hdr and (field != "investment_balance" or usd_cell is not None):
                            native[field] = v            # under a EUR | FX | USD header: native
                            q = rate_from(r, hdr)
                            if q and field == "commitment":
                                rates.setdefault("_commitment", _amt(q))
                            continue
                    else:
                        v = _text(v)
                        if _text(v).lower() in ("n/a", "na"):
                            continue
                    if field in ("accounting_method", "entity_type", "gl_account", "company_code"):
                        values[field] = v      # a note's section, then the shares it became: the last is current
                    else:
                        values.setdefault(field, v)
                    continue
                if k.startswith("equity income") and not h:
                    table = {"income": True, "amount": 1, "usd": None}
                    continue
                if k == "distributions":
                    heads = {_key(c): j for j, c in enumerate(r) if isinstance(c, str)}
                    table = {"distributions": True, "amount": 1}
                    continue
                if k == "funding" and any(_key(c) == "investment date" for c in r if isinstance(c, str)):
                    table = {"amount": 1, "date": next(j for j, c in enumerate(r)
                                                       if isinstance(c, str) and _key(c) == "investment date")}
                    section = []
                    continue
                if k.startswith("repayment") and section and len(r) > 1 and (_num(r[1]) or 0) < 0:
                    # the note above was repaid in cash: a loan out and back, not an investment
                    for c in section:
                        c["repaid"] = True
                    m_ = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", a)
                    when_ = from_text_date(m_.group(0)) if m_ else None
                    if when_:
                        events.append({"date": when_, "kind": "distribution", "amount": _amt(abs(_num(r[1])))})
                    table = None
                    continue
                if k == "investment" and any(_key(c) == "investment date" for c in r if isinstance(c, str)):
                    heads = {_key(c): j for j, c in enumerate(r) if isinstance(c, str)}
                    outlay = next((j for h_, j in heads.items() if h_.startswith("cash outlay")), None)
                    basis = "cash"
                    if outlay is None:
                        # no Cash Outlay column: a purchase row's own value is the money put
                        # in — the USD column where a local-currency one sits beside it
                        outlay = next((j for h_, j in heads.items() if h_.endswith(" usd")), None)
                        if outlay is None:
                            outlay = next((j for h_, j in heads.items() if h_ in (
                                "investment value", "amount", "total investment", "cost")), None)
                        basis = "value"
                    if outlay is not None:
                        table = {"amount": outlay, "date": heads["investment date"], "securities": True,
                                 "basis": basis, "terms": heads.get("terms"), "bought": [],
                                 "conversion": [j for h_, j in heads.items()
                                                if re.search(r"(gain|loss) on conversion", h_)]}
                    continue
                if re.match(r"total\b", k):
                    if table and table.get("usd") is not None and k == "total funding":
                        num = _num(r[1]) if len(r) > 1 else None
                        if num is not None:
                            native["drawn_to_date"] = _amt(num)
                    if k.startswith("total accrued interest") and section and len(r) > 1:
                        num = _num(r[1])
                        if num:
                            section[-1]["interest"] = _amt(num)   # the note's balance at conversion adds it
                    table = None
                    continue
            # a dated row of an equity-income or distributions table
            if table and (table.get("income") or table.get("distributions")):
                d = a.date().isoformat() if hasattr(a, "date") else (from_text_date(a) if isinstance(a, str) else None)
                amount = _num(r[table["amount"]]) if table["amount"] < len(r) else None
                if d and amount is not None:
                    ev = {"date": d, "kind": "income" if table.get("income") else "distribution",
                          "amount": _amt(amount)}
                    if table.get("usd") is not None and table["usd"] < len(r) and _num(r[table["usd"]]) is not None:
                        ev["amount_usd"] = _amt(_num(r[table["usd"]]))   # as the accounting team converted it
                    events.append(ev)
                    if table.get("usd") is not None and hdr:
                        q = rate_from(r, hdr)
                        if q:
                            rates[_quarter(d)] = str(q.quantize(Decimal("0.000001")))
                continue
            if table and isinstance(a, str) and a.strip():
                amount = _num(r[table["amount"]] if table["amount"] < len(r) else None)
                d = _text(r[table["date"]]) if table["date"] < len(r) else ""
                # the same purchase listed again (e.g. restated after anti-dilution): no new
                # money and no new value
                restated = bool(table.get("basis") == "value" and amount and amount > 0 and any(
                    bd == d and abs(ba - amount) <= ba * Decimal("0.01") for bd, ba in table["bought"]))
                if table.get("securities") and d and not restated:
                    change = _num(r[1]) if len(r) > 1 else None
                    if change is not None:
                        events.append({"date": d, "kind": "value", "amount": _amt(change)})
                if amount is None or amount <= 0 or not (d or table.get("securities")) or restated:
                    continue
                converts = False
                if table.get("securities"):
                    terms = (_text(r[table["terms"]])
                             if table.get("terms") is not None and table["terms"] < len(r) else "")
                    # not money put in: a sale, acquisition, mark-to-market, repayment,
                    # write-down or rounding line
                    if (re.search(r"\bsale\b|sold|proceeds|acquisition|mtm|mark|repay|conver|"
                                  r"distribut|write|impair|rounding|anti.?dilution", a, re.I)
                            or (_num(r[1]) is not None and _num(r[1]) < 0)):
                        continue
                    table["bought"].append((d, amount))
                    # a note converting into shares: its cash went in as the note — left
                    # out once that note's funding is on file (see _settle_calls)
                    converts = bool(re.search(r"note conversion|conversion of (?:the )?note|converted from",
                                              terms, re.I)
                                    or any((_num(r[j]) or 0) != 0 for j in table.get("conversion", [])
                                           if j < len(r)))
                amount = Decimal(_amt(amount))
                desc = re.sub(rf"^{re.escape(entity)}\s*", "", a.strip(), flags=re.I) or a.strip()
                desc = re.sub(rf"^{re.escape(title)}\s*", "", desc, flags=re.I) or desc
                desc = desc.lstrip(" ,;:-–") or desc
                call = {"date": d, "description": desc, "amount": str(amount),
                        "total_raise": None, "pre_money": None, "currency": ccy if table.get("usd") is not None else None,
                        "amount_usd": None}
                if not d:
                    call["undated"] = True        # a purchase row with no date: dated from the summary
                if converts:
                    call["conversion"] = True
                elif not table.get("securities"):
                    call["note"] = True           # a Funding table's row: the money itself
                    section.append(call)
                if table.get("usd") is not None:
                    usd = _num(r[table["usd"]]) if table["usd"] < len(r) else None
                    if usd is not None:
                        call["amount_usd"] = _amt(abs(usd))
                        q = (abs(usd) / amount) if amount else None
                        if q and d:
                            rates.setdefault(_quarter(d), str(q.quantize(Decimal("0.000001"))))
                calls.append(call)
                if d:
                    events.append({"date": d, "kind": "call", "amount": str(amount)})
                    if table.get("securities"):
                        securities.append((d, desc))
        if own:
            dated = [(t, v) for t, v in own if re.search(r"\d", t)]
            values["ownership_pct"] = (max(dated, key=lambda tv: _text(tv[0]))[1] if dated
                                       else next((v for t, v in own if not t), own[-1][1]))
        if securities and "security" not in values:
            values["security"] = max(securities)[1]
        is_fund = classified["entity_kind"] == "fund" or (
            classified["entity_kind"] == "unknown" and ("commitment" in values or "commitment" in native))
        if ccy and ccy != "USD" and "distribution_to_date" not in native:
            paid_out = [abs(Decimal(str(e["amount"]))) for e in events if e["kind"] == "distribution"]
            if paid_out:                                # the distributions table, in its own currency
                native["distribution_to_date"] = _amt(sum(paid_out, Decimal(0)))
        if not is_fund:
            values.pop("investment_balance", None)      # a company's balance is its book value
            if native.get("investment_balance") is not None:
                native["book_value"] = native.pop("investment_balance")   # in its own currency
            if "drawn_to_date" in native:               # a company's funding is its cost
                native["total_invested"] = native.pop("drawn_to_date")
        commitment = native.pop("commitment", None) if ccy else values.pop("commitment", None)
        values.pop("commitment", None)
        rates.pop("_commitment", None)
        out.append({"entity": entity, "kind": "fund" if is_fund else "direct_investment",
                    "values": values, "native": native, "currency": ccy, "rates": rates,
                    "commitment": commitment, "calls": calls, "events": events, "sheet": ws.title,
                    "doc": "equity"})
    return out


def _settle_calls(calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Decimal]:
    """An entity's tab calls as money put in, and the interest its notes accrued.
    A note repaid in cash is no investment. A conversion row whose note is funded
    on file (at its principal, or principal plus interest) is that note's money
    again and is left out; one whose note is not on file stays, dated at the
    conversion — the nearest date on file for that money."""
    def money(c: dict[str, Any]) -> Decimal:
        return Decimal(c.get("amount_usd") or c["amount"])
    notes = [c for c in calls if c.get("note") and not c.get("repaid")]
    out = []
    for c in calls:
        if c.get("repaid"):
            continue
        if c.get("conversion") and any(
                abs(money(n) + Decimal(n.get("interest") or 0) * k - money(c)) <= money(c) * Decimal("0.01")
                for n in notes for k in (0, 1)):
            continue
        out.append(c)
    return out, sum((Decimal(n.get("interest") or 0) for n in notes), Decimal(0))


def _quarter(d: str) -> str:
    from .base import quarter_label
    return quarter_label(date.fromisoformat(d[:10]))


def from_text_date(t: str) -> Optional[str]:
    from ..portfolio import parse_date
    v = parse_date(t)
    return v if v and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None


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
    pending: dict[str, dict[str, Any]] = {}     # entity -> the calls of all its tabs
    for t in tab_records:                 # the summary row is the position; the tab adds detail
        rec = by_entity.get(t["entity"].lower())
        if rec is None:
            if has_summary:
                # a backup tab with no row in the summary (a converted note, a closed
                # position the summary leaves out) is detail without a position: skipped
                skipped.append(f"{t['sheet']} ({t['entity']}: not in the Investments Summary)")
                continue
            t["calls"] = _settle_calls(t["calls"])[0]
            t["commitment_ccy"] = t.get("currency") if t.get("commitment") else None
            records.append(t)
            by_entity[t["entity"].lower()] = t
            continue
        for k, v in t["values"].items():
            rec["values"].setdefault(k, v)
        for k in ("native", "currency", "rates"):
            if t.get(k):
                rec[k] = t[k]
        if t.get("commitment") and t.get("currency") not in (None, "USD"):
            rec.setdefault("native", {})["commitment"] = t["commitment"]   # its own-currency figure
        if t.get("events"):
            rec["events"] = (rec.get("events") or []) + t["events"]   # e.g. a note tab and a shares tab
        if not rec["commitment"] and t.get("commitment"):
            rec["commitment"] = t["commitment"]            # the summary gives none: the tab's
            rec["commitment_ccy"] = t.get("currency")
        if not rec["calls"] and t["calls"]:
            p = pending.setdefault(t["entity"].lower(), {"calls": [], "sheets": []})
            p["calls"] += t["calls"]
            p["sheets"].append(t["sheet"])
    for key, p in pending.items():
        # the tabs' dated calls must add up to what the summary says was put in (in
        # USD, the summary's currency) — with or without the interest its notes
        # accrued, which a converted note's cost includes; a tab listing the whole
        # fund's calls, or in other units, is named and left out
        rec = by_entity[key]
        calls, interest = _settle_calls(p["calls"])
        paid = rec["values"].get("drawn_to_date") if rec["kind"] == "fund" else rec["values"].get("total_invested")
        total = sum((Decimal(c["amount_usd"]) if c.get("amount_usd") else Decimal(c["amount"]) for c in calls),
                    Decimal(0))
        if paid and Decimal(paid) > 0:
            due = Decimal(paid)
            if (abs(total - due) > max(Decimal(1), due * Decimal("0.05"))
                    and abs(total + interest - due) > max(Decimal(1), due * Decimal("0.005"))):
                skipped.append(f"{', '.join(p['sheets'])} ({rec['entity']}: its {len(calls)} dated calls add up to "
                               f"{total:,.0f}, but the summary's funding is {due:,.0f} — calls not imported)")
                continue
        when = rec["values"].get("initial_investment_date")
        for c in calls:
            if c.get("undated") and when:
                c["date"] = when                  # the tab's purchase row has no date of its own
        rec["calls"] = calls
        rec["note_interest"] = str(interest) if interest else None   # in the funding, not cash
    if summary_rows:
        reading = read_holdings(summary_rows)
        for h in reading["records"]:
            rec = by_entity.get(h["name"].lower())
            if rec is None:
                rec = {"entity": h["name"], "kind": h["kind"], "values": {}, "commitment": None,
                       "calls": [], "sheet": "summary", "doc": "portfolio"}
                records.append(rec)
                by_entity[h["name"].lower()] = rec
            for k, v in h["body"].items():          # the entity's own sheet wins
                rec["values"].setdefault(k, v)
            if rec["commitment"] is None and h.get("commitment"):
                rec["commitment"] = h["commitment"]
        skipped += [f"row {e['row']} ({e['company']}): {e['message']}" for e in reading["errors"]]
    return {"records": records, "sheets": sheets, "skipped": skipped, "period": period}


_MONEY_FIELDS = None
_DOC_RANK = {"portfolio": 1, "equity": 2}      # the accounting workbook is the primary source
# Bump whenever reading or applying a workbook changes what is stored: a workbook
# on file that an older version read is read again at startup
# (pipeline.refresh_master_workbooks), so a fix reaches the data already uploaded.
READER_VERSION = "2026-11-20"


def _money_fields() -> set[str]:
    global _MONEY_FIELDS
    if _MONEY_FIELDS is None:
        from ..portfolio import COMPANY_FIELDS, FUND_FIELDS
        _MONEY_FIELDS = {k for k, _l, t in FUND_FIELDS + COMPANY_FIELDS if t == "money"}
    return _MONEY_FIELDS


def apply_master(reading: dict[str, Any], user: str = "local", source: str = "") -> dict[str, Any]:
    """Write every record: details (merged; empty cells leave what is on file),
    the fund's commitment, rates, history and dated investments.

    Every money figure carries its currency (`field_ccy`): the summaries are in
    USD, a non-USD entity's tab in its own currency; reports convert with the
    entity's USD rates. The accounting workbook (Equity Investments Summary) is
    the primary source: an Investment Portfolio workbook never overwrites a
    figure, a rate or a dated investment that the accounting workbook set.
    Its dated investments are the record up to its quarter (`equity_as_of`):
    reading it again removes the rows an accounting workbook saved that it no
    longer lists, and the report leaves out any other row dated by then
    (portfolio._invested). Rows typed in by hand are never removed."""
    from .. import terms
    from ..portfolio import _order, coerce_details, parse_date, same_investment, usd_rate_at
    existing = {e.lower(): e for e in db.all_details()}
    for f in db.get_facts():
        existing.setdefault(str(f["entity_name"]).lower(), f["entity_name"])
    done, errors, n_calls, n_replaced, n_removed = [], [], 0, 0, 0
    for rec in reading["records"]:
        entity = existing.get(rec["entity"].lower(), rec["entity"])
        doc = rec.get("doc") or "portfolio"
        prior = db.get_details(entity)
        field_src = dict(prior.get("field_src") or {})
        field_ccy = dict(prior.get("field_ccy") or {})
        ccy = rec.get("currency") or prior.get("currency")
        body = dict(rec["values"])
        # the summary's figures are in USD, the reporting currency; a figure only
        # the entity's tab gives is kept in the tab's currency (the report converts it)
        natives = {k: v for k, v in (rec.get("native") or {}).items()     # display-only originals left out
                   if k not in ("commitment", "distribution_to_date", "book_value") and body.get(k) in (None, "")}
        body.update(natives)
        # an Investment Portfolio workbook leaves alone what the accounting workbook set
        for k in list(body):
            if _DOC_RANK.get(field_src.get(k), 0) > _DOC_RANK[doc]:
                body.pop(k)
        try:
            values = coerce_details(body)
        except ValueError as e:
            errors.append(f"{entity}: {e}")
            continue
        for k, v in values.items():
            if v is None:
                continue
            field_src[k] = doc
            if k in _money_fields():
                field_ccy[k] = (ccy if k in natives and ccy else "USD")
        # the workbook's quarter, and which fields it set: a statement for a
        # later quarter supersedes those fields (see portfolio.workbook_dated)
        stamped: dict[str, Any] = {**values, "kind": rec["kind"], "field_src": field_src,
                                   "field_ccy": field_ccy}
        if ccy and ccy != "USD":
            stamped["currency"] = ccy
        if rec.get("rates") and (doc == "equity" or not prior.get("usd_rates_src") == "equity"):
            from ..portfolio import _rates
            merged = {**_rates(prior.get("usd_rates")), **{q: Decimal(v) for q, v in rec["rates"].items()}}
            stamped["usd_rates"] = "; ".join(f"{q}={v}" for q, v in sorted(merged.items()))
            stamped["usd_rates_src"] = doc
        if rec.get("events") and doc == "equity":
            stamped["history"] = rec["events"]
        if doc == "equity":
            stamped["note_interest"] = rec.get("note_interest")   # what the funding adds to the cash
            # the tab's own-currency figures (Total Funding in EUR, ...): the originals
            stamped["native_values"] = {k: str(v) for k, v in (rec.get("native") or {}).items()
                                        if v not in (None, "")}
        if reading.get("period"):
            stamped["workbook_as_of"] = reading["period"]
            stamped["workbook_fields"] = sorted(set(prior.get("workbook_fields") or [])
                                                | {k for k, v in values.items() if v is not None})
        # the accounting workbook's quarter: its dated investments are the record up
        # to then (an older accounting workbook read later doesn't move it back)
        newest = True
        if doc == "equity" and reading.get("period"):
            newest = _order(reading["period"]) >= _order(prior.get("equity_as_of") or "")
            if newest:
                stamped["equity_as_of"] = reading["period"]
        db.set_details(entity, stamped, user=user)
        det = db.get_details(entity)
        if rec.get("commitment") and not (doc == "portfolio" and field_src.get("commitment") == "equity"):
            prof = terms.fund_profile(entity)
            terms.set_fund_profile(entity, investor_aliases=prof.get("investor_aliases") or "",
                                   notes=prof.get("notes") or "", commitment=str(rec["commitment"]),
                                   user=user)
            fs = dict(det.get("field_src") or {})
            fc = dict(det.get("field_ccy") or {})
            fs["commitment"], fc["commitment"] = doc, (rec.get("commitment_ccy") or "USD")
            db.set_details(entity, {"field_src": fs, "field_ccy": fc}, user=user)
            det = db.get_details(entity)
        # dated investments: one investment is one row, whichever workbook lists it
        on_file = db.get_capital_calls(entity)
        listed: set[int] = set()                  # the rows this reading lists

        def usd_of(amount: Decimal, cur: Optional[str], amount_usd: Optional[str], d: str) -> Optional[Decimal]:
            if amount_usd:
                return Decimal(amount_usd)
            if (cur or det.get("currency") or "USD") == "USD":
                return amount
            r = usd_rate_at(det, d)
            return amount * r if r else None

        for c in rec.get("calls", []):
            d = parse_date(c["date"]) if c.get("date") else None
            if d is None and not c.get("undated"):
                continue
            d = d or ""                       # undated: the report dates it from the details on file
            amount = Decimal(c["amount"])
            cur = c.get("currency") or ("USD" if doc == "portfolio" else None)
            new_usd = usd_of(amount, cur, c.get("amount_usd"), d) if d else (
                amount if (cur or det.get("currency") or "USD") == "USD" else None)
            src_name = source or "master workbook"
            same = next((x for x in on_file if x["call_date"] == d and Decimal(x["amount"]) == amount
                         and (x.get("source") or "") in (src_name, "")), None)
            if same is not None:                              # this workbook's row, already on file (a re-read)
                listed.add(same["id"])
                # an older reader saved it without its currency or USD value: the
                # accounting workbook's are the record
                fixed: dict[str, Any] = {}
                if doc == "equity" and cur and cur != "USD" and same.get("currency") != cur:
                    fixed["currency"] = cur
                if doc == "equity" and c.get("amount_usd") and (
                        same.get("amount_usd") is None or Decimal(str(same["amount_usd"])) != Decimal(c["amount_usd"])):
                    fixed["amount_usd"] = str(Decimal(c["amount_usd"]))
                if (fixed or not same.get("source")) and same.get("id"):
                    # name its file (rows saved before sources were recorded have none)
                    db.stamp_capital_call(same["id"], same.get("source") or src_name,
                                          same.get("doc_kind") or doc, user=user, **fixed)
                    same.update({"source": same.get("source") or src_name,
                                 "doc_kind": same.get("doc_kind") or doc, **fixed})
                continue
            twin = None if not d else next((x for x in on_file if x["call_date"] and (x.get("source") or "") != src_name
                                            and same_investment(
                x["call_date"], usd_of(Decimal(x["amount"]), x.get("currency"), x.get("amount_usd"), x["call_date"]),
                d, new_usd)), None)
            if twin is not None:
                if _DOC_RANK.get(twin.get("doc_kind") or "", 0) >= _DOC_RANK[doc] or doc != "equity":
                    listed.add(twin["id"])
                    continue                                  # already on file from as good a source
                db.delete_capital_call(twin["id"], user=user)  # the accounting workbook's row replaces it
                on_file = [x for x in on_file if x["id"] != twin["id"]]
                n_replaced += 1
            cid = db.add_capital_call(entity, d, amount, c.get("description") or "",
                                      source=source or "master workbook",
                                      total_raise=Decimal(c["total_raise"]) if c.get("total_raise") else None,
                                      pre_money=Decimal(c["pre_money"]) if c.get("pre_money") else None,
                                      user=user, currency=cur, doc_kind=doc,
                                      amount_usd=Decimal(c["amount_usd"]) if c.get("amount_usd") else None)
            on_file.append({"id": cid, "call_date": d, "amount": str(amount), "currency": cur,
                            "amount_usd": c.get("amount_usd"), "doc_kind": doc,
                            "source": source or "master workbook"})
            listed.add(cid)
            n_calls += 1
        # the record up to its quarter: an accounting workbook's row this one no
        # longer lists (an older reading of it, an earlier workbook) goes
        if doc == "equity" and newest and rec.get("calls"):
            for x in on_file:
                if x.get("doc_kind") == "equity" and x["id"] not in listed:
                    db.delete_capital_call(x["id"], user=user,
                                           note=f"no longer listed in {source or 'the accounting workbook'}")
                    n_removed += 1
        done.append({"entity": entity, "kind": rec["kind"], "fields": sorted(values),
                     "sheet": rec.get("sheet", ""), "currency": det.get("currency") or "USD"})
    return {"updated": done, "errors": errors, "calls_added": n_calls, "calls_replaced": n_replaced,
            "calls_removed": n_removed}


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
