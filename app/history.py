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

All amounts in the entity's own currency (its statements'), with USD alongside
where a rate is on file.
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
    for q in sorted((q for q in db.get_investments()["quarters"] if q["entity_name"] == entity),
                    key=lambda q: q["period_order"]):
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
    events = sorted(det.get("history") or [], key=lambda e: e["date"])
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
            sign = -1 if e["kind"] == "distribution" else 1
            v += Decimal(str(e["amount"])) * sign
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
    return {"entity": entity, "name": row["investment_name"], "kind": row["kind"], "currency": ccy,
            "as_of": rep["as_of"], "as_of_label": rep["as_of_label"], "quarters": rep["quarters"],
            "theme": row.get("theme") or row.get("region_sector") or "",
            "status": "exited" if row.get("exited") else (row.get("operating_status") or ""),
            "paid_in": _f(paid), "value_now": _f(now), "irr_pct": e["irr_pct"] if e else None,
            "irr_estimated": bool(e and e["estimated"]),
            "financials": financials, "roll_forward": roll, "investments": investments,
            "value": value_points, "value_basis": basis,
            "by_year": _by_year(events),
            "usd_rate": _f(portfolio.usd_rate_at(det, rep["as_of"])) if ccy != "USD" else 1.0}


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
            row[e["kind"]] += float(e["amount"])
    return [out[y] for y in sorted(out)]
