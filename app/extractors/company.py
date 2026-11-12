"""A portfolio company's own financial statements -> Lam Capital's
"Direct Investment Financials" figures.

Portfolio companies report on their own Profit and Loss, Balance Sheet and
Statement of Cash Flows (QuickBooks exports, audited statements, scans). The
quarterly report takes five figures from them:

  Revenue              P&L  "Total for Income" / "Total revenue" / "Revenue"
  Operating Expense    P&L  "Total for Expenses" / "Total operating expenses"
  Operating Cash Flow  CF   "Net cash provided by (used in) operating activities"
  CapEx                CF   fixed-asset purchases under investing activities
  Ending Cash          BS   "Total for Bank Accounts" / "Cash and cash equivalents"
                            (else CF "Cash at end of period")

Lines are matched on their whole wording inside the statement they belong to,
never on a fragment ("Deferred Revenue" and "Department of Revenue Payable"
are not revenue), and each figure is read from the column that covers the
reporting period: the quarter's "Total", not one month of it, a prior year or
a budget column. Burn and runway are computed from these figures
(performance.py), never read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ..models import RawItem, parse_number
from .base import _MONTH_RX, column_fit, normalize_period

CORE = ("revenue", "operating_expense", "operating_cash_flow", "capex", "ending_cash")
_BALANCES = {"ending_cash"}
_UNSCALED = {"gross_runway_months", "net_runway_months"}   # months, not money

# metric -> statements it may come from (in order of preference) and its
# whole-line wordings (in order of preference)
_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "revenue": (("income_statement",), (
        r"total income", r"total (?:net )?revenues?(?:,? net)?", r"(?:net )?revenues?(?:,? net)?",
        r"total (?:net )?sales", r"(?:net )?sales(?:,? net)?", r"total operating revenues?",
        r"operating revenues?")),
    "operating_expense": (("income_statement",), (
        r"total expenses", r"total operating expenses?", r"operating expenses?,? total",
        r"total opex", r"operating expenses?")),
    "operating_cash_flow": (("cash_flow",), (
        r"net cash\b.*\boperating activities", r"(?:net )?cash flows?\b.*\boperating activities",
        r"net cash\b.*\boperations", r"(?:net )?operating cash flows?",
        r"cash (?:used in|provided by|generated (?:from|by)) operations")),
    "capex": (("cash_flow",), (
        r"(?:total )?capital expenditures?", r"capex")),
    "ending_cash": (("balance_sheet", "cash_flow"), (
        r"total bank accounts", r"total cash and cash equivalents", r"cash and cash equivalents",
        r"total cash", r"cash",
        r"(?:total )?cash\b.*\bend of (?:the )?(?:period|year|quarter|month)",
        r"ending cash(?: balance)?")),
    "net_income": (("income_statement", "cash_flow"), (
        r"net income(?: \(loss\))?", r"net \(?loss\)?(?: income)?", r"net loss and comprehensive loss",
        r"net income \(loss\) and comprehensive income \(loss\)")),
    "gross_monthly_burn": ((), (r"gross monthly burn(?: rate)?",)),
    "gross_runway_months": ((), (r"gross runway(?: \(?months\)?)?",)),
    "net_monthly_burn": ((), (r"net monthly burn(?: rate)?",)),
    "net_runway_months": ((), (r"net runway(?: \(?months\)?)?",)),
}
# balance-sheet wordings are not cash-flow wordings and vice versa
_STATEMENT_ONLY = {
    ("ending_cash", "cash_flow"): (r"(?:total )?cash\b.*\bend of (?:the )?(?:period|year|quarter|month)",
                                   r"ending cash(?: balance)?"),
}

_PART = re.compile(r"\b(operating|investing|financing) activities\b", re.I)
_PART_TOTAL = re.compile(r"^(?:net cash|net (?:increase|decrease)|total|(?:net )?cash flows? "
                         r"(?:provided|used|\(used))", re.I)
_CAPEX_YES = re.compile(r"fixed asset|property|plant|equipment|computer|server|furniture|fixture|"
                        r"leasehold|vehicle|machinery|building|construction in progress|"
                        r"capital expenditure|capex|tooling|hardware|printer|software", re.I)
_CAPEX_NO = re.compile(r"proceeds|\bsales? of\b|disposal|investments?\b|securities|marketable|"
                       r"treasury|certificates? of deposit|notes? receivable|loans?\b|deposits?\b|"
                       r"acquisitions? of (?:business|compan|subsidiar)|interest|dividends?|"
                       r"restricted cash|intangible|goodwill", re.I)


@dataclass
class Line:
    """One printed statement line and its figure(s), one RawItem per column."""
    label: str
    section: str
    statement: str
    items: list[RawItem]
    part: str = ""            # operating | investing | financing, on a cash-flow statement
    is_total: bool = False    # "Net cash provided by investing activities"


@dataclass
class Pick:
    metric: str
    value: Optional[Decimal]
    label: str
    raw_value: str
    provenance: str
    confidence: float
    flags: list[str] = field(default_factory=list)
    source: str = ""          # where known wording came from (taught / learned), if any
    sections: list[str] = field(default_factory=list)
    items: list[RawItem] = field(default_factory=list)
    # the figure month by month ({"2026-01": value, ...}), when the statement prints
    # one column per month and they add up to the figure (balances: month-ends)
    breakdown: dict[str, Decimal] = field(default_factory=dict)


def norm(label: str) -> str:
    """Line wording for matching: lower case, no QuickBooks account numbers,
    "Total for X" read as "Total X"."""
    s = (label or "").lower().replace("’", "'")
    s = re.sub(r"\s+", " ", s).strip(" :.-–\t$*")
    s = re.sub(r"^total for ", "total ", s)
    s = re.sub(r"^(total )?\d{3,6}(?:\.\d+)?\s+", r"\1", s)
    return s.strip()


def _row_key(item: RawItem) -> str:
    p = item.provenance or ""
    m = re.match(r"^(.*)![A-Z]+(\d+)", p)
    if m:
        return f"{m.group(1)}!{m.group(2)}"
    m = re.match(r"^row(\d+)col\d+", p)
    return "row" + m.group(1) if m else ""


def _sheet(item: RawItem) -> str:
    """The worksheet a cell came from; a PDF's pages read as one stream."""
    p = item.provenance or ""
    return p.split("!", 1)[0] if "!" in p else ""


def _header(item: RawItem) -> str:
    m = re.search(r"!\S+\s+\((.*)\)\s*$|^row\d+col\d+\s+\((.*)\)\s*$", item.provenance or "")
    return (m.group(1) or m.group(2) or "") if m else ""


def group_lines(raw_items: list[RawItem]) -> list[Line]:
    """Statement lines in document order; a spreadsheet row's figure columns
    are one line."""
    lines: list[Line] = []
    for it in raw_items:
        rk = _row_key(it)
        if rk and lines and _row_key(lines[-1].items[-1]) == rk and lines[-1].label == it.label:
            lines[-1].items.append(it)
            continue
        lines.append(Line(it.label, it.section, it.statement, [it]))
    # cash-flow parts: a heading starts one, its "Net cash ..." total closes it
    part, sheet, statement, section = "", None, None, None
    for ln in lines:
        sh = _sheet(ln.items[0])
        if sh != sheet or ln.statement != statement:
            part, sheet, statement, section = "", sh, ln.statement, None
        if ln.section != section:
            section = ln.section
            m = _PART.search(section or "")
            if m:
                part = "" if _PART_TOTAL.match(section.strip()) else m.group(1).lower()
        m = _PART.search(ln.label)
        if m and _PART_TOTAL.match(norm(ln.label)):
            ln.part, ln.is_total, part = m.group(1).lower(), True, ""
            continue
        ln.part = part
    return lines


def _value(item: RawItem, dc: Optional[bool]) -> Optional[Decimal]:
    v = parse_number(item.value_raw, dc)[0]
    if v is None and item.value_raw.strip() in {"-", "–", "—"}:
        v = Decimal(0)
    return v


def _column(line: Line, period: str, balance: bool, dc: Optional[bool]) -> Optional[RawItem]:
    """The figure column covering the reporting period; ties go to the right-most."""
    usable = [it for it in line.items if _value(it, dc) is not None]
    if not usable:
        return None
    ranked = sorted(enumerate(usable),
                    key=lambda iv: (column_fit(_header(iv[1]), period, balance=balance), iv[0]))
    return ranked[-1][1]


_NOT_ONE_MONTH = re.compile(r"total|ytd|year|quarter|\bq[1-4]\b|ended|ending|through|budget|"
                            r"forecast|\bplan\b|\bvar|%|prior|\bpy\b", re.I)


def _month_of(header: str) -> Optional[str]:
    """"Jan 2026" / "Jan 31, 2026" -> "2026-01"; a total, range or budget column -> None."""
    h = (header or "").strip()
    if len(_MONTH_RX.findall(h)) != 1 or _NOT_ONE_MONTH.search(h):
        return None
    p = normalize_period(h)
    return p[:7] if p and re.match(r"20\d{2}-\d{2}", p) else None


def _quarter_months(period: str) -> list[str]:
    m = re.fullmatch(r"(20\d{2})-Q([1-4])", period or "")
    if not m:
        return []
    y, q = int(m.group(1)), int(m.group(2))
    return [f"{y}-{3 * q - 2 + i:02d}" for i in range(3)]


def _months(line: Line, dc: Optional[bool], scale: int, period: str) -> dict[str, Decimal]:
    """The line's value per month of the quarter, from its month columns."""
    want = _quarter_months(period)
    out: dict[str, Decimal] = {}
    for it in line.items:
        m = _month_of(_header(it))
        v = _value(it, dc)
        if m in want and v is not None and m not in out:
            out[m] = v * scale if scale != 1 else v
    return out


def _checked_split(metric: str, split: dict[str, Decimal], value: Optional[Decimal],
                   period: str) -> dict[str, Decimal]:
    """Keep a monthly split only when it accounts for the figure exactly: three
    months adding up to a flow, or the quarter's last month-end for a balance."""
    want = _quarter_months(period)
    if value is None or not want or not split:
        return {}
    if metric in _BALANCES:
        return split if split.get(want[-1]) is not None and abs(split[want[-1]] - value) <= 1 else {}
    return split if len(split) == 3 and abs(sum(split.values()) - value) <= 1 else {}


def _pick_line(metric: str, line: Line, item: RawItem, dc: Optional[bool], scale: int,
               confidence: float, source: str = "", period: str = "") -> Pick:
    value = _value(item, dc)
    flags: list[str] = []
    if value is not None and scale != 1 and metric not in _UNSCALED:
        value *= scale
        flags.append(f"scaled_x{scale}")
    cols = re.search(r"cols=(\d+)", item.provenance or "")
    if cols:
        flags.append(f"multi_column({cols.group(1)})")
    prov = item.provenance
    header = _header(item)
    if len(line.items) > 1 and header:
        prov += " · column chosen for the period"
    split = {} if metric in _UNSCALED else _checked_split(
        metric, _months(line, dc, scale, period), value, period)
    return Pick(metric, value, line.label, item.value_raw, prov, confidence, flags, source,
                [line.section] if line.section else [], [item], split)


def read_statements(raw_items: list[RawItem], period: str, dc: Optional[bool], scale: int,
                    terms=None) -> tuple[dict[str, Pick], list[str]]:
    """{metric: Pick} for the Direct Investment figures the statements show, and
    review notes. `terms` (a TermIndex) lets taught / confirmed wording win."""
    lines = group_lines(raw_items)
    picks: dict[str, Pick] = {}
    notes: list[str] = []

    # 1. wording this company's reviewers taught or confirmed before
    if terms is not None:
        for ln in lines:
            hit = terms.lookup(ln.section, ln.label)
            if not hit or hit[0] not in _RULES or hit[0] in picks:
                continue
            item = _column(ln, period, hit[0] in _BALANCES, dc)
            if item is not None:
                picks[hit[0]] = _pick_line(hit[0], ln, item, dc, scale, max(hit[1], 0.95),
                                           hit[2], period)

    # 2. standard wording, inside the statement each figure belongs to
    for metric, (statements, patterns) in _RULES.items():
        if metric in picks:
            continue
        for stmt in statements or ("",):
            allowed = _STATEMENT_ONLY.get((metric, stmt))
            for pat in patterns:
                if allowed is not None and pat not in allowed:
                    continue
                if stmt == "balance_sheet" and (metric, "cash_flow") in _STATEMENT_ONLY \
                        and pat in _STATEMENT_ONLY[(metric, "cash_flow")]:
                    continue
                rx = re.compile(pat)
                for ln in lines:
                    if stmt and ln.statement not in (stmt, ""):
                        continue
                    if metric == "capex" and ln.part not in ("investing", ""):
                        continue
                    if not rx.fullmatch(norm(ln.label)):
                        continue
                    item = _column(ln, period, metric in _BALANCES, dc)
                    if item is None:
                        continue
                    picks[metric] = _pick_line(metric, ln, item, dc, scale, 0.95, period=period)
                    break
                if metric in picks:
                    break
            if metric in picks:
                break

    # 3. CapEx from the investing section: fixed-asset purchase lines, summed
    if "capex" not in picks:
        cap = _capex(lines, period, dc, scale)
        if cap is not None:
            picks["capex"] = cap

    # 4. the balance sheet's cash and the cash-flow statement's closing cash agree
    if "ending_cash" in picks and picks["ending_cash"].items[0].statement == "balance_sheet":
        cf = [ln for ln in lines if ln.statement == "cash_flow"
              and any(re.fullmatch(p, norm(ln.label)) for p in _STATEMENT_ONLY[("ending_cash",
                                                                                "cash_flow")])]
        if cf:
            item = _column(cf[0], period, True, dc)
            v = _value(item, dc) if item is not None else None
            bs = picks["ending_cash"].value
            if v is not None and bs is not None:
                v = v * scale
                if abs(v - bs) > max(Decimal(1), abs(bs) * Decimal("0.001")):
                    picks["ending_cash"].flags.append(f"cash_flow_ending_cash({v})")
                    notes.append(f"Balance-sheet cash {bs:,.2f} differs from the cash-flow "
                                 f"statement's closing cash {v:,.2f} (e.g. restricted cash) "
                                 "— check which one the report should carry.")
                else:
                    picks["ending_cash"].provenance += " · agrees with the cash-flow statement"
    return picks, notes


def _capex(lines: list[Line], period: str, dc: Optional[bool], scale: int) -> Optional[Pick]:
    inv = [ln for ln in lines if ln.part == "investing" and ln.statement in ("cash_flow", "")]
    lines_ = [ln for ln in inv if not ln.is_total and not norm(ln.label).startswith("total ")]
    subtotals = [ln for ln in inv if not ln.is_total and norm(ln.label).startswith("total ")]
    totals = [ln for ln in inv if ln.is_total]

    def capex_like(ln: Line) -> bool:
        if re.search(r"proceeds|\bsales? of\b|disposal", ln.label, re.I):
            return False
        return bool(_CAPEX_YES.search(ln.label)) or not _CAPEX_NO.search(ln.label)

    chosen = [ln for ln in lines_ if capex_like(ln)] or [ln for ln in subtotals if capex_like(ln)]
    if not chosen and (lines_ or subtotals):
        # investing lines, none of them buying fixed assets (e.g. securities)
        return Pick("capex", Decimal(0), "(no fixed-asset purchases)", "",
                    "investing lines that are not capex: "
                    + ", ".join(ln.label for ln in lines_ + subtotals), 0.9, ["no_capex_lines"])
    if chosen:
        parts: dict[tuple[str, str], tuple[Line, RawItem, Decimal]] = {}
        for ln in chosen:
            item = _column(ln, period, False, dc)
            v = _value(item, dc) if item is not None else None
            if v is not None:
                parts.setdefault((norm(ln.label), str(v)), (ln, item, v))   # PDFs may repeat a line
        if not parts:
            return None
        total = sum((v for _l, _i, v in parts.values()), Decimal(0))
        flags = []
        if scale != 1:
            total *= scale
            flags.append(f"scaled_x{scale}")
        skipped = [ln.label for ln in lines_ if ln not in chosen]
        prov = "; ".join(i.provenance for _l, i, _v in parts.values())
        if skipped:
            prov += f" · not capex: {', '.join(skipped)}"
        elif totals:
            # every investing line is capex, so they must add up to the printed
            # total — if not, a line was misread or missed
            item = _column(totals[0], period, False, dc)
            t = _value(item, dc) if item is not None else None
            if t is not None:
                t *= scale
                if abs(t - total) > Decimal(1):
                    flags.append(f"investing_total({t})")
                else:
                    prov += " · adds up to the investing total"
        split: dict[str, Decimal] = {}
        for ln, _i, _v in parts.values():
            for m, v in _months(ln, dc, scale, period).items():
                split[m] = split.get(m, Decimal(0)) + v
        return Pick("capex", total, " + ".join(ln.label for ln, _i, _v in parts.values()),
                    " + ".join(i.value_raw for _l, i, _v in parts.values()), prov, 0.95, flags,
                    "", sorted({ln.section for ln, _i, _v in parts.values() if ln.section}),
                    [i for _l, i, _v in parts.values()], _checked_split("capex", split, total, period))
    if totals:
        ln = totals[0]
        item = _column(ln, period, False, dc)
        v = _value(item, dc) if item is not None else None
        if v is not None:
            pick = _pick_line("capex", ln, item, dc, scale, 0.95, period=period)
            if v != 0:
                pick.flags.append("capex_from_investing_total")
            return pick
    if any(ln.statement == "cash_flow" for ln in lines) and any(
            ln.part == "operating" or ln.is_total for ln in lines):
        # a cash-flow statement with nothing under investing activities: no capex
        return Pick("capex", Decimal(0), "(no investing activity)", "",
                    "cash-flow statement shows no investing lines", 0.9, ["no_investing_lines"])
    return None
