"""Model step for a portfolio company's own financial statements.

Used when the statements are scanned images, or when their wording or layout
leaves one of the Direct Investment figures unread by extractors/company.py.
The model names the printed line(s) and copies their figures; code parses,
scales and sums them. Everything it returns is a suggestion for review.
"""
from __future__ import annotations

import base64
from typing import Optional

from ..config import settings
from . import get_provider

FIELD_GUIDE = {
    "revenue": "total revenue / total income for the period, from the profit and loss "
               "(income statement): 'Total for Income', 'Total Income', 'Total revenue', "
               "'Revenue'. If the income section is printed with no figure, return \"0\".",
    "operating_expense": "total operating expenses for the period: 'Total for Expenses', "
                         "'Total Expenses', 'Total operating expenses' (not cost of goods sold, "
                         "not 'other expenses' below operating income)",
    "operating_cash_flow": "net cash provided by (used in) operating activities for the "
                           "period, from the statement of cash flows",
    "capex": "each investing-activities line that buys fixed assets / property and equipment "
             "(one component per printed line; not purchases of investments or securities, "
             "not deposits, not sale proceeds)",
    "ending_cash": "cash at the END of the period: balance sheet 'Total Bank Accounts' / "
                   "'Total for Bank Accounts' / 'Cash and cash equivalents'; else the cash-flow "
                   "statement's 'Cash at end of period'",
    "net_income": "net income (loss) for the period",
}


def build_messages(page_texts: list[tuple[int, str]], page_images: list[tuple[int, bytes]],
                   filename: str) -> list:
    fields = "\n".join(f'  - "{k}": {v}' for k, v in FIELD_GUIDE.items())
    instructions = f"""You read a portfolio company's own financial statements (profit and \
loss, balance sheet, statement of cash flows) for an investor's quarterly report.
File name: {filename}

Return a JSON object.
1. "fields": for each field below that the statements show, the printed line(s) that give it:
   {{"components": [{{"label": "<line label as printed>", "value": "<figure EXACTLY as printed>",
   "page": <page number>}}]}}. Copy figures verbatim (keep minus signs, parentheses,
   thousands separators); do not add, convert or compute. Every field is one line (the
   total) except capex, which lists each purchase line. Omit fields that aren't shown.
{fields}
2. Columns: use the column covering the WHOLE reporting period ("Total", "Three months
   ended ..."), never a single month, a prior year, a budget or a variance column. On the
   balance sheet use the period-end column.
3. Also return:
   "period_start", "period_end": the dates the figures cover (YYYY-MM-DD),
   "currency": ISO code (USD, EUR, ...),
   "scale": 1, or 1000 if the statements say "in thousands" / "$000", or 1000000 for millions,
   "company": the company's name if printed, else null,
   "notes": one short sentence on anything a reviewer should check.

JSON shape:
{{"fields": {{"revenue": {{"components": [{{"label": "...", "value": "...", "page": 3}}]}}}},
  "period_start": "...", "period_end": "...", "currency": "USD", "scale": 1,
  "company": null, "notes": "..."}}"""
    content: list = [{"type": "text", "text": instructions}]
    for pno, text in page_texts:
        content.append({"type": "text", "text": f"--- page {pno} (text) ---\n{text}"})
    for pno, png in page_images:
        content.append({"type": "text", "text": f"--- page {pno} (scanned image) ---"})
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(png).decode()}})
    return [{"role": "user", "content": content}]


def read_company_statements(page_texts: list[tuple[int, str]],
                            page_images: list[tuple[int, bytes]], filename: str) -> dict:
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
            fields[key] = [{"label": str(c.get("label") or key), "value": str(c["value"]),
                            "page": _page(c.get("page"))} for c in comps]
    try:
        scale = int(float(data.get("scale") or 1))
    except (TypeError, ValueError):
        scale = 1
    return {
        "fields": fields,
        "period_start": _s(data.get("period_start")),
        "period_end": _s(data.get("period_end")),
        "currency": _s(data.get("currency")),
        "scale": scale if scale in (1, 1000, 1_000_000) else 1,
        "company": _s(data.get("company")),
        "notes": _s(data.get("notes")),
    }


def _page(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _s(v) -> Optional[str]:
    return None if v in (None, "", "null") else str(v).strip()
