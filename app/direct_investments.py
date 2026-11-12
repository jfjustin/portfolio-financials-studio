"""Lam Capital's quarterly "Direct Investment Financials" report.

Per portfolio company, for each quarter of a calendar year and the full year:
Budget | Actual for

    Revenue · Operating Cash Flow · CapEx · Ending Cash · Operating Expense
    Gross Monthly Burn · Gross Runway · Net Monthly Burn · Net Runway

Actuals are published (approved) values only: the five figures read from each
company's own P&L, balance sheet and cash-flow statement. Burn and runway are
computed with Lam's definitions, never read:

    Gross Monthly Burn = Operating Expense ÷ months in the period
    Net Monthly Burn   = −(Operating Cash Flow + CapEx) ÷ months in the period
    Runway             = Ending Cash ÷ monthly burn, in whole months

Budgets are entered by the team (db.budgets). The workbook export reproduces
Lam's layout: one sheet per company, "<Company>(in $000)", Budget | Actual
column pairs for Q1'26 … Q4'26 and 2026, figures in $000.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from sqlalchemy import select

from . import db
from .extractors.base import quarter_label

BASE = ("revenue", "operating_cash_flow", "capex", "ending_cash", "operating_expense")
ROWS = (("revenue", "Revenue"), ("operating_cash_flow", "Operating Cash Flow"),
        ("capex", "CapEx"), ("ending_cash", "Ending Cash"),
        ("operating_expense", "Operating Expense"),
        ("gross_monthly_burn", "Gross Monthly Burn"), ("gross_runway", "Gross Runway"),
        ("net_monthly_burn", "Net Monthly Burn"), ("net_runway", "Net Runway"))
RUNWAYS = ("gross_runway", "net_runway")
_FLOWS = ("revenue", "operating_cash_flow", "capex", "operating_expense")
# what a finished report printed, kept to check our computation against
_REPORTED = {"gross_monthly_burn": "gross_monthly_burn", "net_monthly_burn": "net_monthly_burn",
             "gross_runway_months": "gross_runway", "net_runway_months": "net_runway"}


def derive(m: dict[str, Optional[Decimal]], months: Optional[int]) -> dict[str, Any]:
    """Burn and runway from the five figures (Lam's definitions)."""
    opex, ocf, capex, cash = (m.get(k) for k in ("operating_expense", "operating_cash_flow",
                                                  "capex", "ending_cash"))
    gross = opex / months if opex is not None and months else None
    net = -(ocf + (capex or 0)) / months if ocf is not None and months else None
    return {"gross_monthly_burn": gross, "net_monthly_burn": net,
            "gross_runway": _runway(cash, gross), "net_runway": _runway(cash, net)}


def _runway(cash: Optional[Decimal], burn: Optional[Decimal]) -> Optional[int]:
    """Whole months of cash at that burn; None when nothing burns (cash-flow positive)."""
    if cash is None or burn is None or burn <= 0:
        return None
    return int((cash / burn).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _quarter_of(period: str) -> Optional[str]:
    """Company figures are kept per calendar quarter; files read before that
    carry their statement date instead ("2026-03-31")."""
    if re.fullmatch(r"20\d{2}-Q[1-4]", period or ""):
        return period
    m = re.fullmatch(r"(20\d{2})-(\d{2})-(\d{2})", period or "")
    if m:
        try:
            return quarter_label(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            return None
    return None


def _filenames() -> dict[int, str]:
    with db.get_engine().connect() as conn:
        return {r.id: r.filename for r in conn.execute(
            select(db.submissions.c.id, db.submissions.c.filename))}


def _actuals() -> tuple[dict[tuple[str, str], dict[str, dict]], dict, dict]:
    """Published company figures {(company, quarter): {metric: detail}} — the
    newest value wins, a reviewer-confirmed one over an auto-accepted one —
    plus the figures still waiting for review {(company, quarter): {metric:
    submission}} and approved values that disagree with the one used."""
    names = _filenames()
    keep = set(BASE) | set(_REPORTED)
    rows = [f for f in db.trusted_facts()
            if f["entity_kind"] == "direct_investment" and f["metric_key"] in keep]
    rows.sort(key=lambda f: (f["status"] == "confirmed", f["id"]))
    out: dict[tuple[str, str], dict[str, dict]] = {}
    seen: dict[tuple[str, str, str], list[dict]] = {}
    for f in rows:
        q = _quarter_of(f["period"])
        if not q:
            continue
        d = {"value": Decimal(f["value"]), "status": f["status"], "fact_id": f["id"],
             "submission_id": f["submission_id"], "file": names.get(f["submission_id"], ""),
             "line": f["raw_label"] or "", "currency": f["currency"] or "USD",
             "manual": "manual_entry" in json.loads(f["flags"] or "[]"),
             "breakdown": {m: Decimal(v) for m, v in json.loads(f.get("breakdown") or "{}").items()}}
        out.setdefault((f["entity_name"], q), {})[f["metric_key"]] = d
        seen.setdefault((f["entity_name"], q, f["metric_key"]), []).append(d)
    conflicts: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for (company, q, metric), found in seen.items():
        used = out[(company, q)][metric]
        tol = max(Decimal(1), abs(used["value"]) * Decimal("0.001"))
        others = [d for d in found if d is not used and abs(d["value"] - used["value"]) > tol]
        if others and metric in BASE:
            conflicts.setdefault((company, q), {})[metric] = others
    pending: dict[tuple[str, str], dict[str, int]] = {}
    for f in db.unapproved_facts():
        q = _quarter_of(f["period"] or "")
        if q and f["entity_kind"] == "direct_investment" and f["metric_key"] in BASE:
            pending.setdefault((f["entity_name"], q), {})[f["metric_key"]] = f["submission_id"]
    return out, pending, conflicts


def _issues(act: dict, detail: dict, pending: dict[str, int], conflicts: dict[str, list],
            checks: list[str]) -> list[dict]:
    """What a person needs to look at for one company and quarter."""
    labels = dict(ROWS)
    out = []
    by_file: dict[int, list[str]] = {}
    for k in BASE:
        if k not in act and k in pending:
            by_file.setdefault(pending[k], []).append(k)
    for sid, keys in by_file.items():
        out.append({"kind": "review", "metrics": keys, "submission_id": sid,
                    "message": ", ".join(labels[k] for k in keys)
                               + ": read from a file, waiting for review"})
    missing = [k for k in BASE if k not in act and k not in pending]
    if missing:
        out.append({"kind": "missing", "metrics": missing,
                    "message": ", ".join(labels[k] for k in missing)
                               + ": in no approved file — upload the statement that shows "
                               + ("them" if len(missing) > 1 else "it")
                               + ", or type the figure into the table"})
    for k, others in conflicts.items():
        used = detail[k]
        also = "; ".join(f"{o['value']:,.2f} ({o['file'] or 'entered'})" for o in others)
        out.append({"kind": "mismatch", "metric": k, "submission_id": used["submission_id"],
                    "message": f"{labels[k]}: {used['value']:,.2f} is used "
                               f"({used['file'] or 'entered'}), but {also} is also approved "
                               "— check which is right and reject the other"})
    out += [{"kind": "check", "message": m} for m in checks]
    return out


def _year_total(parts: list[dict[str, Optional[Decimal]]]) -> dict[str, Optional[Decimal]]:
    """The year so far: flows added up over the quarters (only when every one of
    them has the figure), cash as of the latest quarter."""
    if not parts:
        return {}
    out: dict[str, Optional[Decimal]] = {}
    for k in _FLOWS:
        vals = [p.get(k) for p in parts]
        out[k] = sum(vals, Decimal(0)) if all(v is not None for v in vals) else None
    out["ending_cash"] = parts[-1].get("ending_cash")
    return out


def _month_bounds(p: str) -> tuple[str, str]:
    """A quarter ("2026-Q1") or a month ("2026-02") as its first and last month."""
    m = re.fullmatch(r"(20\d{2})-Q([1-4])", p or "")
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        return f"{y}-{3 * q - 2:02d}", f"{y}-{3 * q:02d}"
    if re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", p or ""):
        return p, p
    raise ValueError("start and end must be months (2025-11) or quarters (2025-Q4)")


def months_between(start: str, end: str) -> list[str]:
    """Every month from the earlier to the later of two months / quarters."""
    (a0, a1), (b0, b1) = _month_bounds(start), _month_bounds(end)
    lo, hi = min(a0, b0), max(a1, b1)
    n0, n1 = int(lo[:4]) * 12 + int(lo[5:]) - 1, int(hi[:4]) * 12 + int(hi[5:]) - 1
    if n1 - n0 >= 120:
        raise ValueError("a range covers at most ten years")
    return [f"{n // 12}-{n % 12 + 1:02d}" for n in range(n0, n1 + 1)]


def _quarter_of_month(m: str) -> str:
    return f"{m[:4]}-Q{(int(m[5:]) - 1) // 3 + 1}"


def _months_of(q: str) -> list[str]:
    a, _b = _month_bounds(q)
    return [f"{a[:4]}-{int(a[5:]) + i:02d}" for i in range(3)]


def month_label(m: str) -> str:
    return date(int(m[:4]), int(m[5:]), 1).strftime("%b'%y")


def month_runs(months: list[str]) -> str:
    """["2025-04", "2025-05", "2025-06", "2025-09"] -> "Apr'25–Jun'25, Sep'25"."""
    out: list[str] = []
    run: list[str] = []
    for m in sorted(months):
        if run and int(m[:4]) * 12 + int(m[5:]) != int(run[-1][:4]) * 12 + int(run[-1][5:]) + 1:
            out.append(month_label(run[0]) + ("–" + month_label(run[-1]) if len(run) > 1 else ""))
            run = []
        run.append(m)
    if run:
        out.append(month_label(run[0]) + ("–" + month_label(run[-1]) if len(run) > 1 else ""))
    return ", ".join(out)


def _split(detail: Optional[dict], balance: bool) -> dict[str, Decimal]:
    """A quarter figure's monthly split, only when it still accounts for the
    figure (a reviewer's later edit of the quarter total voids it)."""
    if not detail:
        return {}
    bd = detail.get("breakdown") or {}
    if not bd:
        return {}
    v = detail["value"]
    if balance:
        return bd if abs(bd[max(bd)] - v) <= 1 else {}
    return bd if len(bd) == 3 and abs(sum(bd.values()) - v) <= 1 else {}


def range_column(company: str, months: list[str], actuals: dict, budgets: dict) -> dict[str, Any]:
    """One company over any run of months, as one column. A quarter wholly in
    the range counts as its total; a quarter partly in it counts month by month
    when its statement printed monthly columns, and is named as unsplittable
    when it didn't. Flows add up, cash is as of the latest month-end with one,
    burn is over the months each flow covers."""
    mset = set(months)
    quarters = sorted({_quarter_of_month(m) for m in months})
    labels = dict(ROWS)
    act: dict[str, Optional[Decimal]] = {}
    covered: dict[str, list[str]] = {k: [] for k in _FLOWS}
    unsplit: dict[str, list[str]] = {}       # quarter -> months it could not give
    split_used: set[str] = set()
    for k in _FLOWS:
        total: Optional[Decimal] = None
        for q in quarters:
            qm = [m for m in _months_of(q) if m in mset]
            d = actuals.get((company, q), {}).get(k)
            if d is None:
                continue
            if len(qm) == 3:
                total = (total or Decimal(0)) + d["value"]
                covered[k] += qm
                continue
            bd = _split(d, False)
            if not bd:
                unsplit.setdefault(q, [])
                unsplit[q] = sorted(set(unsplit[q]) | set(qm))
                continue
            split_used.add(q)
            for m in qm:
                total = (total or Decimal(0)) + bd[m]
                covered[k].append(m)
        act[k] = total
    cash, cash_month = None, None
    for m in sorted(months, reverse=True):
        q = _quarter_of_month(m)
        d = actuals.get((company, q), {}).get("ending_cash")
        if d is None:
            continue
        if m == _months_of(q)[-1]:
            cash, cash_month = d["value"], m
            break
        bd = _split(d, True)
        if m in bd:
            cash, cash_month = bd[m], m
            split_used.add(q)
            break
    act["ending_cash"] = cash
    n = {k: len(covered[k]) for k in _FLOWS}
    gross = act["operating_expense"] / n["operating_expense"] if act["operating_expense"] is not None else None
    net = (-(act["operating_cash_flow"] + (act["capex"] or 0)) / n["operating_cash_flow"]
           if act["operating_cash_flow"] is not None else None)
    have = sorted(set().union(*covered.values()))
    actual = ({**act, "gross_monthly_burn": gross, "net_monthly_burn": net,
               "gross_runway": _runway(cash, gross), "net_runway": _runway(cash, net)}
              if have or cash is not None else {})
    # budgets: per quarter as typed; a quarter partly in the range counts pro rata
    bud: dict[str, Optional[Decimal]] = {}
    per_q = {q: {m: v for (c, p, m), v in budgets.items() if c == company and p == q} for q in quarters}
    prorated = False
    if all(per_q.values()):
        for k in _FLOWS:
            parts = []
            for q in quarters:
                share = Decimal(len([m for m in _months_of(q) if m in mset])) / 3
                prorated = prorated or share != 1
                v = per_q[q].get(k)
                parts.append(None if v is None else v * share)
            bud[k] = sum(parts, Decimal(0)) if all(v is not None for v in parts) else None
        last_q = quarters[-1]
        bud["ending_cash"] = per_q[last_q].get("ending_cash") if months[-1] == _months_of(last_q)[-1] else None
        bud = {**bud, **derive(bud, len(months))}
    missing = [m for m in months if not any(m in covered[k] for k in _FLOWS)]
    issues = []
    for q, qm in unsplit.items():
        issues.append({"kind": "missing", "message":
                       f"{month_runs(qm)}: "
                       f"{q} is on file only as a quarter total (its statement has no monthly "
                       "columns), so part of it can't be taken — widen the range to the whole "
                       "quarter, or upload a monthly statement"})
    gap = [m for m in missing if not any(m in qm for qm in unsplit.values())]
    if gap and have:
        issues.append({"kind": "missing", "message": "no approved figures for "
                       + month_runs(gap) + " — the totals cover the other months only"})
    partial = [k for k in _FLOWS if act[k] is not None and len(covered[k]) < len(have)]
    if partial:
        issues.append({"kind": "estimate", "message": ", ".join(labels[k] for k in partial)
                       + ": not in every month covered — added up over the months that have it"})
    if prorated and bud:
        issues.append({"kind": "check", "message": "budgets are typed per quarter: a quarter only "
                       "partly in the range counts pro rata by months"})
    if cash_month and cash_month != months[-1]:
        issues.append({"kind": "check", "message": f"Ending Cash is as of {month_label(cash_month)}, "
                       "the latest month-end on file in the range"})
    return {"actual": actual, "budget": bud, "sources": {}, "pending": {}, "checks": [],
            "issues": issues, "months": len(have), "covered": have, "missing": missing,
            "split_from": sorted(split_used), "cash_as_of": cash_month,
            "quarters": [q for q in quarters if any(actuals.get((company, q), {}).get(k) for k in BASE)]}


def report(year: Optional[int] = None, start: Optional[str] = None,
           end: Optional[str] = None) -> dict[str, Any]:
    """The report for one calendar year (default: the latest with actuals);
    with `start` and `end` quarters, one more column covering that range."""
    actuals, pending, conflicts = _actuals()
    budgets = db.get_budgets()
    years = sorted({int(q[:4]) for _c, q in actuals}
                   | {int(p[:4]) for _c, p, _m in budgets if p[:4].isdigit()})
    span = months_between(start, end) if start and end else []
    span_quarters = sorted({_quarter_of_month(m) for m in span})
    range_key = f"{span[0]}..{span[-1]}" if span else None
    if year is None:
        year = int(span[-1][:4]) if span else (years[-1] if years else date.today().year)
    quarters = [f"{year}-Q{n}" for n in range(1, 5)]
    unread: dict[tuple[str, str], list[dict]] = {}
    for sub in db.unread_submissions():
        q = _quarter_of(sub["period"] or "")
        if sub["report_type"] == "company_financials" and q:
            unread.setdefault((sub["entity_name"], q), []).append(sub)
    companies = sorted({c for c, q in actuals if q in quarters or q in span_quarters}
                       | {c for c, p, _m in budgets if p[:4] == str(year)}
                       | {c for c, q in pending if q in quarters}
                       | {c for c, q in unread if q in quarters}, key=_natural)
    latest = max((q for _c, q in actuals if q in quarters), default=None)
    out = []
    for company in companies:
        cols: dict[str, dict] = {}
        act_parts, bud_parts = [], []
        currency = "USD"
        for q in quarters:
            detail = actuals.get((company, q), {})
            act = {k: d["value"] for k, d in detail.items() if k in BASE}
            bud = {m: v for (c, p, m), v in budgets.items() if c == company and p == q}
            reported = {_REPORTED[k]: d["value"] for k, d in detail.items() if k in _REPORTED}
            for d in detail.values():
                currency = d["currency"]
            pend = {k: sid for k, sid in pending.get((company, q), {}).items() if k not in act}
            checks = _checks(act, reported)
            cols[q] = {
                "actual": {**act, **derive(act, 3)} if act else {},
                "budget": {**bud, **derive(bud, 3)} if bud else {},
                "sources": {k: {x: d[x] for x in ("file", "line", "status", "submission_id",
                                                  "manual")}
                            for k, d in detail.items() if k in BASE},
                "pending": pend,
                "checks": checks,
                "issues": ([{"kind": "unread", "submission_id": sub["id"],
                             "message": f"No figures could be read from {sub['filename']} — "
                                        "open it to see why, or type the figures in"}
                            for sub in unread.get((company, q), [])]
                           + (_issues(act, detail, pend, conflicts.get((company, q), {}), checks)
                              if act or pend or unread.get((company, q)) else [])),
            }
            if act:
                act_parts.append(act)
            if bud:
                bud_parts.append(bud)
        # the full year: actuals so far; the annual budget as entered, else the
        # quarterly budgets added up
        act_year = _year_total(act_parts)
        bud_year = {m: v for (c, p, m), v in budgets.items() if c == company and p == str(year)}
        if not bud_year and len(bud_parts) == 4:
            bud_year = _year_total(bud_parts)
        cols[str(year)] = {
            "actual": {**act_year, **derive(act_year, 3 * len(act_parts))} if act_parts else {},
            "budget": {**bud_year, **derive(bud_year, 12)} if bud_year else {},
            "sources": {}, "pending": {}, "checks": [], "issues": [],
            "months": 3 * len(act_parts)}
        if range_key:
            cols[range_key] = range_column(company, span, actuals, budgets)
        out.append({"company": company, "currency": currency, "columns": cols})
    all_quarters = sorted({q for _c, q in actuals} | {p for _c, p, _m in budgets
                                                      if re.fullmatch(r"20\d{2}-Q[1-4]", p)})
    return {"year": year, "years": years or [year],
            "periods": quarters + [str(year)] + ([range_key] if range_key else []),
            "range": {"key": range_key, "start": span[0], "end": span[-1], "months": span,
                      "quarters": span_quarters,
                      "label": f"{month_label(span[0])}–{month_label(span[-1])}"} if span else None,
            "quarters_on_file": all_quarters,
            "latest_quarter": latest, "rows": [{"key": k, "label": l} for k, l in ROWS],
            "companies": out}


def _natural(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _checks(act: dict, reported: dict) -> list[str]:
    """Where a finished report's own burn/runway disagrees with ours."""
    if not act or not reported:
        return []
    ours = derive(act, 3)
    out = []
    for k, v in reported.items():
        mine = ours.get(k)
        if mine is None:
            continue
        tol = Decimal("0.6") if k in RUNWAYS else max(Decimal(1000), abs(mine) * Decimal("0.01"))
        if abs(Decimal(mine) - v) > tol:
            out.append(f"{dict(ROWS)[k]}: the file shows {v:,.0f}, computed {mine:,.0f}")
    return out


# --------------------------------------------------------------------------
# Workbook export — Lam's layout
# --------------------------------------------------------------------------
_MONEY_K = '"$"#,##0_);("$"#,##0)'
_MONTHS = '0" Months"'


def _sheet_title(name: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:31] or "Company"
    title, n = base, 2
    while title.lower() in used:
        title = f"{base[:28]} {n}"
        n += 1
    used.add(title.lower())
    return title


def default_quarter(rep: dict) -> int:
    lq = rep.get("latest_quarter")
    return int(lq[-1]) if lq else 1


def workbook(year: Optional[int] = None, quarter: Optional[int] = None,
             start: Optional[str] = None, end: Optional[str] = None) -> tuple[bytes, str]:
    """(xlsx bytes, file name) — "Direct Investment Financials Q1 2026.xlsx"; with a
    start and end quarter, one more Budget | Actual pair for that range."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rep = report(year, start, end)
    year = rep["year"]
    quarter = quarter or default_quarter(rep)
    title = f"Direct Investment Financials Q{quarter} {year}"
    yy = str(year)[-2:]
    heads = [f"Q{n}'{yy}" for n in range(1, 5)] + [str(year)]
    if rep["range"]:
        heads.append(rep["range"]["label"])
        title += f" ({rep['range']['start']} to {rep['range']['end']})"
    bold = Font(bold=True)
    pending_fill = PatternFill("solid", fgColor="FFF4CE")

    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for comp in rep["companies"]:
        ws = wb.create_sheet(_sheet_title(comp["company"], used))
        unit = "$000" if comp["currency"] == "USD" else f"{comp['currency']} 000"
        ws.cell(1, 1, f"{comp['company']}(in {unit})").font = bold
        for i, (per, head) in enumerate(zip(rep["periods"], heads)):
            c = 2 + 2 * i
            ws.cell(1, c, head).font = bold
            ws.cell(1, c).alignment = Alignment(horizontal="center")
            ws.merge_cells(start_row=1, start_column=c, end_row=1, end_column=c + 1)
            ws.cell(2, c, "Budget")
            ws.cell(2, c + 1, "Actual")
            col = comp["columns"][per]
            for r, (key, _label) in enumerate(ROWS, start=3):
                for j, kind in enumerate(("budget", "actual")):
                    v = col[kind].get(key)
                    cell = ws.cell(r, c + j)
                    if v is None:
                        if kind == "actual" and key in col.get("pending", []):
                            cell.fill = pending_fill
                            cell.comment = Comment("Read from the file, waiting for review "
                                                   "in Portfolio Financials Studio.", "PFS")
                        continue
                    if key in RUNWAYS:
                        cell.value, cell.number_format = int(v), _MONTHS
                    else:
                        cell.value, cell.number_format = float(Decimal(v) / 1000), _MONEY_K
        for r, (_key, label) in enumerate(ROWS, start=3):
            ws.cell(r, 1, label)
        ws.column_dimensions["A"].width = 24
        for c in range(2, 2 + 2 * len(heads)):
            ws.column_dimensions[get_column_letter(c)].width = 11
        ws.freeze_panes = "B3"

    # where every actual came from (file and printed line), for the audit trail
    src = wb.create_sheet("Sources")
    src.append(["Company", "Quarter", "Figure", "Value", "Status", "File", "Line in the file"])
    for cell in src[1]:
        cell.font = bold
    labels = dict(ROWS)
    for comp in rep["companies"]:
        for per in rep["periods"][:4]:
            col = comp["columns"][per]
            for key in BASE:
                s = col["sources"].get(key)
                if s:
                    src.append([comp["company"], per, labels[key], float(col["actual"][key]),
                                s["status"], s["file"], s["line"]])
                elif key in col["pending"]:
                    src.append([comp["company"], per, labels[key], None, "waiting for review",
                                "", ""])
    src.append([])
    src.append(["Gross Monthly Burn = Operating Expense ÷ months; Net Monthly Burn = "
                "−(Operating Cash Flow + CapEx) ÷ months; Runway = Ending Cash ÷ monthly burn, "
                "whole months. Only approved values are reported."])
    for c, w in zip("ABCDEFG", (22, 10, 20, 16, 18, 48, 48)):
        src.column_dimensions[c].width = w
    if not rep["companies"]:
        ws = wb.create_sheet("Direct Investments", 0)
        ws.cell(1, 1, f"No approved direct-investment figures for {year} yet.")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"{title}.xlsx"
