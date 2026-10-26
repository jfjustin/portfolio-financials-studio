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


def _lp_names() -> list[str]:
    return [n.strip() for n in settings.lp_names.split(",") if n.strip()]


def build_messages(page_texts: list[tuple[int, str]], page_images: list[tuple[int, bytes]],
                   filename: str) -> list:
    fields = "\n".join(f'  - "{k}": {v}' for k, v in FIELD_GUIDE.items())
    investor = " / ".join(_lp_names()) or "the limited partner the statement is addressed to"
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
   it up for that quarter, as {{"components": [{{"label": "<line label>", "value":
   "<value EXACTLY as printed>"}}]}}. Copy values verbatim (keep parentheses, minus
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

JSON shape:
{{"document_kind": "...", "investor_column": "...", "period_column": "...",
  "period_end": "...", "period_start": null, "currency": "...", "number_format": "...",
  "fields": {{"funding": {{"components": [{{"label": "...", "value": "..."}}]}}}},
  "notes": "..."}}"""
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
                         filename: str) -> dict:
    """Ask the model; returns its normalized answer or {"_error": ...}."""
    if not settings.llm_enabled:
        return {"_error": "no model configured"}
    provider = get_provider()
    if page_images and not provider.supports_vision:
        page_images = []
    if not page_texts and not page_images:
        return {"_error": "nothing readable to send to the model"}
    data = provider.complete_json(build_messages(page_texts, page_images, filename))
    if not isinstance(data, dict) or data.get("_error"):
        return {"_error": (data or {}).get("_error", "no answer")}
    fields: dict[str, list[dict]] = {}
    for key, spec in (data.get("fields") or {}).items():
        if key not in FIELD_GUIDE or not isinstance(spec, dict):
            continue
        comps = [c for c in spec.get("components") or []
                 if isinstance(c, dict) and str(c.get("value", "")).strip()]
        if comps:
            fields[key] = [{"label": str(c.get("label", key)), "value": str(c["value"])}
                           for c in comps]
    return {
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
