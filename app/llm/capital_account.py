"""Model step for investor (LP) capital-account statements, in any layout.

Deterministic parsing handles statements laid out the way it expects. Real
statements vary: a column for the whole partnership next to the investor's,
"3 months / year to date / inception" columns, since-inception statements that
only break out "Called in Q1, 2026"-style lines, European number formats,
scanned pages. This step asks the configured model to read the statement the
way an analyst would — pick OUR investor's column (settings.lp_names) and the
CURRENT QUARTER's column — and to copy the printed lines that make up each
roll-forward field. Code, not the model, parses and sums those printed values.

Everything returned is a suggestion: flagged `llm_suggested`, `needs_review`,
then carried forward / reconciled like any other roll-forward upload.
"""
from __future__ import annotations

import base64
from typing import Optional

from ..config import settings
from . import get_provider

# roll-forward fields the model may fill, with the guidance it gets for each
FIELD_GUIDE = {
    "beginning_balance": "capital account / fair market value at the START of the quarter "
                         "(e.g. 'Opening value', 'Beginning balance', capital account 'as at' "
                         "the previous quarter-end)",
    "funding": "capital contributed / called / drawn down IN the quarter",
    "return_of_capital": "distributions paid to the investor IN the quarter (negative)",
    "dividends": "dividend / income distributions IN the quarter",
    "investment_mtm": "unrealized gains/losses in the quarter, including unrealized FX",
    "net_realized_gain": "realized gains/losses in the quarter",
    "accrued_interest": "interest / investment income in the quarter",
    "other": "management fees, partnership/operating expenses, GP share or carried "
             "interest, taxes, other income/expense in the quarter",
    "equity_income_loss": "ONLY when the statement gives a single net income/(loss) line "
                          "instead of the breakdown above (never both)",
    "ending_balance": "capital account / fair market value at the END of the quarter "
                      "(e.g. 'Closing value', 'Capital account at Q1')",
    "commitment": "the investor's total capital commitment",
    "remaining_commitment": "unfunded / undrawn commitment",
    "cumulative_contributions": "total contributions since inception / to date",
    "cumulative_distributions": "total distributions since inception / to date",
}


def _lp_names(extra: str = "") -> list[str]:
    names = [n.strip() for n in f"{settings.lp_names},{extra}".split(",") if n.strip()]
    return list(dict.fromkeys(names))


def context_text(ctx: Optional[dict]) -> str:
    """What the app already knows: statement wording (preset + taught) and what
    reviewers confirmed for this fund before. Empty when there's nothing."""
    if not ctx:
        return ""
    entity = ctx.get("entity") or "this fund"
    lines: list[str] = []
    terms = ctx.get("terms") or []
    if terms:
        lines.append("Statement wording we already know (quarters, years, dates and "
                     "gain/loss wording vary from quarter to quarter):")
        for t in terms[:80]:
            sec = f"[{t['section']}] " if t.get("section") else ""
            target = ("not a quarter value — skip" if t["metric_key"] == "ignore"
                      else t["metric_key"])
            scope = f" ({entity} only)" if t["source"] == "taught_fund" else ""
            note = f" — {t['note']}" if t.get("note") and not t["note"].startswith("taught from") else ""
            lines.append(f'  - {sec}"{t["label"]}" -> {target}{scope}{note}')
    mem = ctx.get("memory")
    if mem:
        lines.append(f"What reviewers already confirmed for {entity}:")
        for c in mem.get("columns") or []:
            lines.append(f'  - our investor\'s column/row was "{c["investor"]}", '
                         f'quarter column "{c["period"]}"')
        if mem.get("currency"):
            lines.append(f"  - currency: {mem['currency']}")
        known = {(t["section_key"], t["label_key"], t["metric_key"]) for t in terms}
        conf = [e for e in mem.get("learned") or []
                if (e["section_key"], e["label_key"], e["metric_key"]) not in known][:40]
        if conf:
            lines.append("  - confirmed lines: " + "; ".join(
                (f"[{e['section']}] " if e["section"] else "") + f'"{e["label"]}" -> {e["metric_key"]}'
                for e in conf))
        lq = mem.get("last_quarter")
        if lq:
            vals = ", ".join(f"{k} {v:,.0f}" for k, v in lq["values"].items()
                             if k in ("beginning_balance", "ending_balance", "funding",
                                      "return_of_capital"))
            lines.append(f"  - last confirmed quarter {lq['period']}: {vals}. This quarter's "
                         "beginning balance should equal that ending balance unless the "
                         "statement restates it — if it differs, say so in notes.")
        notes = (mem.get("profile") or {}).get("notes")
        if notes:
            lines.append(f"  - reviewer notes on this fund's statements: {notes}")
    commitment = ctx.get("commitment")
    if commitment:
        lines.append(f"Our investor's commitment to {entity} is {commitment} — use it to find "
                     "our row in a table of investors when names are missing or redacted.")
    if lines:
        lines.append("Use this as guidance. Where this document clearly differs, follow the "
                     "document and say what changed in \"notes\".")
    return "\n".join(lines)


def build_messages(page_texts: list[tuple[int, str]], page_images: list[tuple[int, bytes]],
                   filename: str, context: Optional[dict] = None) -> list:
    fields = "\n".join(f'  - "{k}": {v}' for k, v in FIELD_GUIDE.items())
    aliases = (((context or {}).get("memory") or {}).get("profile") or {}).get(
        "investor_aliases") or ""
    investor = (" / ".join(_lp_names(aliases))
                or "the limited partner the statement is addressed to")
    instructions = f"""You read private-fund investor statements. Our investor is: {investor}.
File name: {filename}

Return ONE quarter of OUR investor's capital-account roll-forward as a JSON object.

1. "document_kind":
   - "investor_statement" — the document shows our investor's own capital account
     (a column, row or whole statement for that one investor; a statement addressed
     to a single limited partner counts even if names are redacted).
   - "fund_level" — only whole-fund / partnership totals, or a table of many
     investors where ours cannot be identified (e.g. redacted names).
   - "other".
2. Choosing the column:
   - If there are columns for the whole fund/partnership AND for our investor, use
     OUR INVESTOR's column, never the total.
   - If there are columns for different periods ("3 months", "quarter", "year to
     date", "inception to date", or two dates), use the CURRENT QUARTER's column.
   - If the statement is cumulative and only some lines are for the quarter (e.g.
     "Called in Q1, 2026", "Capital Loss Q1, 2026"), take quarter activity only from
     those quarter-specific lines.
3. "fields": for each field below that is present, list the printed line(s) that make
   it up for that quarter, as {{"components": [{{"section": "<heading the line sits
   under, as printed, or empty>", "label": "<line label as printed>", "value": "<value
   EXACTLY as printed>"}}]}}. Copy values verbatim (keep parentheses, minus
   signs, thousands separators). Do not add, convert or compute anything. Never put
   year-to-date, since-inception or "to date" totals into quarter fields (use the
   cumulative_* fields for those). When a subtotal and its components both appear,
   list only the components. Omit fields that aren't there.
{fields}
4. Also return:
   "period_end": the quarter-end date the chosen column reports (YYYY-MM-DD),
   "period_start": start date of the period that column covers (YYYY-MM-DD) or null,
   "investor_column": header/label of the column or row you used,
   "period_column": header of the period column you used,
   "currency": ISO code (USD, EUR, ...),
   "number_format": "1,234.56" or "1.234,56" (how this document writes numbers),
   "notes": one short sentence on anything a reviewer should check.
5. When "document_kind" is "fund_level", ALSO return:
   "fund_fields": the WHOLE FUND's figures for the current quarter, using the same
     field names and the same components format — beginning_balance = net assets at
     the start of the quarter, funding = capital called / contributed in the quarter,
     return_of_capital = distributions in the quarter (negative), accrued_interest =
     investment income in the quarter, other = fees and expenses in the quarter
     (negative), net_realized_gain, investment_mtm = change in unrealized in the
     quarter, ending_balance = net assets at quarter end, commitment = TOTAL
     commitments of all investors. Prefer the fund's statement of changes in net
     assets / statement of operations for the quarter.
   "investor_row": if the document has a table of individual investors (e.g. capital
     accounts) and the guidance below identifies OUR row — by a name/alias, or by our
     commitment amount — return that row as {{"row_label": "...", "commitment": "...",
     "paid_in": "...", "distributions": "...", "capital_account": "..."}} with values
     exactly as printed (capital_account = the row's capital account / NAV at quarter
     end). If several rows match our commitment and show identical figures, use any of
     them and say so in notes. If ours can't be identified, return null.

JSON shape:
{{"document_kind": "...", "investor_column": "...", "period_column": "...",
  "period_end": "...", "period_start": null, "currency": "...", "number_format": "...",
  "fields": {{"funding": {{"components": [{{"section": "...", "label": "...", "value": "..."}}]}}}},
  "fund_fields": {{}}, "investor_row": null,
  "notes": "..."}}"""
    known = context_text(context)
    if known:
        instructions += "\n\n" + known
    content: list = [{"type": "text", "text": instructions}]
    for pno, text in page_texts:
        content.append({"type": "text", "text": f"--- page {pno} (text, columns aligned) ---\n{text}"})
    for pno, png in page_images:
        content.append({"type": "text", "text": f"--- page {pno} (scanned image) ---"})
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(png).decode()}})
    return [{"role": "user", "content": content}]


_CUES = ("capital account", "limited partner", "partner's capital", "partners' capital",
         "capital called", "contribution", "commitment", "drawdown", "distribution")


def select_pages(pages: list[dict], budget: int) -> list[tuple[int, str]]:
    """Text pages for the model: everything if it fits, else the pages most
    about capital accounts (kept in document order) up to the budget."""
    texts = [(p["page"], p["text"]) for p in pages if p["has_text"]]
    if sum(len(t) for _, t in texts) <= budget:
        return texts
    names = [n.lower() for n in _lp_names()]

    def score(t: str) -> int:
        low = t.lower()
        return sum(low.count(c) for c in _CUES) + 5 * sum(low.count(n) for n in names)
    chosen, used = [], 0
    for pno, t in sorted(texts, key=lambda pt: -score(pt[1])):
        if used + len(t) > budget:
            continue
        chosen.append((pno, t))
        used += len(t)
    return sorted(chosen)


def read_capital_account(page_texts: list[tuple[int, str]],
                         page_images: list[tuple[int, bytes]],
                         filename: str, context: Optional[dict] = None) -> dict:
    """Ask the model; returns its normalized answer or {"_error": ...}."""
    if not settings.llm_enabled:
        return {"_error": "no model configured"}
    provider = get_provider()
    if page_images and not provider.supports_vision:
        page_images = []
    if not page_texts and not page_images:
        return {"_error": "nothing readable to send to the model"}
    data = provider.complete_json(build_messages(page_texts, page_images, filename, context))
    if not isinstance(data, dict) or data.get("_error"):
        return {"_error": (data or {}).get("_error", "no answer")}
    fields: dict[str, list[dict]] = {}
    for key, spec in (data.get("fields") or {}).items():
        if key not in FIELD_GUIDE or not isinstance(spec, dict):
            continue
        comps = [c for c in spec.get("components") or []
                 if isinstance(c, dict) and str(c.get("value", "")).strip()]
        if comps:
            fields[key] = [{"label": str(c.get("label", key)), "value": str(c["value"]),
                            "section": str(c.get("section") or "").strip()}
                           for c in comps]
    fund_fields: dict[str, list[dict]] = {}
    for key, spec in (data.get("fund_fields") or {}).items():
        if key not in FIELD_GUIDE or not isinstance(spec, dict):
            continue
        comps = [c for c in spec.get("components") or []
                 if isinstance(c, dict) and str(c.get("value", "")).strip()]
        if comps:
            fund_fields[key] = [{"label": str(c.get("label", key)), "value": str(c["value"]),
                                 "section": str(c.get("section") or "").strip()}
                                for c in comps]
    row = data.get("investor_row")
    investor_row = ({k: _s(row.get(k)) for k in
                     ("row_label", "commitment", "paid_in", "distributions", "capital_account")}
                    if isinstance(row, dict) else None)
    return {
        "fund_fields": fund_fields,
        "investor_row": investor_row,
        "document_kind": str(data.get("document_kind") or "other"),
        "investor_column": _s(data.get("investor_column")),
        "period_column": _s(data.get("period_column")),
        "period_end": _s(data.get("period_end")),
        "period_start": _s(data.get("period_start")),
        "currency": _s(data.get("currency")),
        "number_format": _s(data.get("number_format")),
        "notes": _s(data.get("notes")),
        "fields": fields,
    }


def _s(v) -> Optional[str]:
    return None if v in (None, "", "null") else str(v).strip()
