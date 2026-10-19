"""Portfolio performance analytics — the Direct Investment / Fund Investment
summary tables and Lam Capital's overall TVPI / DPI / IRR.

Mirrors the shape of Lam Capital's own "Portfolio Value" reports (direct
investments: cost / latest FMV / book value / unrealized gain; funds: vintage,
commit, draw, distribution, balance, QoQ change, total % gain/loss) as closely
as the extracted data model supports. Built entirely from TRUSTED facts — the
same human-in-the-loop guarantee as the rest of the app.

Honest limitations (documented rather than faked):
  * "Vintage" is approximated as the earliest period with trusted data for that
    fund, not the fund's true legal vintage year (not currently extracted).
  * "# Invested" (portfolio companies per fund) is not computed — the app does
    not yet extract per-holding schedules of investments within a fund.
  * IRR is a money-weighted XIRR over the fund's own period-level net cash
    flows (funding out, distributions in, latest balance as a terminal inflow),
    using each period's derived calendar end-date. It is a reasonable estimate,
    not a substitute for the GP's own cash-flow-accurate IRR.
"""
from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from . import db


# --------------------------------------------------------------------------
# Period -> calendar date (for XIRR cash-flow timing)
# --------------------------------------------------------------------------
def _period_end_date(period: str) -> Optional[date]:
    """Best-effort last calendar day covered by a normalized period label."""
    if not period:
        return None
    m = re.search(r"(20\d{2})-Q([1-4])", period)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        month = {1: 3, 2: 6, 3: 9, 4: 12}[q]
        return date(year, month, calendar.monthrange(year, month)[1])
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", period)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.search(r"^(20\d{2})-(\d{2})$", period)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, calendar.monthrange(y, mo)[1])
    m = re.search(r"(20\d{2})", period)
    if m:
        return date(int(m.group(1)), 12, 31)  # "YYYY-FY" -> year-end approximation
    return None


# --------------------------------------------------------------------------
# XIRR — money-weighted return over irregular dated cash flows
# --------------------------------------------------------------------------
def xirr(cashflows: list[tuple[date, float]]) -> Optional[float]:
    """Annualized money-weighted return. None if it can't be solved.

    Newton's method with a bisection fallback; pure Python, no numpy/scipy.
    Convention: outflows (capital calls) negative, inflows (distributions,
    terminal value) positive.
    """
    if len(cashflows) < 2:
        return None
    if not any(cf < 0 for _d, cf in cashflows) or not any(cf > 0 for _d, cf in cashflows):
        return None  # needs at least one outflow and one inflow to be solvable
    t0 = cashflows[0][0]
    years = [(d - t0).days / 365.0 for d, _cf in cashflows]

    def npv(rate: float) -> float:
        return sum(cf / ((1.0 + rate) ** t) for t, (_d, cf) in zip(years, cashflows))

    def dnpv(rate: float) -> float:
        return sum(-t * cf / ((1.0 + rate) ** (t + 1))
                   for t, (_d, cf) in zip(years, cashflows) if t > 0)

    rate = 0.1
    for _ in range(100):
        f = npv(rate)
        df = dnpv(rate)
        if abs(df) < 1e-12:
            break
        new_rate = rate - f / df
        if new_rate <= -0.9999:
            new_rate = (rate - 0.9999) / 2  # keep (1+rate) positive
        if abs(new_rate - rate) < 1e-7:
            return round(new_rate, 5)
        rate = new_rate

    # bisection fallback over a wide bracket
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None  # no sign change in bracket; can't solve robustly
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            return round(mid, 5)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return round((lo + hi) / 2, 5)


# --------------------------------------------------------------------------
# Direct investments summary
# --------------------------------------------------------------------------
def direct_investment_summary() -> list[dict[str, Any]]:
    """One row per direct-investment company: cost, FMV, book value, unrealized
    gain/(loss), ownership %, as of the latest trusted period."""
    by_entity: dict[str, dict[str, Any]] = {}
    for row in db.grand_ledger():
        if row["entity_kind"] != "direct_investment":
            continue
        e = by_entity.setdefault(row["entity_name"], {"period_order": -1, "metrics": {}})
        if row["period_order"] >= e["period_order"]:
            if row["period_order"] > e["period_order"]:
                e["metrics"] = {}  # newer period supersedes older values
            e["period_order"] = row["period_order"]
            e["period"] = row["period"]
            e["fiscal_label"] = row.get("fiscal_label")
            e["metrics"][row["metric_key"]] = row["value"]

    out = []
    for name, e in sorted(by_entity.items()):
        m = e["metrics"]
        cost = _f(m.get("cost_basis"))
        fmv = _f(m.get("fair_value"))
        unrealized = _f(m.get("unrealized_gain"))
        if unrealized is None and cost is not None and fmv is not None:
            unrealized = fmv - cost
        out.append({
            "company": name,
            "as_of_period": e.get("period"),
            "fiscal_label": e.get("fiscal_label"),
            "investment_cost": cost,
            "estimated_fmv": fmv,
            "book_value": cost,  # no separate amortized-cost tracking yet; cost is the proxy
            "unrealized_gain_loss": unrealized,
            "ownership_pct": _f(m.get("ownership_pct")),
            "moic": _f(m.get("moic")) or (round(fmv / cost, 2) if cost else None),
        })
    return out


def operating_metrics_summary() -> list[dict[str, Any]]:
    """One row per direct-investment company with operating-performance data
    (Revenue, Operating Cash Flow, CapEx, Ending Cash, Operating Expense) —
    the portfolio company's OWN quarterly performance, distinct from
    `direct_investment_summary()`'s cost/FMV accounting of Lam's stake.
    Mirrors Lam Capital's own "Direct Investment" quarterly reporting format.
    """
    by_entity: dict[str, dict[str, Any]] = {}
    for row in db.grand_ledger():
        if row["entity_kind"] != "direct_investment":
            continue
        if row["metric_key"] not in ("revenue", "operating_cash_flow", "capex",
                                     "ending_cash", "operating_expense"):
            continue
        e = by_entity.setdefault(row["entity_name"], {"period_order": -1, "metrics": {}})
        if row["period_order"] >= e["period_order"]:
            if row["period_order"] > e["period_order"]:
                e["metrics"] = {}
            e["period_order"] = row["period_order"]
            e["period"] = row["period"]
            e["fiscal_label"] = row.get("fiscal_label")
            e["metrics"][row["metric_key"]] = row["value"]

    out = []
    for name, e in sorted(by_entity.items()):
        m = e["metrics"]
        revenue = _f(m.get("revenue"))
        opex = _f(m.get("operating_expense"))
        out.append({
            "company": name,
            "as_of_period": e.get("period"),
            "fiscal_label": e.get("fiscal_label"),
            "revenue": revenue,
            "operating_cash_flow": _f(m.get("operating_cash_flow")),
            "capex": _f(m.get("capex")),
            "ending_cash": _f(m.get("ending_cash")),
            "operating_expense": opex,
            "net_burn": (round(opex - revenue, 2)
                        if revenue is not None and opex is not None else None),
        })
    return out


# --------------------------------------------------------------------------
# Fund investments summary
# --------------------------------------------------------------------------
def fund_summary() -> list[dict[str, Any]]:
    """One row per fund: since/"vintage" proxy, cumulative funding (draw),
    cumulative distributions, latest balance, QoQ change, total % gain/loss."""
    quarters_by_entity: dict[str, list[dict]] = {}
    for q in db.get_investments()["quarters"]:
        quarters_by_entity.setdefault(q["entity_name"], []).append(q)

    commitments = {e["entity_name"]: e.get("total_commitment")
                   for e in db.get_investments()["entities"]}

    out = []
    for name, qs in sorted(quarters_by_entity.items()):
        qs = sorted(qs, key=lambda r: r["period_order"])
        cum_funding = sum(q["funding"] or 0 for q in qs)
        # Distributions are stored as the (negative) balance-impact in the
        # roll-forward — a distribution REDUCES the capital account. Reporting
        # and TVPI/DPI/IRR need the positive cash-received magnitude instead.
        cum_distributions = sum(abs(q["return_of_capital"] or 0) + abs(q["dividends"] or 0)
                                for q in qs)
        latest = qs[-1]
        ending = latest.get("ending_balance")
        qoq_change = None
        if len(qs) >= 2 and qs[-2].get("ending_balance") is not None and ending is not None:
            qoq_change = ending - qs[-2]["ending_balance"]
        total_gain_pct = None
        if cum_funding:
            total_gain_pct = round(
                ((ending or 0) + cum_distributions - cum_funding) / cum_funding * 100, 1)
        out.append({
            "fund": name,
            "since": qs[0]["period_label"],         # proxy for "vintage" (earliest trusted period)
            "latest_period": latest["period_label"],
            "fiscal_label": db.fiscal_info(latest["period_label"])["fiscal_label"],
            "cumulative_funding": round(cum_funding, 2),
            "cumulative_distributions": round(cum_distributions, 2),
            "commitment": commitments.get(name),
            "lam_investment_balance": ending,
            "qoq_change": qoq_change,
            "total_pct_gain_loss": total_gain_pct,
        })
    return out


# --------------------------------------------------------------------------
# Portfolio-level TVPI / DPI / IRR (Lam Capital overall, across all funds)
# --------------------------------------------------------------------------
def portfolio_performance() -> dict[str, Any]:
    """Aggregate TVPI, DPI, and a money-weighted IRR across all fund entities."""
    quarters_by_entity: dict[str, list[dict]] = {}
    for q in db.get_investments()["quarters"]:
        quarters_by_entity.setdefault(q["entity_name"], []).append(q)

    total_funding = total_distributions = total_nav = 0.0
    cashflows: list[tuple[date, float]] = []
    for name, qs in quarters_by_entity.items():
        qs = sorted(qs, key=lambda r: r["period_order"])
        for q in qs:
            d = _period_end_date(q["period_label"])
            funding = q.get("funding") or 0
            # see fund_summary(): distributions are stored as the negative
            # balance-impact; use the positive cash-received magnitude here.
            dist = abs(q.get("return_of_capital") or 0) + abs(q.get("dividends") or 0)
            total_funding += funding
            total_distributions += dist
            if d:
                if funding:
                    cashflows.append((d, -funding))
                if dist:
                    cashflows.append((d, dist))
        ending = qs[-1].get("ending_balance") or 0
        total_nav += ending
        d = _period_end_date(qs[-1]["period_label"])
        if d and ending:
            cashflows.append((d, ending))  # terminal value as a final inflow

    cashflows.sort(key=lambda cf: cf[0])
    tvpi = round((total_distributions + total_nav) / total_funding, 2) if total_funding else None
    dpi = round(total_distributions / total_funding, 2) if total_funding else None
    irr = xirr(cashflows)

    return {
        "total_funding": round(total_funding, 2),
        "total_distributions": round(total_distributions, 2),
        "total_nav": round(total_nav, 2),
        "tvpi": tvpi,
        "dpi": dpi,
        "irr_pct": round(irr * 100, 1) if irr is not None else None,
        "n_funds": len(quarters_by_entity),
    }


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(Decimal(str(v)))
    except Exception:
        return None
