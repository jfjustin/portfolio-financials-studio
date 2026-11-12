"""IRR — the "IRR" tab of the Investment Portfolio workbook, from dated cash flows.

Money-weighted (XIRR) returns as of a quarter, for the whole portfolio and for
the splits the team reports: Core vs Life Sciences investments, direct
investments vs fund investments, and each fund or company on its own.

The flows behind every figure, in USD (a non-USD entity converts at its rate):

    out  dated capital calls / investments entered or imported for the entity;
         failing those, each quarter's funding from the roll-forward (funds);
         failing that, the total invested on its initial investment date
    in   a fund's distributions by quarter from the roll-forward (return of
         capital + dividends); a company's exit proceeds on its exit date;
         failing a dated figure, the since-inception distributions as of the
         quarter (an estimate that understates the return)
    end  what is still held, as a final inflow at the quarter end: a fund's
         investment balance, a held company's value

Anything resting on an undated fallback is marked `estimated`, and every entity
carries the flows it was computed from, so a figure can be checked line by line.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from . import db, portfolio
from .extractors.base import quarter_bounds
from .performance import xirr

GROUPS = (("all", "All investments"), ("core", "Core investments"),
          ("life_sciences", "Life Sciences investments"), ("direct", "Direct investments"),
          ("funds", "Fund investments"))
SPLITS = {"all": ("all",), "group": ("core", "life_sciences"), "kind": ("direct", "funds")}


def _d(v) -> Optional[Decimal]:
    return portfolio._d(v)


def _iso(v) -> Optional[date]:
    return portfolio._iso(v)


def _flows(row: dict, quarters: list[dict], as_of_end: date, factor: Decimal) -> tuple[list[dict], list[str]]:
    """The entity's dated flows (USD) up to the quarter end, and the notes on
    any estimate among them."""
    flows: list[dict] = []
    notes: list[str] = []
    det = row.get("details") or {}

    def add(d: Optional[date], amount: Optional[Decimal], kind: str, source: str, est: bool = False):
        if d is None or amount is None or amount == 0 or d > as_of_end:
            return
        flows.append({"date": d.isoformat(), "amount": float(amount * factor), "kind": kind,
                      "source": source, "estimated": est})

    # ---- money out
    entered = [c for c in row.get("invested", []) if c["source"] == "entered"]
    rolled = [c for c in row.get("invested", []) if c["source"] != "entered"]
    if entered or rolled:
        for c in entered:
            add(_iso(c["date"]), -abs(Decimal(str(c["amount"]))), "call",
                c.get("description") or "investment entered")
        for c in rolled:
            add(_iso(c["date"]), -abs(Decimal(str(c["amount"]))), "call",
                c.get("description") or "roll-forward funding", est=True)
        if rolled:
            notes.append("some calls are a quarter's funding from the roll-forward, dated at the "
                         "quarter end")
    else:
        total = row.get("drawn_to_date") if row["kind"] == "fund" else row.get("total_invested")
        start = _iso(det.get("initial_investment_date")) or _iso(det.get("last_investment_date"))
        if start is None and quarters and re.fullmatch(r"20\d{2}-Q[1-4]", quarters[0]["period_label"] or ""):
            start = quarter_bounds(quarters[0]["period_label"])[0]
        if total is not None and start is not None:
            add(start, -abs(Decimal(str(total))), "call", "total invested, on the initial investment date",
                est=True)
            notes.append("no dated calls on file: the total invested is placed on the initial "
                         "investment date")
        elif total is not None:
            notes.append("no dated calls and no initial investment date on file — enter them in "
                         "its details")

    # ---- money in
    if row["kind"] == "fund":
        dated = Decimal(0)
        for q in quarters:
            label = q["period_label"]
            if not re.fullmatch(r"20\d{2}-Q[1-4]", label or ""):
                continue
            end = quarter_bounds(label)[1]
            amount = abs(Decimal(str(q.get("return_of_capital") or 0))) + abs(
                Decimal(str(q.get("dividends") or 0)))
            if amount:
                add(end, amount, "distribution", f"{label} distributions (roll-forward)")
                dated += amount
        total = row.get("distribution_to_date")
        if total is not None and Decimal(str(total)) > dated + 1:
            rest = Decimal(str(total)) - dated
            add(as_of_end, rest, "distribution",
                "distributions since inception not dated by quarter, placed at the quarter end",
                est=True)
            notes.append("part of the distributions has no date: placed at the quarter end "
                         "(understates the return)")
        add(as_of_end, _d(row.get("investment_balance")), "value", "investment balance held")
    else:
        if row.get("exited"):
            ex = row["exit"]
            proceeds = _d(ex.get("proceeds"))
            when = _iso(ex.get("date"))
            add(when or as_of_end, proceeds, "proceeds",
                "exit proceeds" + ("" if when else ", undated: placed at the quarter end"),
                est=when is None)
            if when is None and proceeds:
                notes.append("the exit has no date: proceeds placed at the quarter end")
            escrow = _d(ex.get("escrow"))
            if escrow:
                add(as_of_end, escrow, "value", "proceeds on escrow, still to come")
        else:
            value = row.get("total_value")
            if value is None:
                value = _d(det.get("book_value"))
                if value is not None:
                    notes.append("no fair value on file — the book value stands in")
            add(as_of_end, _d(value), "value", "value held")
    flows.sort(key=lambda f: (f["date"], f["amount"]))
    return flows, notes


def _rate(flows: list[dict]) -> Optional[float]:
    cf = [(date.fromisoformat(f["date"]), f["amount"]) for f in flows]
    return xirr(sorted(cf, key=lambda x: x[0]))


def _totals(flows: list[dict]) -> dict[str, float]:
    out = {"invested": 0.0, "returned": 0.0, "value": 0.0}
    for f in flows:
        if f["kind"] == "call":
            out["invested"] += -f["amount"]
        elif f["kind"] == "value":
            out["value"] += f["amount"]
        else:
            out["returned"] += f["amount"]
    return out


def report(as_of: Optional[str] = None) -> dict[str, Any]:
    """IRR per entity and per group, as of a quarter (default: the latest)."""
    rep = portfolio.report(as_of)
    summ = portfolio.summary(rep=rep)
    as_of_end = quarter_bounds(rep["as_of"])[1]
    lines = {ln["entity"]: ln for ln in summ["lines"]}
    quarters_by: dict[str, list[dict]] = {}
    for q in db.get_investments()["quarters"]:
        if portfolio._order(q["period_label"]) <= portfolio._order(rep["as_of"]):
            quarters_by.setdefault(q["entity_name"], []).append(q)
    for qs in quarters_by.values():
        qs.sort(key=lambda q: q["period_order"])
    entities: list[dict[str, Any]] = []
    left_out: list[dict[str, str]] = []
    for row in rep["funds"] + rep["companies"]:
        factor, why, fx_note = portfolio._usd(row, rep["as_of"])
        if factor is None:
            left_out.append({"entity": row["entity"], "name": row["investment_name"], "why": why})
            continue
        flows, notes = _flows(row, quarters_by.get(row["entity"], []), as_of_end, factor)
        if fx_note:
            notes.append(fx_note)
        ln = lines.get(row["entity"])
        group = ln["group"] if ln else portfolio._group(row)
        has_out = any(f["amount"] < 0 for f in flows)
        has_in = any(f["amount"] > 0 for f in flows)
        if not has_out:
            left_out.append({"entity": row["entity"], "name": row["investment_name"],
                             "why": "no investment on file" + (": " + notes[-1] if notes else "")})
            continue
        rate = _rate(flows) if has_in else None
        if not has_in:
            notes.append("nothing back yet and no value held: the return is a total loss (−100%)")
            rate = -1.0
        entities.append({"entity": row["entity"], "name": row["investment_name"], "kind": row["kind"],
                         "group": group, "status": ln["status"] if ln else ("fund" if row["kind"] == "fund" else "current"),
                         "irr_pct": round(rate * 100, 1) if rate is not None else None,
                         **{k: round(v, 2) for k, v in _totals(flows).items()},
                         "first_date": flows[0]["date"], "n_flows": len(flows),
                         "estimated": any(f["estimated"] for f in flows), "notes": notes,
                         "flows": flows})
    groups: dict[str, dict[str, Any]] = {}
    for key, label in GROUPS:
        members = [e for e in entities if key == "all"
                   or (key == "core" and e["group"] == "Core")
                   or (key == "life_sciences" and e["group"] != "Core")
                   or (key == "direct" and e["kind"] == "direct_investment")
                   or (key == "funds" and e["kind"] == "fund")]
        flows = [f for e in members for f in e["flows"]]
        rate = _rate(flows) if flows else None
        groups[key] = {"key": key, "label": label, "n": len(members),
                       "irr_pct": round(rate * 100, 1) if rate is not None else None,
                       **{k: round(v, 2) for k, v in _totals(flows).items()},
                       "first_date": min((e["first_date"] for e in members), default=None),
                       "estimated": any(e["estimated"] for e in members),
                       "members": [e["entity"] for e in members]}
    return {"as_of": rep["as_of"], "as_of_label": rep["as_of_label"], "quarters": rep["quarters"],
            "as_of_end": as_of_end.isoformat(), "groups": groups, "splits": SPLITS,
            "entities": entities, "left_out": left_out}
