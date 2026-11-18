"""One fund's or company's history over time, for the History section.

    financials   a company's own quarterly figures (revenue, operating expense,
                 operating cash flow, CapEx, ending cash) and burn, approved
                 values only — the Direct Investment Financials readings
    roll-forward a fund's quarterly roll-forward (beginning, funding,
                 distributions, ending balance)
    investments  every dated investment, once, with the running total invested
    value        the carrying value over time: the accounting workbook's dated
                 value changes (purchases, mark-ups, sales) added up; failing
                 those, money put in plus income less distributions; a fund's
                 roll-forward balances; and today's value

Investment figures are in USD, like every other report (a figure on file in
another currency is converted at the entity's rate for its date); a company's
own financial statements stay in the currency it reports in.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from . import db, direct_investments, irr, portfolio
from .extractors.base import quarter_bounds


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


def history(entity: str, as_of: Optional[str] = None) -> dict[str, Any]:
    rep = portfolio.report(as_of)
    row = next((r for r in rep["funds"] + rep["companies"] if r["entity"] == entity), None)
    if row is None:
        raise KeyError(entity)
    ccy = row.get("currency") or "USD"
    det = row.get("details") or {}
    as_of_end = quarter_bounds(rep["as_of"])[1]
    tab_ccy = (det.get("currency") or "USD").upper()      # the accounting tab's currency

    def in_usd(e: dict) -> Optional[dict]:
        if tab_ccy == "USD":
            return e
        if e.get("amount_usd") is not None:                # as the accounting team converted it
            return {**e, "amount": e["amount_usd"]}
        r = portfolio.usd_rate_at(det, e["date"])
        return {**e, "amount": str(Decimal(str(e["amount"])) * r)} if r else None

    # ---- the company's own quarterly figures
    actuals, _pending, _conflicts = direct_investments._actuals()
    quarters = sorted((q for (c, q) in actuals if c == entity), key=portfolio._order)
    financials = []
    for q in quarters:
        if portfolio._order(q) > portfolio._order(rep["as_of"]):
            continue
        vals = {k: d["value"] for k, d in actuals[(entity, q)].items() if k in direct_investments.BASE}
        derived = direct_investments.derive(vals, 3)
        financials.append({"period": q, "end": quarter_bounds(q)[1].isoformat(),
                           "currency": next((d["currency"] for d in actuals[(entity, q)].values()), ccy),
                           **{k: _f(v) for k, v in vals.items()},
                           **{k: _f(v) if not isinstance(v, int) else v for k, v in derived.items()}})

    # ---- a fund's roll-forward
    roll = []
    own_q = sorted((q for q in db.get_investments()["quarters"] if q["entity_name"] == entity),
                   key=lambda q: q["period_order"])
    for q in portfolio.roll_forward_usd(own_q, det, (rep.get("statement_currency") or {}).get(entity, "USD")):
        if portfolio._order(q["period_label"]) > portfolio._order(rep["as_of"]):
            continue
        roll.append({"period": q["period_label"],
                     "end": quarter_bounds(q["period_label"])[1].isoformat()
                     if portfolio.re.fullmatch(r"20\d{2}-Q[1-4]", q["period_label"] or "") else None,
                     "beginning": q.get("beginning_balance"), "funding": q.get("funding"),
                     "distributions": abs(q.get("return_of_capital") or 0) + abs(q.get("dividends") or 0),
                     "ending": q.get("ending_balance")})

    # ---- dated investments, once each, with the running total
    investments, running = [], Decimal(0)
    for c in row.get("invested", []):
        running += Decimal(str(c["amount"]))
        investments.append({"date": c["date"], "description": c["description"], "amount": _f(c["amount"]),
                            "amount_usd": _f(c.get("amount_usd")), "cumulative": _f(running),
                            "document": c.get("document"), "also_in": c.get("also_in") or [],
                            "estimated": c["source"] != "entered"})

    # ---- carrying value over time
    events = [x for x in (in_usd(e) for e in sorted(det.get("history") or [], key=lambda e: e["date"])) if x]
    value_points: list[list[Any]] = []
    if any(e["kind"] == "value" for e in events):
        v = Decimal(0)
        for e in events:
            if e["kind"] == "value":
                v += Decimal(str(e["amount"]))
                value_points.append([e["date"], float(v), "accounting workbook: value change"])
        basis = "the accounting workbook's dated value changes (purchases, mark-ups, sales)"
    elif events:
        v = Decimal(0)
        for e in events:
            amt = Decimal(str(e["amount"]))
            v += -abs(amt) if e["kind"] == "distribution" else amt     # tabs book distributions negative
            value_points.append([e["date"], float(v), f"accounting workbook: {e['kind']}"])
        basis = "money put in, plus income, less distributions (the accounting workbook)"
    else:
        basis = ""
    for r_ in roll:
        if r_["end"] and r_["ending"] is not None:
            value_points.append([r_["end"], float(r_["ending"]), f"roll-forward {r_['period']} ending balance"])
            basis = basis or "the roll-forward's quarter-end balances"
    now = row.get("investment_balance") if row["kind"] == "fund" else (
        row.get("total_value") if row.get("total_value") is not None else portfolio._d(det.get("book_value")))
    if now is not None and not row.get("exited"):
        value_points.append([as_of_end.isoformat(), float(now), "value now"])
    value_points.sort(key=lambda p: p[0])

    I = irr.report(rep["as_of"], base=rep)
    e = next((x for x in I["entities"] if x["entity"] == entity), None)
    paid = row.get("drawn_to_date") if row["kind"] == "fund" else row.get("total_invested")
    facts = _facts(row, det, investments, paid, now)
    return {"entity": entity, "name": row["investment_name"], "kind": row["kind"], "currency": ccy,
            "facts": facts,
            "as_of": rep["as_of"], "as_of_label": rep["as_of_label"], "quarters": rep["quarters"],
            "theme": row.get("theme") or row.get("region_sector") or "",
            "status": "exited" if row.get("exited") else (row.get("operating_status") or ""),
            "paid_in": _f(paid), "value_now": _f(now), "irr_pct": e["irr_pct"] if e else None,
            "irr_estimated": bool(e and e["estimated"]),
            "financials": financials, "roll_forward": roll, "investments": investments,
            "value": value_points, "value_basis": basis,
            "by_year": _by_year(events), "native_currency": row.get("native_currency") or ccy,
            "usd_rate": _f(portfolio.usd_rate_at(det, rep["as_of"])) if ccy != "USD" else 1.0}


def _facts(row: dict, det: dict, investments: list[dict], paid: Any, now: Any) -> list[dict[str, Any]]:
    """The one-page summary's investment facts, in order: [{label, value, kind}],
    kind money | pct | date | multiple | text; a fact with no value is left out."""
    dates = sorted(i["date"] for i in investments)
    out: list[tuple[str, Any, str]]
    if row["kind"] == "fund":
        drawn, dist, nav = row.get("drawn_to_date"), row.get("distribution_to_date"), row.get("investment_balance")
        out = [("Vintage", row.get("vintage"), "text"), ("Region / sector", row.get("region_sector"), "text"),
               ("Commitment", row.get("commitment"), "money"), ("Drawn to date", drawn, "money"),
               ("Future draw", row.get("future_draw"), "money"),
               ("Distributed to date", dist, "money"), ("Investment balance (NAV)", nav, "money"),
               ("TVPI", (Decimal(str(nav or 0)) + Decimal(str(dist or 0))) / Decimal(str(drawn))
                if drawn else None, "multiple"),
               ("DPI", Decimal(str(dist or 0)) / Decimal(str(drawn)) if drawn else None, "multiple"),
               ("First call", dates[0] if dates else None, "date"),
               ("Ownership of the fund", det.get("ownership_pct"), "pct")]
    else:
        ex = row.get("exit") or {}
        gain = (Decimal(str(now)) - Decimal(str(paid))) if now is not None and paid is not None else None
        out = [("Theme", row.get("theme"), "text"),
               ("Status", det.get("holding_status") or ("Exit" if row.get("exited") else ""), "text"),
               ("Round / security", row.get("current_round") if row.get("current_round") not in (None, "-")
                else row.get("security"), "text"),
               ("First investment", dates[0] if dates else det.get("initial_investment_date"), "date"),
               ("Latest investment", dates[-1] if dates else det.get("last_investment_date"), "date"),
               ("Investments made", len(investments) or None, "text"),
               ("Total invested", paid, "money"), ("Ownership", det.get("ownership_pct"), "pct"),
               ("Book value", det.get("book_value"), "money"),
               ("Fair value", row.get("total_value") if not row.get("exited") else None, "money"),
               ("Unrealized gain / (loss)", gain if not row.get("exited") else None, "money"),
               ("Multiple (value ÷ invested)", Decimal(str(now)) / Decimal(str(paid))
                if now is not None and paid else None, "multiple"),
               ("Impairment to date", det.get("impairment"), "money"),
               ("Impaired", det.get("impairment_date"), "date"),
               ("Exit date", ex.get("date"), "date"), ("Exit proceeds", ex.get("proceeds"), "money"),
               ("Accounting method", det.get("accounting_method"), "text")]
    return [{"label": k, "value": float(v) if isinstance(v, Decimal) else v, "kind": t}
            for k, v, t in out if v not in (None, "", 0) or (t == "money" and v == 0 and k == "Book value")]


def entities(as_of: Optional[str] = None) -> list[dict[str, str]]:
    rep = portfolio.report(as_of)
    return ([{"entity": r["entity"], "name": r["investment_name"], "kind": "direct_investment"}
             for r in rep["companies"]]
            + [{"entity": r["entity"], "name": r["investment_name"], "kind": "fund"} for r in rep["funds"]])


def _by_year(events: list[dict]) -> list[dict[str, Any]]:
    """The accounting workbook's dated calls, income and distributions, by year."""
    out: dict[str, dict[str, float]] = {}
    for e in events:
        y = str(e["date"])[:4]
        row = out.setdefault(y, {"year": y, "call": 0.0, "income": 0.0, "distribution": 0.0, "value": 0.0})
        if e["kind"] in row:
            amt = float(e["amount"])
            row[e["kind"]] += abs(amt) if e["kind"] in ("call", "distribution") else amt
    return [out[y] for y in sorted(out)]
