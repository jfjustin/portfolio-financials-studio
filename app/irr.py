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
from .extractors.base import previous_quarter, quarter_bounds
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
                f"{c.get('description') or 'investment'} · {c.get('document') or 'entered by hand'}")
        for c in rolled:
            add(_iso(c["date"]), -abs(Decimal(str(c["amount"]))), "call",
                f"{c.get('description') or 'funding'} · {c.get('document') or 'roll-forward'}", est=True)
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
            # what a master workbook already counted as distributed by its own
            # quarter is dated there, not at the latest quarter end
            wb_total = _d(det.get("distribution_to_date")) if det.get("workbook_as_of") else None
            wb_end = quarter_bounds(det["workbook_as_of"])[1] if wb_total else None
            wb_dated = sum((Decimal(str(abs(q.get("return_of_capital") or 0))) + Decimal(str(abs(q.get("dividends") or 0)))
                            for q in quarters if wb_end and quarter_bounds(q["period_label"])[1] <= wb_end
                            and re.fullmatch(r"20\d{2}-Q[1-4]", q["period_label"] or "")), Decimal(0))
            if wb_total and wb_end and wb_end <= as_of_end and wb_total - wb_dated > 1:
                part = min(rest, wb_total - wb_dated)
                add(wb_end, part, "distribution",
                    f"distributions by {det['workbook_as_of']} per the workbook, not dated by quarter, "
                    "placed at that quarter end", est=True)
                rest -= part
            if rest > 1:
                add(as_of_end, rest, "distribution",
                    "distributions since inception not dated by quarter, placed at the quarter end",
                    est=True)
            notes.append("part of the distributions has no date: placed at the quarter end it was "
                         "reported by (understates the return)")
        add(as_of_end, _d(row.get("investment_balance")), "value",
            "investment balance held · " + (row.get("sources", {}).get("investment_balance") or "entered"))
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
            add(as_of_end, _d(value), "value",
                "value held · " + (row.get("sources", {}).get("total_value") or "entered in its details"))
    flows.sort(key=lambda f: (f["date"], f["amount"]))
    return flows, notes


def _coverage(flows: list[dict]) -> list[dict[str, str]]:
    """What the figure covers, document by document: for each source, the kind
    of flow, the dates it spans and how many flows — so a fund whose calls come
    from the accounting workbook (2020–2025) and whose balance and distributions
    come from this year's statements says exactly that."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for f in flows:
        doc = f["source"].split(" · ")[-1] if " · " in f["source"] else f["source"]
        groups.setdefault((f["kind"], doc), []).append(f)
    out = []
    for (kind, doc), fs in sorted(groups.items(), key=lambda kv: kv[1][0]["date"]):
        dates = sorted(f["date"] for f in fs)
        out.append({"kind": kind, "document": doc, "from": dates[0], "to": dates[-1], "n": len(fs),
                    "estimated": any(f["estimated"] for f in fs)})
    return out


def _rate(flows: list[dict]) -> Optional[float]:
    cf = [(date.fromisoformat(f["date"]), f["amount"]) for f in flows]
    return xirr(sorted(cf, key=lambda x: x[0]))


def _totals(flows: list[dict]) -> dict[str, float]:
    out = {"invested": 0.0, "returned": 0.0, "value": 0.0}
    for f in flows:
        if f["kind"] in ("call", "opening"):
            out["invested"] += -f["amount"]
        elif f["kind"] == "value":
            out["value"] += f["amount"]
        else:
            out["returned"] += f["amount"]
    return out


def _window(entity: dict, flows: list[dict], row: dict, quarters: list[dict], start: str, end: str,
            as_of: str, factor: Decimal) -> tuple[list[dict], Optional[str]]:
    """The flows within a window of quarters, with what was held at its start
    as an opening outflow and what was held at its end as a closing inflow —
    both from the roll-forward's ending balances, never guessed. Returns the
    flows, or (None, why) when the window can't be valued for this entity."""
    start_date, end_date = quarter_bounds(start)[0], quarter_bounds(end)[1]
    inside = [f for f in flows if start_date.isoformat() <= f["date"] <= end_date.isoformat()
              and not (f["kind"] == "value" and end != as_of)]
    first = flows[0]["date"] if flows else None
    balance = {q["period_label"]: q.get("ending_balance") for q in quarters}
    out: list[dict] = list(inside)
    if first and first < start_date.isoformat():
        if row["kind"] != "fund":
            if row.get("exited") and (row["exit"].get("date") or "") < start_date.isoformat():
                return [], "closed before the window"
            return [], "no value on file at the window start (a company's value is only kept as of now)"
        prev = previous_quarter(start)
        opening = balance.get(prev)
        det = row.get("details") or {}
        if opening is None and det.get("workbook_as_of") == prev and det.get("investment_balance"):
            opening = det["investment_balance"]          # the workbook's balance, as of that quarter
        if opening is None:
            return [], f"no balance on file for {prev}, the quarter before the window"
        out.append({"date": start_date.isoformat(), "amount": -float(Decimal(str(opening)) * factor),
                    "kind": "opening", "source": f"balance at the end of {prev}, as the opening value",
                    "estimated": False})
    if end != as_of:
        if row["kind"] == "fund":
            closing = balance.get(end)
            det = row.get("details") or {}
            if closing is None and det.get("workbook_as_of") == end and det.get("investment_balance"):
                closing = det["investment_balance"]
            if closing is None:
                return [], f"no balance on file for {end}, the window's last quarter"
            out.append({"date": end_date.isoformat(), "amount": float(Decimal(str(closing)) * factor),
                        "kind": "value", "source": f"balance at the end of {end}, as the closing value",
                        "estimated": False})
        elif not (row.get("exited") and (row["exit"].get("date") or "") <= end_date.isoformat()):
            return [], "no value on file at the window end (a company's value is only kept as of now)"
    out.sort(key=lambda f: (f["date"], f["amount"]))
    if not any(f["amount"] < 0 for f in out):
        return [], "nothing invested within the window"
    return out, None


def report(as_of: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None,
           vintage_from: Optional[int] = None, vintage_to: Optional[int] = None) -> dict[str, Any]:
    """IRR per entity and per group, as of a quarter (default: the latest).
    With `start` and `end` quarters, the return earned within that window;
    with vintage years, only entities first invested in within them."""
    rep = portfolio.filter_vintage(portfolio.report(as_of), vintage_from, vintage_to)
    summ = portfolio.summary(rep=rep)
    as_of_end = quarter_bounds(rep["as_of"])[1]
    if (start and not end) or (end and not start):
        raise ValueError("a window needs both a start and an end quarter")
    if start and end:
        if portfolio._order(start) > portfolio._order(end):
            start, end = end, start
        if portfolio._order(end) > portfolio._order(rep["as_of"]):
            end = rep["as_of"]
        if portfolio._order(start) > portfolio._order(rep["as_of"]):
            raise ValueError("the window starts after the as-of quarter")
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
        # the calls on file should add up to what was paid in: a shortfall means
        # the dated investments are incomplete, and the return rests on them
        paid = row.get("drawn_to_date") if row["kind"] == "fund" else row.get("total_invested")
        called = sum(-f["amount"] for f in flows if f["kind"] == "call")
        if paid is not None and called and abs(called - float(Decimal(str(paid)) * factor)) > max(
                1.0, 0.01 * float(Decimal(str(paid)) * factor)):
            notes.append(f"the dated investments on file add up to {called:,.0f}, but paid-in is "
                         f"{float(Decimal(str(paid)) * factor):,.0f} — enter the missing calls")
            for f in flows:
                if f["kind"] == "call":
                    f["estimated"] = True
        if start and end:
            flows, why = _window({}, flows, row, quarters_by.get(row["entity"], []), start, end,
                                 rep["as_of"], factor)
            if why:
                left_out.append({"entity": row["entity"], "name": row["investment_name"], "why": why})
                continue
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
                         "first_date": flows[0]["date"], "last_date": flows[-1]["date"],
                         "n_flows": len(flows), "coverage": _coverage(flows),
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
            "window": {"start": start, "end": end} if start and end else None,
            "vintage": rep.get("vintage"),
            "entities": entities, "left_out": left_out}
