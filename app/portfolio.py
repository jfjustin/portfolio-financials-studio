"""Lam Capital's "Investment Portfolio" report (e.g. "Investment Portfolio Jun'26").

One sheet per fund in Lam's layout — Investment Name, Type, Current / Most
Recent Round, Vintage, Fund Size, Commitment, Future Draw, Region / Sector,
Investment Balance, Drawn to Date, Distribution to Date, the fund's Latest
Stats (date, # of investments, cost, value, gain %) and its dated "Invested"
history — one per direct investment, and a "Summary-Cash Flows" sheet whose
cells reference them exactly as Lam's workbook does ($B$2, $B$3, $B$10, ...).

Figures come from approved values only:

  Investment Balance    the roll-forward's latest ending balance
  Commitment            fund parameters, else the statements' commitment
  Drawn to Date         the statements' contributions since inception, else
                        the funding of the quarters on file
  Distribution to Date  the statements' distributions since inception, else
                        the distributions of the quarters on file
  Future Draw           the statements' remaining commitment, else
                        commitment − drawn to date
  Latest Stats          the fund's schedule of investments, else the details
                        entered for the fund
  Invested              dated calls entered for the fund; a quarter without one
                        shows that quarter's funding from the roll-forward

Master data (type, vintage, fund size, region / sector, accounting details;
for companies theme, round, status, exit) is entered once per entity. Every
figure carries where it came from.
"""
from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select

from . import db
from .extractors.base import quarter_bounds, quarter_label
from .models import parse_number

RF = "investment_roll_forward"

# master data, per kind: (key, label, type)
FUND_FIELDS = (
    ("display_name", "Investment Name", "text"),
    ("investment_type", "Investment Type", "text"),
    ("current_round", "Current / Most Recent Round", "text"),
    ("vintage", "Vintage", "year"),
    ("fund_size", "Fund Size", "money"),
    ("region_sector", "Region / Sector Focus", "text"),
    ("entity_type", "Entity Type", "text"),
    ("accounting_method", "Method of Accounting", "text"),
    ("gl_account", "GL Account", "text"),
    ("company_code", "Company Code", "text"),
    ("ownership_pct", "% Ownership", "percent"),
    ("initial_investment_date", "Initial Investment Date", "date"),
    ("terms", "Terms", "longtext"),
    ("stats_as_of", "Latest Stats — date", "date"),
    ("stats_count", "Latest Stats — # of investments", "int"),
    ("stats_cost", "Latest Stats — cost basis", "money"),
    ("stats_value", "Latest Stats — value", "money"),
    # typed in when no statement / roll-forward on file shows the figure
    ("investment_balance", "Investment Balance — if no roll-forward has it", "money"),
    ("drawn_to_date", "Drawn to Date — if no statement shows it", "money"),
    ("distribution_to_date", "Distribution to Date — if no statement shows it", "money"),
    ("future_draw", "Future Draw — if no statement shows it", "money"),
    ("returned_capital", "Returned capital to date — the part of distributions that is "
                         "capital, not gain", "money"),
    ("portfolio_group", "Portfolio group (e.g. Core, Life Sciences)", "text"),
    ("usd_rate", "USD per unit of its currency (non-USD funds)", "rate"),
    ("usd_rates", "USD rate by quarter, e.g. 2026-Q1=1.08; 2026-Q2=1.10", "rates"),
)
_FUND_FIGURES = (("commitment", "Commitment"), ("investment_balance", "Investment Balance"),
                 ("drawn_to_date", "Drawn to Date"), ("distribution_to_date", "Distribution to Date"),
                 ("future_draw", "Future Draw"))
# statement values that feed the fund figures, while they wait for review
_PENDING_FEEDS = {"commitment": "commitment", "ending_balance": "investment_balance",
                  "cumulative_contributions": "drawn_to_date",
                  "cumulative_distributions": "distribution_to_date",
                  "remaining_commitment": "future_draw", "fund_size": "fund_size",
                  "portfolio_investments": "stats", "portfolio_cost": "stats",
                  "portfolio_fair_value": "stats"}
COMPANY_FIELDS = (
    ("display_name", "Investment Name", "text"),
    ("investment_type", "Investment Type", "text"),
    ("theme", "Theme", "text"),
    ("current_round", "Current / Most Recent Round", "text"),
    ("security", "Investments (security held)", "text"),
    ("last_investment_date", "Most Recent Investment Date", "date"),
    ("total_invested", "Total Investment to Date", "money"),
    ("projected_investment", "Projected Future Investments", "money"),
    ("total_value", "Total Value", "money"),
    ("impairment", "Impairment to date (write-down)", "money"),
    ("book_value", "Book Value (carrying value)", "money"),
    ("operating_status", "Operating Status", "choice:Active,Inactive"),
    ("exit_date", "Date of Exit", "date"),
    ("exit_value", "Exit — Total Value", "money"),
    ("exit_proceeds", "Exit — Total Received Proceeds", "money"),
    ("exit_escrow", "Exit — Total Proceeds on Escrow", "money"),
    ("portfolio_group", "Portfolio group (e.g. Core, Life Sciences)", "text"),
    ("usd_rate", "USD per unit of its currency (if not USD)", "rate"),
    ("usd_rates", "USD rate by quarter, e.g. 2026-Q1=1.08; 2026-Q2=1.10", "rates"),
)
_MONEY = {k for k, _l, t in FUND_FIELDS + COMPANY_FIELDS if t == "money"}


def _d(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return parse_number(str(v))[0]


def _iso(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def parse_date(raw: str) -> Optional[str]:
    """"3/15/2019", "2019-03-15", "15 Mar 2019" -> "2019-03-15"."""
    from .extractors.base import normalize_period
    raw = (raw or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", raw)
    if m:
        y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        try:
            return date(y, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    p = normalize_period(raw)
    return p if p and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p) else None


_RATE_PAIR = re.compile(r"(20\d{2})\s*-?\s*Q([1-4])\s*[=:]\s*([0-9][0-9.,]*)", re.I)


def _rates(raw) -> dict[str, Decimal]:
    """"2026-Q1=1.08; 2026-Q2=1.10" -> {"2026-Q1": 1.08, "2026-Q2": 1.10}."""
    out: dict[str, Decimal] = {}
    for y, q, v in _RATE_PAIR.findall(str(raw or "")):
        rate = parse_number(v.rstrip(".,"))[0]
        if rate:
            out[f"{y}-Q{q}"] = rate
    return out


def coerce_details(body: dict[str, Any]) -> dict[str, Any]:
    """Typed master-data values from what a person typed or a sheet holds: money
    ("3m", "1,250,000"), dates, years, choices. An empty value removes the
    field. Raises ValueError naming the field that can't be read."""
    fields = {k: t for k, _l, t in FUND_FIELDS + COMPANY_FIELDS}
    values: dict[str, Any] = {}
    for key, raw in body.items():
        if key == "kind":
            if raw in (None, ""):
                values["kind"] = None          # a hand-added entry leaves the report
            elif raw not in ("fund", "direct_investment"):
                raise ValueError("kind must be fund or direct_investment")
            else:
                values["kind"] = raw
            continue
        if key == "commitment":
            continue
        if key not in fields:
            raise ValueError(f"unknown field '{key}'")
        text = str(raw if raw is not None else "").strip()
        kind = fields[key]
        if not text:
            values[key] = None
        elif kind in ("money", "int", "percent", "year", "rate"):
            v = parse_number(text)[0]
            if v is None:
                raise ValueError(f"{key}: can't read a number from '{text}'")
            values[key] = str(int(v)) if kind in ("int", "year") else str(v)
        elif kind == "rates":
            rates = _rates(text)
            if not rates:
                raise ValueError(f"{key}: write each quarter's rate as 2026-Q1=1.08")
            values[key] = "; ".join(f"{q}={r}" for q, r in sorted(rates.items()))
        elif kind == "date":
            iso = parse_date(text)
            if iso is None:
                raise ValueError(f"{key}: can't read a date from '{text}'")
            values[key] = iso
        elif kind.startswith("choice:"):
            choice = next((c for c in kind[7:].split(",") if c.lower() == text.lower()), None)
            if choice is None:
                raise ValueError(f"{key} must be one of {kind[7:]}")
            values[key] = choice
        else:
            values[key] = text[:2000]
    return values


def _order(period: str) -> int:
    return db._period_order(period or "")


def _filenames() -> dict[int, str]:
    with db.get_engine().connect() as conn:
        return {r.id: r.filename for r in conn.execute(
            select(db.submissions.c.id, db.submissions.c.filename))}


def _latest(metrics: set[str], until: int) -> dict[tuple[str, str], dict]:
    """The newest approved value per (entity, metric) up to a quarter."""
    names = _filenames()
    rows = [f for f in db.trusted_facts()
            if f["metric_key"] in metrics and _order(f["period"]) <= until]
    rows.sort(key=lambda f: (_order(f["period"]), f["status"] == "confirmed", f["id"]))
    out: dict[tuple[str, str], dict] = {}
    for f in rows:
        out[(f["entity_name"], f["metric_key"])] = {
            "value": Decimal(f["value"]), "period": f["period"], "status": f["status"],
            "file": names.get(f["submission_id"], ""), "kind": f["entity_kind"],
            "currency": f["currency"] or "USD"}
    return out


def quarters_on_file() -> list[str]:
    qs = {q["period_label"] for q in db.get_investments()["quarters"]
          if re.fullmatch(r"20\d{2}-Q[1-4]", q["period_label"] or "")}
    return sorted(qs)


def _src(fact: dict) -> str:
    return (f"{fact['file']} ({fact['period']}, {fact['status']})" if fact.get("file")
            else f"{fact['period']} ({fact['status']})")


def report(as_of: Optional[str] = None) -> dict[str, Any]:
    """Funds and direct investments as of a calendar quarter (default: the latest
    quarter with an approved roll-forward)."""
    qs = quarters_on_file()
    if not as_of:
        as_of = qs[-1] if qs else quarter_label(date.today())
    until = _order(as_of)
    inv = db.get_investments()
    details = db.all_details()
    profiles = {}
    with db.get_engine().connect() as conn:
        for r in conn.execute(select(db.fund_profiles)).mappings():
            profiles[r["entity_name"]] = dict(r)
    calls = db.get_capital_calls()
    names = _filenames()
    waiting: dict[str, list[dict]] = {}
    waiting_kind: dict[str, str] = {}
    for f in db.unapproved_facts():
        if _order(f["period"] or "") <= until and f["entity_kind"] in ("fund", "direct_investment"):
            waiting_kind.setdefault(f["entity_name"], f["entity_kind"])
            waiting.setdefault(f["entity_name"], []).append(
                {"field": _PENDING_FEEDS.get(f["metric_key"]), "metric": f["metric_label"],
                 "submission_id": f["submission_id"], "file": names.get(f["submission_id"], "")})
    facts = _latest({"commitment", "remaining_commitment", "cumulative_contributions",
                     "cumulative_distributions", "fund_size", "portfolio_investments",
                     "portfolio_cost", "portfolio_fair_value", "ownership_pct",
                     "cost_basis", "fair_value"}, until)
    quarters_by: dict[str, list[dict]] = {}
    for q in inv["quarters"]:
        if _order(q["period_label"]) <= until:
            quarters_by.setdefault(q["entity_name"], []).append(q)
    kinds = {e["entity_name"]: e["entity_type"] for e in inv["entities"]}
    for (entity, _m), f in facts.items():
        kinds.setdefault(entity, f["kind"])
    for entity, kind in waiting_kind.items():
        kinds.setdefault(entity, kind)      # values on file, none approved yet: still listed
    for entity, det in details.items():
        if det.get("kind"):
            kinds[entity] = det["kind"]
    # files that gave no figures at all still put their fund / company on the
    # report, flagged for a person
    from .report_types import REPORT_TYPES
    unread: dict[str, list[dict]] = {}
    for sub in db.unread_submissions():
        rt = REPORT_TYPES.get(sub["report_type"])
        if rt and rt.entity_kind in ("fund", "direct_investment") \
                and _order(sub["period"] or "") <= until:
            kinds.setdefault(sub["entity_name"], rt.entity_kind)
            unread.setdefault(sub["entity_name"], []).append(sub)
    files_by: dict[str, list[dict]] = {}
    for sub in db.list_submissions():
        if sub.get("extractor") != db.MANUAL and _order(sub.get("period") or "") <= until:
            files_by.setdefault(sub["entity_name"], []).append(
                {"id": sub["id"], "file": sub["filename"], "report_type": sub["report_type"]})
    own_statement = {f["entity_name"] for f in db.get_facts()
                     if f["report_type"] == RF and f["status"] != "rejected"}
    rf_trusted: dict[tuple[str, str], dict[str, dict]] = {}
    for f in db.trusted_facts():
        if f["report_type"] == RF:
            rf_trusted.setdefault((f["entity_name"], f["period"]), {})[f["metric_key"]] = f
    funds = [_fund(name, quarters_by.get(name, []), details.get(name, {}),
                   profiles.get(name, {}), facts, [c for c in calls if c["entity_name"] == name],
                   until, waiting.get(name, []),
                   {"files": files_by.get(name, []), "own_statement": name in own_statement,
                    "unread": {sub["id"] for sub in unread.get(name, [])},
                    "rf_trusted": rf_trusted, "names": names})
             for name in sorted((k for k, v in kinds.items() if v == "fund"), key=_natural)]
    companies = [_company(name, details.get(name, {}), facts,
                          [c for c in calls if c["entity_name"] == name], until,
                          files_by.get(name, []))
                 for name in sorted((k for k, v in kinds.items() if v == "direct_investment"),
                                    key=_natural)]
    for co in companies:
        files: dict[int, list[str]] = {}
        for w in waiting.get(co["entity"], []):
            if w["metric"] not in files.setdefault(w["submission_id"], []):
                files[w["submission_id"]].append(w["metric"])
        co["issues"] += [{"kind": "review", "submission_id": sid,
                          "message": ", ".join(m) + ": read from a file, waiting for review"}
                         for sid, m in files.items()]
    currency_by: dict[str, str] = {}
    for f in sorted(db.trusted_facts(), key=lambda f: f["id"]):
        if f.get("currency") and f["unit"] == "currency":
            currency_by[f["entity_name"]] = f["currency"]
    for row in funds:
        row["currency"] = currency_by.get(row["entity"], "USD")
    for row in companies:
        row["currency"] = "USD"        # holding records are kept in USD
    for row in funds + companies:
        row["issues"] = [{"kind": "unread", "submission_id": sub["id"],
                          "message": f"No figures could be read from {sub['filename']} — open "
                                     "it to see why, or enter the figures in the details"}
                         for sub in unread.get(row["entity"], [])] + row["issues"]
    return {"as_of": as_of, "as_of_label": _month_label(as_of), "quarters": qs or [as_of],
            "funds": funds, "companies": companies, "totals": _totals(funds, companies),
            "fund_fields": [{"key": k, "label": l, "type": t} for k, l, t in FUND_FIELDS],
            "company_fields": [{"key": k, "label": l, "type": t} for k, l, t in COMPANY_FIELDS]}


def _natural(name: str) -> list:
    """"Fund 3" before "Fund 11"."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _month_label(quarter: str) -> str:
    """"2026-Q2" -> "Jun'26" (the month the quarter ends)."""
    if not re.fullmatch(r"20\d{2}-Q[1-4]", quarter or ""):
        return quarter or ""
    end = quarter_bounds(quarter)[1]
    return f"{end.strftime('%b')}'{end.strftime('%y')}"


def _fund(name: str, quarters: list[dict], det: dict, prof: dict, facts: dict,
          calls: list[dict], until: int, waiting: Optional[list[dict]] = None,
          ctx: Optional[dict] = None) -> dict[str, Any]:
    ctx = ctx or {"files": [], "own_statement": False, "rf_trusted": {}, "names": {}}
    # whole-fund reports only (no statement of Lam's own, none unread — an unread
    # scan may be Lam's statement): Lam's share needs Lam's commitment
    whole_fund = ([x["id"] for x in ctx["files"] if x["report_type"] == "fund_financials"]
                  if not ctx["own_statement"] and not ctx.get("unread") else [])
    ctx = {**ctx, "whole_fund": whole_fund}
    quarters = sorted(quarters, key=lambda q: q["period_order"])
    src: dict[str, str] = {}

    def fact(metric: str) -> Optional[Decimal]:
        f = facts.get((name, metric))
        if f is None:
            return None
        src[metric] = _src(f)
        return abs(f["value"]) if metric != "ownership_pct" else f["value"]

    mismatches: list[str] = []

    def entered(field: str, label: str, found: Optional[Decimal], found_src: str
                ) -> Optional[Decimal]:
        """A figure typed into the fund's details wins; if a document also has it
        and disagrees, that's flagged for a person to settle."""
        typed = _d(det.get(field))
        if typed is None:
            return found
        src[field] = "entered"
        if found is not None and abs(typed - found) > max(Decimal(1), abs(found) * Decimal("0.001")):
            mismatches.append(f"{label}: entered {typed:,.2f}, but {found_src} shows {found:,.2f} "
                              "— clear the entered value to use the document's")
        return typed

    balance = quarters[-1]["ending_balance"] if quarters else None
    if balance is not None:
        balance = Decimal(str(balance))
        src["investment_balance"] = f"roll-forward {quarters[-1]['period_label']} ending balance"
    balance = entered("investment_balance", "Investment Balance", balance,
                      src.get("investment_balance", ""))
    stated = fact("commitment")
    commitment = _d(prof.get("commitment"))
    if commitment is not None:
        if stated is not None and abs(commitment - stated) > max(Decimal(1), stated * Decimal("0.001")):
            mismatches.append(f"Commitment: fund parameters say {commitment:,.2f}, but "
                              f"{src['commitment']} shows {stated:,.2f}")
        src["commitment"] = "fund parameters"
    else:
        commitment = stated
    # since-inception figures from the statements; failing that, the quarters on
    # file — an estimate until a statement with the since-inception figure arrives
    estimated: set[str] = set()
    on_file = f"{len(quarters)} quarter(s) on file ({quarters[0]['period_label']}" + (
        f"–{quarters[-1]['period_label']})" if len(quarters) > 1 else ")") if quarters else ""
    drawn = fact("cumulative_contributions")
    if drawn is not None:
        src["drawn_to_date"] = src.pop("cumulative_contributions")
    elif quarters:
        drawn = sum((Decimal(str(q["funding"] or 0)) for q in quarters), Decimal(0))
        src["drawn_to_date"] = f"funding of the {on_file} only"
        estimated.add("drawn_to_date")
    distributed = fact("cumulative_distributions")
    if distributed is not None:
        src["distribution_to_date"] = src.pop("cumulative_distributions")
    elif quarters:
        distributed = sum((abs(Decimal(str(q["return_of_capital"] or 0)))
                           + abs(Decimal(str(q["dividends"] or 0))) for q in quarters), Decimal(0))
        src["distribution_to_date"] = f"distributions of the {on_file} only"
        estimated.add("distribution_to_date")
    for field, label in (("drawn_to_date", "Drawn to Date"),
                         ("distribution_to_date", "Distribution to Date")):
        if det.get(field) not in (None, ""):
            found = drawn if field == "drawn_to_date" else distributed
            typed = entered(field, label, None if field in estimated else found,
                            src.get(field, ""))
            estimated.discard(field)
            if field == "drawn_to_date":
                drawn = typed
            else:
                distributed = typed
    # the part of the distributions that gave capital back rather than paid out a gain
    returned, returned_estimated = _d(det.get("returned_capital")), False
    if returned is not None:
        src["returned_capital"] = "entered"
    elif quarters:
        returned = sum((abs(Decimal(str(q["return_of_capital"] or 0))) for q in quarters),
                       Decimal(0))
        src["returned_capital"] = f"return of capital of the {on_file} only"
        returned_estimated = True
    if returned is not None and distributed is not None:
        returned = min(abs(returned), distributed)
    future = fact("remaining_commitment")
    if future is not None:
        src["future_draw"] = src.pop("remaining_commitment")
    future = entered("future_draw", "Future Draw", future, src.get("future_draw", ""))
    if future is None and commitment is not None and drawn is not None:
        future = max(commitment - drawn, Decimal(0))
        src["future_draw"] = "commitment − drawn to date"
        if "drawn_to_date" in estimated:
            estimated.add("future_draw")
    fund_size = _d(det.get("fund_size"))
    if fund_size is not None:
        src["fund_size"] = "fund details"
    else:
        fund_size = fact("fund_size")

    # Latest Stats: entered details win (a reviewer's override), else the schedule
    stats = {"as_of": det.get("stats_as_of"), "count": _d(det.get("stats_count")),
             "cost": _d(det.get("stats_cost")), "value": _d(det.get("stats_value"))}
    stats_src = "fund details" if any(v is not None for v in stats.values()) else ""
    for key, metric in (("count", "portfolio_investments"), ("cost", "portfolio_cost"),
                        ("value", "portfolio_fair_value")):
        if stats[key] is None and (name, metric) in facts:
            f = facts[(name, metric)]
            stats[key] = abs(f["value"])
            stats_src = stats_src or _src(f)
            if not stats["as_of"]:
                stats["as_of"] = quarter_bounds(f["period"])[1].isoformat() \
                    if re.fullmatch(r"20\d{2}-Q[1-4]", f["period"]) else f["period"]
    stats["gain_pct"] = (float(stats["value"] / stats["cost"] - 1) * 100
                         if stats["cost"] and stats["value"] is not None else None)
    if stats_src:
        src["stats"] = stats_src

    invested = _invested(calls, quarters, until)
    # the first call the team entered — a quarter inferred from the roll-forward
    # on file is not the fund's first investment
    entered = [r for r in invested if r["source"] == "entered"]
    first_date = _iso(det.get("initial_investment_date")) or (
        _iso(entered[0]["date"]) if entered else None)
    vintage = det.get("vintage") or (first_date.year if first_date else None)
    if vintage and not det.get("vintage"):
        src["vintage"] = "year of the first investment entered"
    # the accounting identity: balance = funding + equity income − distributions;
    # only from since-inception figures, never from an estimate
    equity_income = (balance - drawn + distributed
                     if None not in (balance, drawn, distributed)
                     and not estimated & {"drawn_to_date", "distribution_to_date"} else None)
    ownership = _d(det.get("ownership_pct"))
    if ownership is None and (name, "ownership_pct") in facts:
        ownership = fact("ownership_pct")
    return {
        "entity": name, "kind": "fund",
        "investment_name": det.get("display_name") or name,
        "investment_type": det.get("investment_type") or "LP",
        "current_round": det.get("current_round") or "-",
        "vintage": int(vintage) if vintage else None,
        "fund_size": fund_size, "commitment": commitment, "future_draw": future,
        "region_sector": det.get("region_sector") or "",
        "investment_balance": balance, "drawn_to_date": drawn,
        "distribution_to_date": distributed, "returned_capital": returned,
        "returned_capital_estimated": returned_estimated,
        "drawn_commit": float(drawn / commitment) if drawn is not None and commitment else None,
        "stats": stats, "invested": invested,
        "accounting": {
            "entity_type": det.get("entity_type") or "Limited Partnership",
            "accounting_method": det.get("accounting_method") or "Equity Method",
            "gl_account": det.get("gl_account") or "", "company_code": det.get("company_code") or "",
            "ownership_pct": ownership, "initial_investment_date": (
                first_date.isoformat() if first_date else None),
            "terms": det.get("terms") or "", "total_commitment": commitment,
            "remaining_commitment": future, "total_funding": drawn,
            "total_equity_income": equity_income, "total_distributions": distributed,
            "investment_balance": balance},
        "latest_quarter": quarters[-1]["period_label"] if quarters else None,
        "details": det, "sources": src, "estimated": sorted(estimated),
        "files": ctx["files"],
        # whole-fund reports only: Lam's share needs Lam's commitment, then a re-read
        "fund_level_ids": whole_fund,
        "removable": not ctx["files"] and not quarters and commitment is None and drawn is None,
        "issues": _fund_issues({"commitment": commitment, "investment_balance": balance,
                                "drawn_to_date": drawn, "distribution_to_date": distributed,
                                "future_draw": future}, estimated, src,
                               mismatches + _reconciles(name, quarters, ctx), waiting or [],
                               name, ctx),
    }


# activity lines of a quarter's roll-forward (beginning + these = ending)
_ROLL_FLOWS = ("funding", "equity_income_loss", "return_of_capital", "dividends", "other",
               "investment_mtm", "sale_of_investment", "net_realized_gain", "accrued_interest",
               "note_conversion", "gain_loss_on_conversion", "acquisition_impact", "impairment")


def _reconciles(name: str, quarters: list[dict], ctx: dict) -> list[dict]:
    """The latest quarter's approved roll-forward must add up: beginning +
    activity = ending. If it doesn't, a balance was misread — flag it."""
    if not quarters:
        return []
    q = quarters[-1]["period_label"]
    vals = ctx["rf_trusted"].get((name, q), {})
    if "beginning_balance" not in vals or "ending_balance" not in vals:
        return []
    begin = Decimal(vals["beginning_balance"]["value"])
    end = Decimal(vals["ending_balance"]["value"])
    activity = sum((Decimal(vals[k]["value"]) for k in _ROLL_FLOWS if k in vals), Decimal(0))
    if abs(begin + activity - end) <= max(Decimal(1), abs(end) * Decimal("0.001")):
        return []
    sid = vals["beginning_balance"]["submission_id"]
    return [{"kind": "mismatch", "submission_id": sid,
             "message": f"{q} doesn't add up: beginning {begin:,.2f} + activity {activity:,.2f} = "
                        f"{begin + activity:,.2f}, but the ending balance is {end:,.2f} — check "
                        f"the beginning and ending balances in {ctx['names'].get(sid, 'the file')}"}]


def _fund_issues(values: dict, estimated: set, src: dict, mismatches: list,
                 waiting: list[dict], name: str = "", ctx: Optional[dict] = None) -> list[dict]:
    """What a person needs to look at for one fund."""
    ctx = ctx or {"files": [], "own_statement": False}
    out = []
    pending = {w["field"] for w in waiting}
    labels = dict(_FUND_FIGURES)
    fund_level = ctx.get("whole_fund", [])
    if fund_level and values.get("commitment") is None:
        out.append({"kind": "commitment", "reread_ids": fund_level,
                    "message": f"{name}'s report{'s' if len(fund_level) > 1 else ''} cover"
                               f"{'' if len(fund_level) > 1 else 's'} the whole fund — Lam's own "
                               "row isn't in it. Enter Lam's commitment to the fund: Lam's share "
                               "is then worked out from the report (it's re-read for you)"})
    missing = [f for f, _l in _FUND_FIGURES if values.get(f) is None and f not in pending]
    if missing:
        plural = len(missing) > 1
        if ctx["files"]:
            out.append({"kind": "missing", "fields": missing, "files": ctx["files"],
                        "message": ", ".join(labels[f] for f in missing) + ": not in what was "
                                   "read from its files — re-read them (files read before an "
                                   "update miss what's read now), or enter "
                                   + ("them" if plural else "it") + " in the fund's details"})
        else:
            out.append({"kind": "missing", "fields": missing,
                        "message": ", ".join(labels[f] for f in missing) + ": no file on record "
                                   "— upload the statement that shows " + ("them" if plural else "it")
                                   + ", or enter " + ("them" if plural else "it")
                                   + " in the fund's details"})
    est = [f for f, _l in _FUND_FIGURES if f in estimated and values.get(f) is not None]
    if est:
        out.append({"kind": "estimate", "fields": est,
                    "message": ", ".join(labels[f] for f in est) + ": estimated from the "
                               "quarters on file — a statement with the since-inception figures "
                               "replaces " + ("them" if len(est) > 1 else "it")
                               + ", or enter the figures"})
    out += [m if isinstance(m, dict) else {"kind": "mismatch", "message": m} for m in mismatches]
    by_file: dict[int, list[str]] = {}
    files: dict[int, str] = {}
    for w in waiting:
        if w["metric"] not in by_file.setdefault(w["submission_id"], []):
            by_file[w["submission_id"]].append(w["metric"])
        files[w["submission_id"]] = w["file"]
    for sid, metrics in by_file.items():
        out.append({"kind": "review", "submission_id": sid,
                    "message": ", ".join(metrics) + f": read from {files[sid] or 'a file'}, "
                               "waiting for review"})
    return out


def _invested(calls: list[dict], quarters: list[dict], until: int) -> list[dict]:
    """Dated calls entered for the entity; each quarter without one shows that
    quarter's funding from the roll-forward."""
    rows = []
    for c in calls:
        d = _iso(c["call_date"])
        if d is None or _order(quarter_label(d)) > until:
            continue
        rows.append({"date": d.isoformat(), "description": c["description"] or "",
                     "amount": Decimal(c["amount"]), "total_raise": _d(c.get("total_raise")),
                     "pre_money": _d(c.get("pre_money")), "source": "entered", "id": c["id"]})
    covered = {quarter_label(_iso(r["date"])) for r in rows}
    for q in quarters:
        funding = Decimal(str(q["funding"] or 0))
        label = q["period_label"]
        if funding > 0 and re.fullmatch(r"20\d{2}-Q[1-4]", label or "") and label not in covered:
            end = quarter_bounds(label)[1]
            rows.append({"date": end.isoformat(),
                         "description": f"Called in Q{label[-1]} {label[:4]} (roll-forward)",
                         "amount": funding, "total_raise": None, "pre_money": None,
                         "source": "roll-forward", "id": None})
    return sorted(rows, key=lambda r: r["date"])


def _company(name: str, det: dict, facts: dict, calls: list[dict], until: int,
             files: Optional[list[dict]] = None) -> dict[str, Any]:
    src: dict[str, str] = {}
    invested = _invested(calls, [], until)
    total = _d(det.get("total_invested"))
    if total is not None:
        src["total_invested"] = "details"
    elif (name, "cost_basis") in facts:
        total = abs(facts[(name, "cost_basis")]["value"])
        src["total_invested"] = _src(facts[(name, "cost_basis")])
    elif invested:
        total = sum((r["amount"] for r in invested), Decimal(0))
        src["total_invested"] = f"{len(invested)} investment(s) entered"
    value = _d(det.get("total_value"))
    if value is None and (name, "fair_value") in facts:
        value = abs(facts[(name, "fair_value")]["value"])
        src["total_value"] = _src(facts[(name, "fair_value")])
    exited = bool(det.get("exit_date"))
    last = det.get("last_investment_date") or (invested[-1]["date"] if invested else None)
    return {
        "entity": name, "kind": "direct_investment",
        "investment_name": det.get("display_name") or name,
        "investment_type": det.get("investment_type") or "Direct",
        "theme": det.get("theme") or "", "current_round": det.get("current_round") or "",
        "security": det.get("security") or det.get("current_round") or "",
        "last_investment_date": last, "total_invested": total,
        "projected_investment": _d(det.get("projected_investment")),
        "total_value": _d(det.get("exit_value")) if exited and det.get("exit_value") else value,
        "operating_status": det.get("operating_status") or ("Inactive" if exited else "Active"),
        "exited": exited,
        "exit": {"date": det.get("exit_date"), "value": _d(det.get("exit_value")),
                 "proceeds": _d(det.get("exit_proceeds")), "escrow": _d(det.get("exit_escrow"))},
        "invested": invested, "details": det, "sources": src,
        "files": files or [],
        "removable": not files and total is None and value is None,
        "issues": ([] if total is not None else [
            {"kind": "missing", "field": "total_invested",
             "message": "Lam's holding record — Total Investment to Date, round, theme, status — "
                        "isn't in the company's own statements; enter it once in its details"}])
        + ([{"kind": "missing", "field": "exit_proceeds",
             "message": "Exit proceeds: enter them in the company's details"}]
           if exited and det.get("exit_proceeds") in (None, "") else []),
    }


def _totals(funds: list[dict], companies: list[dict]) -> dict[str, Any]:
    def s(rows, key):
        return sum((r[key] for r in rows if r.get(key) is not None), Decimal(0))
    drawn, future = s(funds, "drawn_to_date"), s(funds, "future_draw")
    commit, dist = s(funds, "commitment"), s(funds, "distribution_to_date")
    direct = s(companies, "total_invested")
    proceeds = sum((c["exit"]["proceeds"] or 0 for c in companies if c["exited"]), Decimal(0))
    inflow = dist + proceeds
    outflow = -(drawn + direct)
    committed = -future
    return {"fund_drawn": drawn, "fund_future_draw": future, "fund_commitment": commit,
            "fund_distributions": dist, "drawn_commit": float(drawn / commit) if commit else None,
            "direct_invested": direct,
            "direct_active_invested": s([c for c in companies
                                         if c["operating_status"] == "Active"], "total_invested"),
            "direct_projected": s(companies, "projected_investment"),
            "exit_proceeds": proceeds, "cash_inflow": inflow, "cash_outflow": outflow,
            "committed": committed, "net_cash_flow": inflow + outflow + committed}


# --------------------------------------------------------------------------
# Portfolio summary — the team's "dashboard" table: direct investments by
# status, funds, and every investment together, by portfolio group
# --------------------------------------------------------------------------
SUMMARY_COLUMNS = (("count", "#"), ("paid_in", "Investment Cost / Paid-in"),
                   ("value", "Total Value"), ("book", "Book Value"), ("impairment", "Impairment"),
                   ("proceeds", "Proceeds & Distributions"), ("realized", "Realized Gain/(Loss)"),
                   ("unrealized", "Unrealized Gain/(Loss)"), ("total_gain", "Total Gain/(Loss)"),
                   ("gain_pct", "Gain/(Loss) %"), ("tvpi", "TVPI"), ("dpi", "DPI"), ("aum", "AUM"))
_SUM_KEYS = ("paid_in", "value", "book", "impairment", "proceeds", "realized", "unrealized", "aum")
_LIFE_SCIENCES = re.compile(r"life\s*sci|genomic|biotech|pharma|health|medic", re.I)


def _group(row: dict) -> str:
    det = row.get("details") or {}
    if det.get("portfolio_group"):
        return det["portfolio_group"].strip()
    text = " ".join(str(x or "") for x in (row.get("theme"), row.get("region_sector")))
    return "Life Sciences" if _LIFE_SCIENCES.search(text) else "Core"


def _usd(row: dict, as_of: str = "") -> tuple[Optional[Decimal], str, str]:
    """(factor, reason, note): the factor that turns the entity's amounts into
    USD as of a quarter, or (None, reason). The quarter's own rate is used when
    its details list one; else the latest earlier quarter's (noted), else the
    single rate entered for the entity."""
    det = row.get("details") or {}
    cur = (row.get("currency") or "USD").upper()
    rates = _rates(det.get("usd_rates"))
    single = _d(det.get("usd_rate"))
    if as_of in rates:
        return rates[as_of], "", ""
    earlier = sorted((q for q in rates if _order(q) <= _order(as_of)), key=_order) if as_of else []
    if earlier:
        q = earlier[-1]
        return rates[q], "", f"converted at the {q} USD rate — enter {as_of}'s in its details"
    if cur == "USD":
        return single or Decimal(1), "", ""
    if single is None:
        return None, f"reports in {cur} — enter its USD rate in its details to include it", ""
    return single, "", ""


def _company_line(co: dict) -> tuple[Optional[dict], str]:
    det = co["details"] or {}
    cost = co["total_invested"]
    if cost is None:
        return None, "no Total Investment to Date on file — enter it in its details"
    impairment = -abs(_d(det.get("impairment")) or Decimal(0))
    book_entered = _d(det.get("book_value"))
    note = ""
    if co["exited"]:
        status = "exited"
        proceeds = co["exit"]["proceeds"] or Decimal(0)
        value = co["exit"]["value"] if co["exit"]["value"] is not None else (
            proceeds + (co["exit"]["escrow"] or Decimal(0)))
        book = book_entered if book_entered is not None else Decimal(0)
        realized, unrealized, aum = proceeds - cost, value - proceeds, Decimal(0)
    else:
        status = "impaired" if impairment < 0 else "current"
        proceeds = Decimal(0)
        book = book_entered if book_entered is not None else cost + impairment
        value = co["total_value"]
        if value is None:
            value, note = book, "no fair value on file — valued at book"
        realized, unrealized, aum = Decimal(0), value - cost, value
    return {"entity": co["entity"], "name": co["investment_name"], "kind": "direct",
            "status": status, "group": _group(co), "paid_in": cost, "value": value, "book": book,
            "impairment": impairment, "proceeds": proceeds, "realized": realized,
            "unrealized": unrealized, "aum": aum, "estimated": False}, note


def _fund_line(f: dict) -> tuple[Optional[dict], str]:
    paid_in, nav = f["drawn_to_date"], f["investment_balance"]
    if paid_in is None or nav is None:
        return None, "no drawn-to-date or investment balance on file"
    dist = f["distribution_to_date"] or Decimal(0)
    returned = min(f.get("returned_capital") or Decimal(0), dist)
    src = set(f.get("estimated") or [])
    est: set[str] = set()
    notes = []
    if "drawn_to_date" in src:
        est |= {"paid_in", "unrealized"}
    if "distribution_to_date" in src:
        est |= {"value", "proceeds", "realized"}
    if est:
        notes.append("drawn / distributed are estimates from the quarters on file")
    if f.get("returned_capital_estimated") and returned:
        est |= {"realized", "unrealized"}
        notes.append("realized gain takes out the return of capital of the quarters on file "
                     "only — enter Returned capital to date in its details")
    return {"entity": f["entity"], "name": f["investment_name"], "kind": "fund", "status": "fund",
            "group": _group(f), "paid_in": paid_in, "value": nav + dist, "book": nav,
            "impairment": Decimal(0), "proceeds": dist, "realized": dist - returned,
            "unrealized": nav - (paid_in - returned), "aum": nav, "estimated": bool(est),
            "estimated_keys": sorted(est)}, "; ".join(notes)


def _aggregate(label: str, lines: list[dict], kind: str) -> dict:
    out: dict[str, Any] = {"label": label, "kind": kind, "count": len(lines)}
    for k in _SUM_KEYS:
        out[k] = sum((ln[k] for ln in lines), Decimal(0))
    out["total_gain"] = out["realized"] + out["unrealized"]
    paid = out["paid_in"]
    out["gain_pct"] = float(out["total_gain"] / paid * 100) if paid else None
    out["tvpi"] = float(out["value"] / paid) if paid else None
    out["dpi"] = float(out["proceeds"] / paid) if paid else None
    # which cells rest on an estimate (the quarters on file, not a since-inception figure)
    est = {k for ln in lines for k in ln.get("estimated_keys", [])}
    if est & {"paid_in", "proceeds"}:
        est |= {"total_gain", "gain_pct", "dpi"}
    if est & {"paid_in", "value"}:
        est.add("tvpi")
    out["estimated"] = sorted(est)
    return out


def summary(as_of: Optional[str] = None, rep: Optional[dict] = None) -> dict[str, Any]:
    """Every investment's cost, value, book value, impairment, proceeds and
    gains — direct investments by status (current / impaired / exited), funds,
    and all of them together by portfolio group — with TVPI, DPI and AUM.

      direct, held      unrealized = value − cost; AUM = value
      direct, exited    realized = proceeds − cost; unrealized = value − proceeds
      fund              value = NAV + distributions; realized = distributions −
                        returned capital; unrealized = NAV − (paid-in − returned
                        capital); book value and AUM = NAV
      every row         total gain = realized + unrealized; gain % = total ÷ cost;
                        TVPI = value ÷ cost; DPI = proceeds ÷ cost
    Amounts are in USD: an entity reporting in another currency needs its USD
    rate (per quarter, or one for all), and is left out (with a note) until it
    has one. Rows carry `estimated`: the cells resting on the quarters on file. Pass `rep` (a
    report() result) to summarise it without reading everything again."""
    rep = rep or report(as_of)
    lines: list[dict] = []
    notes: list[dict] = []
    for row, maker in ([(c, _company_line) for c in rep["companies"]]
                       + [(f, _fund_line) for f in rep["funds"]]):
        line, note = maker(row)
        if line is None:
            notes.append({"entity": row["entity"], "name": row["investment_name"], "kind": row["kind"],
                          "message": f"left out: {note}"})
            continue
        factor, why, fx_note = _usd(row, rep["as_of"])
        if factor is None:
            notes.append({"entity": row["entity"], "name": row["investment_name"], "kind": row["kind"],
                          "message": f"left out: {why}"})
            continue
        note = "; ".join(n for n in (note, fx_note) if n)
        for k in _SUM_KEYS:
            line[k] = line[k] * factor
        line["currency"] = row.get("currency") or "USD"
        lines.append(line)
        if note:
            notes.append({"entity": row["entity"], "name": row["investment_name"], "kind": row["kind"], "message": note})
    direct = [ln for ln in lines if ln["kind"] == "direct"]
    funds = [ln for ln in lines if ln["kind"] == "fund"]
    rows = [_aggregate(label, [ln for ln in direct if ln["status"] == st], "status")
            for st, label in (("current", "Current Direct Investments"),
                              ("impaired", "Impaired Direct Investments"),
                              ("exited", "Exited Direct Investments"))]
    rows.append(_aggregate("Total Direct Investments", direct, "total"))
    rows.append(_aggregate("Fund Investments", funds, "fund"))
    groups = sorted({ln["group"] for ln in lines}, key=lambda g: (g != "Core", g))
    totals = [_aggregate("Total Investments", lines, "total")] + [
        _aggregate(f"{g} Investments", [ln for ln in lines if ln["group"] == g], "group")
        for g in groups]
    return {"as_of": rep["as_of"], "as_of_label": rep["as_of_label"], "quarters": rep["quarters"],
            "columns": [{"key": k, "label": l} for k, l in SUMMARY_COLUMNS],
            "rows": rows, "totals": totals, "lines": lines, "notes": notes}


# --------------------------------------------------------------------------
# Workbook export — Lam's layout
# --------------------------------------------------------------------------
_M = Decimal(1_000_000)
_FMT_M3 = '"$"#,##0.000'
_FMT_M6 = '"$"#,##0.000000'
_FMT_ACCT2 = '_("$"* #,##0.00_);_("$"* (#,##0.00);_("$"* "-"??_);_(@_)'
_FMT_SUM_M = '"$"#,##0.0"M"'
_FMT_SUM = '"$"#,##0.0_);("$"#,##0.0)'
_FMT_PLAIN = '_(* #,##0.0_);_(* (#,##0.0);_(* "-"??_);_(@_)'
_FMT_TOTAL = '"$"#,##0.0"M";-"$"#,##0.0"M"'
_FMT_USD = '#,##0.00_);(#,##0.00)'


def _m(v: Optional[Decimal]) -> Optional[float]:
    return None if v is None else float(v / _M)


def _date(v) -> Optional[date]:
    return _iso(v)


def _sheet_names(rows: list[dict]) -> dict[str, str]:
    used: set[str] = {"summary-cash flows"}
    out = {}
    for r in rows:
        base = re.sub(r"[\[\]:*?/\\]", " ", r["entity"]).strip()[:31] or "Sheet"
        title, n = base, 2
        while title.lower() in used:
            title = f"{base[:28]} {n}"
            n += 1
        used.add(title.lower())
        out[r["entity"]] = title
    return out


def _ref(sheet: str, cell: str) -> str:
    return "'" + sheet.replace("'", "''") + "'!" + cell


def workbook(as_of: Optional[str] = None) -> tuple[bytes, str]:
    """(xlsx bytes, file name) — "Investment Portfolio Jun'26.xlsx"."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    rep = report(as_of)
    bold = Font(bold=True)
    head = Font(bold=True, underline="single")
    key_fill = PatternFill("solid", fgColor="FFFF00")     # Lam marks quarterly updates in yellow
    bal_fill = PatternFill("solid", fgColor="BDD7EE")
    box = Border(*(Side(style="thin"),) * 4)
    wb = Workbook()
    cash_flows = wb.active
    cash_flows.title = "Summary-Cash Flows"
    names = _sheet_names(rep["funds"] + rep["companies"])

    # ---- one sheet per fund ----
    for f in rep["funds"]:
        ws = wb.create_sheet(names[f["entity"]])
        rows = [(2, "Investment Name", f["investment_name"], None, True),
                (3, "Investment Type", f["investment_type"], None, True),
                (4, "Current / Most Recent Round", f["current_round"], None, False),
                (5, "Vintage", f["vintage"], None, False),
                (6, "Fund Size", _m(f["fund_size"]), _FMT_M3, False),
                (7, "Commitment", _m(f["commitment"]), _FMT_M3, False),
                (9, "Future Draw", _m(f["future_draw"]), _FMT_M3, True),
                (10, "Region / Sector Focus", f["region_sector"], None, False),
                (11, "Investment Balance", _m(f["investment_balance"]), _FMT_M6, True),
                (13, "Drawn to Date", _m(f["drawn_to_date"]), None, False)]
        for r, label, value, fmt, key in rows:
            ws.cell(r, 1, label)
            c = ws.cell(r, 2, value)
            if fmt:
                c.number_format = fmt
            if key:
                c.fill = bal_fill if label == "Investment Balance" else key_fill
            c.alignment = Alignment(horizontal="right")
        ws.cell(13, 5, "Distribution to Date")
        dist = _m(f["distribution_to_date"])
        ws.cell(13, 6, -dist if dist else dist).number_format = _FMT_ACCT2
        for field, cell in (("drawn_to_date", "B13"), ("future_draw", "B9"),
                            ("distribution_to_date", "F13"), ("investment_balance", "B11")):
            if f["sources"].get(field) == "entered":
                ws[cell].comment = Comment("Entered by hand in Portfolio Financials Studio "
                                           "(no document on file showed it).", "PFS")
            elif field in f["estimated"]:
                ws[cell].comment = Comment(
                    f"Estimate: {f['sources'][field]}. A statement showing the since-inception "
                    "figure replaces it; or enter it in Portfolio Financials Studio.", "PFS")
        for c, h in zip("BCDEF", ("Date", "# of investments", "Cost Basis", "Value",
                                  "Gain / (Loss) %")):
            ws[f"{c}14"] = h
            ws[f"{c}14"].font = head
            ws[f"{c}14"].alignment = Alignment(horizontal="center")
        st = f["stats"]
        ws["A15"] = "Latest Stats"
        ws["A15"].font = bold
        ws["B15"] = _date(st["as_of"])
        ws["B15"].number_format = "m/d/yyyy"
        ws["C15"] = int(st["count"]) if st["count"] is not None else None
        ws["D15"] = _m(st["cost"])
        ws["E15"] = _m(st["value"])
        ws["F15"] = st["gain_pct"] / 100 if st["gain_pct"] is not None else None
        ws["D15"].number_format = ws["E15"].number_format = _FMT_M3
        ws["F15"].number_format = "0.0%"
        for c in "ABCDEF":
            ws[f"{c}15"].border = box
            if c != "A":
                ws[f"{c}15"].fill = bal_fill
        for c, h in zip("BCDEF", ("Date", "Current Round", "Amount Invested", "Total Raise",
                                  "Pre Money Valuation")):
            ws[f"{c}16"] = h
            ws[f"{c}16"].font = head
            ws[f"{c}16"].alignment = Alignment(horizontal="center")
        _invested_rows(ws, f["invested"], 17, key_fill)
        # the accounting team's Equity Investments Summary, alongside
        a = f["accounting"]
        ws["H2"] = "Equity Investments Summary"
        ws["H2"].font = bold
        acct = [("Entity Type", a["entity_type"], None),
                ("Method of Accounting", a["accounting_method"], None),
                ("GL Account", a["gl_account"], None), ("Company Code", a["company_code"], None),
                ("% Ownership", float(a["ownership_pct"]) / 100
                 if a["ownership_pct"] is not None else None, "0.00%"),
                ("Initial Investment Date", _date(a["initial_investment_date"]), "m/d/yyyy"),
                ("Terms", a["terms"], None),
                ("Total Commitment", _f(a["total_commitment"]), _FMT_USD),
                ("Remaining Commitment", _f(a["remaining_commitment"]), _FMT_USD),
                ("Total Funding", _f(a["total_funding"]), _FMT_USD),
                ("Total Equity Income/(Loss)", _f(a["total_equity_income"]), _FMT_USD),
                ("Total Distributions", -_f(a["total_distributions"])
                 if a["total_distributions"] else _f(a["total_distributions"]), _FMT_USD),
                ("Investment Balance", _f(a["investment_balance"]), _FMT_USD)]
        est = {"Remaining Commitment": "future_draw", "Total Funding": "drawn_to_date",
               "Total Distributions": "distribution_to_date"}
        for i, (label, value, fmt) in enumerate(acct, start=3):
            ws.cell(i, 8, label).alignment = Alignment(horizontal="right")
            c = ws.cell(i, 9, value)
            if fmt:
                c.number_format = fmt
            if est.get(label) in f["estimated"]:
                c.comment = Comment(f"Estimate: {f['sources'][est[label]]}.", "PFS")
        ws.cell(15, 9).fill = bal_fill
        ws.column_dimensions["A"].width = 28
        ws.column_dimensions["B"].width = 16
        for c in "CDEF":
            ws.column_dimensions[c].width = 18
        ws.column_dimensions["H"].width = 28
        ws.column_dimensions["I"].width = 18

    # ---- one sheet per direct investment ----
    for co in rep["companies"]:
        ws = wb.create_sheet(names[co["entity"]])
        rows = [(2, "Investment Name", co["investment_name"], None),
                (3, "Investment Type", co["investment_type"], None),
                (4, "Current / Most Recent Round", co["current_round"], None),
                (5, "Most Recent Investment Date", _date(co["last_investment_date"]), "mmm yyyy"),
                (6, "Total Investment to Date", _m(co["total_invested"]), _FMT_M3),
                (7, "Projected Future Investments", _m(co["projected_investment"]), _FMT_M3),
                (9, "Investments", co["security"], None),
                (10, "Theme", co["theme"], None),
                (11, "Total Value", _m(co["total_value"]), _FMT_M3),
                (12, "Operating Status", co["operating_status"], None)]
        if co["exited"]:
            rows += [(13, "Date of Exit", _date(co["exit"]["date"]), "m/d/yyyy")]
        for r, label, value, fmt in rows:
            ws.cell(r, 1, label)
            c = ws.cell(r, 2, value)
            c.alignment = Alignment(horizontal="right")
            if fmt:
                c.number_format = fmt
        if co["exited"]:
            ws.cell(13, 5, "Total Received Proceeds")
            ws.cell(13, 6, _m(co["exit"]["proceeds"])).number_format = _FMT_M3
            ws.cell(14, 5, "Total Proceeds on Escrow")
            ws.cell(14, 6, _m(co["exit"]["escrow"])).number_format = _FMT_M3
        for c, h in zip("BCDEF", ("Date", "Current Round", "Amount Invested", "Total Raise",
                                  "Pre Money Valuation")):
            ws[f"{c}16"] = h
            ws[f"{c}16"].font = head
        _invested_rows(ws, co["invested"], 17, None)
        ws.column_dimensions["A"].width = 28
        for c in "BCDEF":
            ws.column_dimensions[c].width = 18

    _summary(cash_flows, rep, names)
    _dashboard(wb, summary(rep=rep))
    wb.calculation.fullCalcOnLoad = True          # Excel computes the summary's references
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"Investment Portfolio {rep['as_of_label']}.xlsx"


def _f(v: Optional[Decimal]) -> Optional[float]:
    return None if v is None else float(v)


def _dashboard(wb, summ: dict) -> None:
    """The portfolio summary as a sheet: direct investments by status and funds,
    then all investments by portfolio group (in $M, USD)."""
    from openpyxl.comments import Comment
    from openpyxl.styles import Font
    ws = wb.create_sheet("Dashboard", 1)
    bold = Font(bold=True)
    money, pct, ratio = '"$"#,##0.0"M";-"$"#,##0.0"M"', "0.0%", '0.00"x"'
    ws.cell(1, 1, f"Portfolio Summary — as of {summ['as_of_label']} (USD)").font = Font(bold=True, size=12)
    cols = [c for c in summ["columns"]]

    def table(r: int, rows: list[dict], keys: list[str]) -> int:
        ws.cell(r, 1, "")
        for j, k in enumerate(keys, start=2):
            ws.cell(r, j, next(c["label"] for c in cols if c["key"] == k)).font = bold
        for row in rows:
            r += 1
            ws.cell(r, 1, row["label"]).font = bold if row["kind"] == "total" else Font()
            for j, k in enumerate(keys, start=2):
                v = row.get(k)
                c = ws.cell(r, j)
                if v is None:
                    continue
                if k == "count":
                    c.value = int(v)
                elif k == "gain_pct":
                    c.value, c.number_format = v / 100, pct
                elif k in ("tvpi", "dpi"):
                    c.value, c.number_format = v, ratio
                else:
                    c.value, c.number_format = float(v) / 1e6, money
                if row["kind"] == "total":
                    c.font = bold
                if k in row.get("estimated", []):
                    c.comment = Comment("Includes an estimate from the quarters on file — see "
                                        "the notes below.", "PFS")
        return r

    first = [k for k, _l in SUMMARY_COLUMNS if k not in ("dpi", "aum")]
    r = table(3, summ["rows"], first)
    r = table(r + 2, summ["totals"], [k for k, _l in SUMMARY_COLUMNS])
    for n in summ["notes"]:
        r += 1
        ws.cell(r + 1, 1, f"{n['name']}: {n['message']}")
    ws.column_dimensions["A"].width = 32
    for col in "BCDEFGHIJKLMN":
        ws.column_dimensions[col].width = 16


def _invested_rows(ws, rows: list[dict], start: int, fill) -> None:
    for i, r in enumerate(rows, start=start):
        ws.cell(i, 1, "Invested")
        ws.cell(i, 2, _date(r["date"])).number_format = "m/d/yyyy"
        ws.cell(i, 3, r["description"] or "-")
        ws.cell(i, 4, _m(r["amount"])).number_format = '"$"#,##0.000'
        if r.get("total_raise") is not None:
            ws.cell(i, 5, _m(r["total_raise"])).number_format = _FMT_M3
        if r.get("pre_money") is not None:
            ws.cell(i, 6, _m(r["pre_money"])).number_format = _FMT_M3
        if fill is not None:
            for c in range(2, 7):
                ws.cell(i, c).fill = fill


def _summary(ws, rep: dict, names: dict[str, str]) -> None:
    """Lam's "Summary-Cash Flows": every figure a reference into the entity sheets."""
    from openpyxl.styles import Font
    bold = Font(bold=True)
    r = 3
    ws.cell(r, 1, "Cell References")
    for c, ref in enumerate(("$B$2", "$B$3", "$B$10", "$B$13", "$B$9", "$B$7", "$F$13"), start=3):
        ws.cell(r, c, ref)
    ws.cell(r + 1, 1, "Worksheet Reference")
    for c, h in enumerate(("Fund", "Type", "Region / Sector", "Drawn to date", "Future Draw",
                           "Total Commitment", "Distribution", "Drawn/Commit"), start=3):
        ws.cell(r + 1, c, h).font = bold
    first = r + 2
    r = first
    for f in rep["funds"]:
        sh = names[f["entity"]]
        ws.cell(r, 1, sh)
        for c, cell in ((3, "$B$2"), (4, "$B$3"), (5, "$B$10"), (6, "$B$13"), (7, "$B$9"),
                        (8, "$B$7")):
            ws.cell(r, c, f"={_ref(sh, cell)}")
        ws.cell(r, 9, f"=-{_ref(sh, '$F$13')}")     # distributions shown as cash received
        ws.cell(r, 10, f'=IFERROR(F{r}/H{r},"")')
        for c in (6, 7):
            ws.cell(r, c).number_format = _FMT_SUM_M
        for c in (8, 9):
            ws.cell(r, c).number_format = _FMT_SUM
        ws.cell(r, 10).number_format = "0%"
        r += 1
    fund_total = r
    ws.cell(r, 3, "Total Fund Investments").font = bold
    for c, col in ((6, "F"), (7, "G"), (8, "H"), (9, "I")):
        ws.cell(r, c, f"=SUM({col}{first}:{col}{r - 1})" if r > first else 0)
        ws.cell(r, c).number_format = _FMT_SUM_M
        ws.cell(r, c).font = bold
    ws.cell(r, 10, f'=IFERROR(F{r}/H{r},"")').number_format = "0%"

    # exited direct investments
    r += 4
    ws.cell(r, 1, "Cell References")
    for c, ref in ((3, "$B$2"), (4, "$B$9"), (6, "$B$6"), (7, "$B$11")):
        ws.cell(r, c, ref)
    ws.cell(r + 1, 1, "Worksheet Reference")
    for c, h in enumerate(("Company", "Investments", "Date of Exit", "Cost", "Total Value",
                           "Total Gain/(Loss)", "Total Received Proceeds",
                           "Total Proceeds on Escrow", "Total Gain/(Loss)%",
                           "Total Cash Inflow"), start=3):
        ws.cell(r + 1, c, h).font = bold
    r += 2
    ex_first = r
    for co in [c for c in rep["companies"] if c["exited"]]:
        sh = names[co["entity"]]
        ws.cell(r, 1, sh)
        ws.cell(r, 3, f"={_ref(sh, '$B$2')}")
        ws.cell(r, 4, f"={_ref(sh, '$B$9')}")
        ws.cell(r, 5, _date(co["exit"]["date"])).number_format = "m/d/yyyy"
        ws.cell(r, 6, f"={_ref(sh, '$B$6')}")
        ws.cell(r, 7, f"={_ref(sh, '$B$11')}")
        ws.cell(r, 8, f"=G{r}-F{r}")
        ws.cell(r, 9, _m(co["exit"]["proceeds"]))
        ws.cell(r, 10, _m(co["exit"]["escrow"]))
        ws.cell(r, 11, f'=IFERROR(H{r}/F{r},"")').number_format = "0%"
        ws.cell(r, 12, f"=I{r}")
        for c in (6, 7, 8, 9, 10, 12):
            ws.cell(r, c).number_format = _FMT_SUM
        r += 1
    ex_total = r
    ws.cell(r, 3, "Total Exited Investments").font = bold
    for c, col in ((6, "F"), (7, "G"), (8, "H"), (9, "I"), (10, "J"), (12, "L")):
        ws.cell(r, c, f"=SUM({col}{ex_first}:{col}{r - 1})" if r > ex_first else 0)
        ws.cell(r, c).number_format = _FMT_SUM
    ws.cell(r, 11, f'=IFERROR(H{r}/F{r},"")').number_format = "0%"

    # every direct investment
    r += 6
    ws.cell(r, 1, "Cell References")
    for c, ref in enumerate(("$B$2", "$B$10", "$B$4", "$B$5", "$B$6", "$B$7"), start=3):
        ws.cell(r, c, ref)
    ws.cell(r + 1, 1, "Worksheet Reference")
    for c, h in enumerate(("Company", "Theme", "Current / Most Recent Round",
                           "Most Recent Investment Date", "Total Investment to date",
                           "Projected future investments", "Operating Status"), start=3):
        ws.cell(r + 1, c, h).font = bold
    r += 2
    di_first = r
    for co in rep["companies"]:
        sh = names[co["entity"]]
        ws.cell(r, 1, sh)
        for c, cell in ((3, "$B$2"), (4, "$B$10"), (5, "$B$4"), (6, "$B$5"), (7, "$B$6"),
                        (8, "$B$7")):
            ws.cell(r, c, f"={_ref(sh, cell)}")
        ws.cell(r, 6).number_format = "mmm yyyy"
        ws.cell(r, 7).number_format = ws.cell(r, 8).number_format = _FMT_PLAIN
        ws.cell(r, 9, co["operating_status"])
        r += 1
    di_total = r
    ws.cell(r, 3, "Total Direct Investments").font = bold
    for c, col in ((7, "G"), (8, "H")):
        ws.cell(r, c, f"=SUM({col}{di_first}:{col}{r - 1})" if r > di_first else 0)
        ws.cell(r, c).number_format = _FMT_SUM
    ws.cell(r + 1, 3, "Total Active Direct Investments").font = bold
    ws.cell(r + 1, 7, f'=SUMIF(I{di_first}:I{max(r - 1, di_first)},"Active",'
                      f'G{di_first}:G{max(r - 1, di_first)})' if r > di_first else 0)
    ws.cell(r + 1, 7).number_format = _FMT_SUM

    # totals
    r += 3
    ws.cell(r, 3, "Totals").font = bold
    lines = (("Cash inflow", f"=I{fund_total}+I{ex_total}"),
             ("Actual cash outflow", f"=-(F{fund_total}+G{di_total})"),
             ("Committed investments", f"=-G{fund_total}"),
             ("Total net cash flow", f"=SUM(F{r + 2}:F{r + 4})"))
    for i, (label, formula) in enumerate(lines, start=r + 2):
        ws.cell(i, 3, label)
        ws.cell(i, 6, formula).number_format = _FMT_TOTAL
    ws.cell(r + 5, 3).font = bold
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["C"].width = 30
    for c in "DEFGHIJKL":
        ws.column_dimensions[c].width = 16


# --------------------------------------------------------------------------
# Holdings sheet — every direct investment's holding record in one file
# --------------------------------------------------------------------------
# A company's own statements never show what the stake cost or is worth, so
# these come from the team's records: one row per company, one column per field.
_HOLDING_ALIASES = {
    "company": "name", "name": "name", "investment": "name", "portfolio company": "name",
    "entity": "name", "cost": "total_invested", "investment cost": "total_invested",
    "cost basis": "total_invested", "total invested": "total_invested",
    "fair value": "total_value", "fmv": "total_value", "value": "total_value",
    "latest fmv": "total_value", "round": "current_round", "current round": "current_round",
    "status": "operating_status", "group": "portfolio_group", "portfolio": "portfolio_group",
    "impairment": "impairment", "book value": "book_value", "type": "investment_type",
    "last investment date": "last_investment_date", "projected": "projected_investment",
    "exit date": "exit_date", "exit value": "exit_value", "exit proceeds": "exit_proceeds",
    "proceeds": "exit_proceeds", "escrow": "exit_escrow", "usd rate": "usd_rate",
    "security": "security", "display name": "display_name",
}
_IN_MILLIONS = re.compile(r"\(\s*\$?\s*(?:m|mm|mn|millions?)\s*\)|\$\s*(?:m|mm)\b|in millions", re.I)
_IN_THOUSANDS = re.compile(r"\$\s*000|\(\s*\$?\s*(?:k|000s?|thousands?)\s*\)|\$\s*k\b|in thousands", re.I)


def _holding_key(text: str) -> str:
    t = _IN_MILLIONS.sub("", _IN_THOUSANDS.sub("", str(text or "")))
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _holding_columns() -> dict[str, str]:
    cols = dict(_HOLDING_ALIASES)
    for key, label, _t in COMPANY_FIELDS:
        cols[_holding_key(label)] = key
        cols[_holding_key(key)] = key
    cols[_holding_key("Investment Name")] = "display_name"
    return cols


def _sheet_rows(data: bytes, filename: str) -> list[list[Any]]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True).worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    import csv
    text = data.decode("utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text))]


def import_holdings(data: bytes, filename: str, user: str = "local") -> dict[str, Any]:
    """Fill the direct investments' holding records from one sheet (xlsx or csv):
    a "Company" column plus any of the holding fields — cost, value, round,
    dates, impairment, book value, group, status, exit. A column headed "($M)"
    or "($000)" is scaled; an empty cell leaves what is on file. A row that
    can't be read is reported and skipped whole; nothing is half-applied."""
    known = _holding_columns()
    types = {k: t for k, _l, t in COMPANY_FIELDS}
    labels = {k: l for k, l, _t in COMPANY_FIELDS}
    rows = [r for r in _sheet_rows(data, filename) if any(c not in (None, "") for c in r)]
    head = next((i for i, r in enumerate(rows[:15])
                 if any(known.get(_holding_key(c)) == "name" for c in r)), None)
    if head is None:
        raise ValueError('no "Company" column found — the first row should name the columns '
                         "(download the holdings sheet for the layout)")
    columns: dict[int, tuple[str, Decimal]] = {}
    ignored: list[str] = []
    for i, cell in enumerate(rows[head]):
        if cell in (None, ""):
            continue
        key = known.get(_holding_key(cell))
        if key is None or (key in columns_keys(columns)):
            ignored.append(str(cell))
            continue
        scale = (Decimal(1_000_000) if _IN_MILLIONS.search(str(cell)) else
                 Decimal(1000) if _IN_THOUSANDS.search(str(cell)) else Decimal(1))
        columns[i] = (key, scale)
    existing = {**{e.lower(): e for e in db.all_details()},
                **{str(d.get("display_name", "")).lower(): e
                   for e, d in db.all_details().items() if d.get("display_name")}}
    for f in db.get_facts():
        existing.setdefault(str(f["entity_name"]).lower(), f["entity_name"])
    done, errors = [], []
    for n, row in enumerate(rows[head + 1:], start=head + 2):
        body: dict[str, Any] = {}
        name = ""
        for i, (key, scale) in columns.items():
            cell = row[i] if i < len(row) else None
            if cell in (None, ""):
                continue
            if hasattr(cell, "isoformat"):
                cell = cell.isoformat()[:10]
            text = str(cell).strip()
            if key == "name":
                name = text
                continue
            if types.get(key) == "money" and scale != 1:
                v = parse_number(text)[0]
                if v is not None and re.fullmatch(r"[\s$€£(),.\-\d]+", text):
                    text = str(v * scale)
            body[key] = text
        if not name or re.match(r"total\b", name, re.I):
            continue
        try:
            values = coerce_details(body)
        except ValueError as e:
            message = re.sub(r"^\w+", lambda m: labels.get(m.group(0), m.group(0)), str(e))
            errors.append({"row": n, "company": name, "message": message})
            continue
        entity = existing.get(name.lower(), name)
        db.set_details(entity, {**values, "kind": "direct_investment"}, user=user)
        done.append({"company": entity, "fields": sorted(values)})
    return {"updated": len(done), "rows": done, "errors": errors, "ignored_columns": ignored}


def columns_keys(columns: dict[int, tuple[str, Decimal]]) -> set[str]:
    return {k for k, _s in columns.values()}


def holdings_csv() -> str:
    """The holdings sheet as it stands — every direct investment on the report
    with its holding record — to fill in and import back."""
    import csv
    fields = [(k, l) for k, l, _t in COMPANY_FIELDS if k != "display_name"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Company", "Investment Name"] + [label for _k, label in fields])
    for co in report()["companies"]:
        det = co["details"] or {}
        w.writerow([co["entity"], det.get("display_name", "")]
                   + [det.get(k, "") for k, _l in fields])
    return buf.getvalue()
