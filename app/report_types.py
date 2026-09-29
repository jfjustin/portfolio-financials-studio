"""Canonical report types and their metric definitions.

This is the domain heart of the pipeline. Every report — whether it arrives as a
standard internal Excel template or a varied company PDF — is normalized into a
set of `FinancialFact` rows keyed by (entity, period, report_type, metric_key).

Each metric carries synonyms so that a label found in a document
("Net Asset Value", "NAV", "Total equity") maps deterministically to one
canonical key. Anything we can't confidently map is surfaced for human review
rather than silently dropped or guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Unit(str, Enum):
    CURRENCY = "currency"
    PERCENT = "percent"
    COUNT = "count"
    RATIO = "ratio"
    TEXT = "text"


@dataclass(frozen=True)
class MetricSpec:
    key: str                      # canonical machine key, e.g. "nav"
    label: str                    # human label, e.g. "Net Asset Value"
    unit: Unit = Unit.CURRENCY
    synonyms: tuple[str, ...] = ()  # lowercase substrings that map to this metric
    # role in reconciliation checks (see validate.py):
    #   "total"      -> should equal sum of its components
    #   "component"  -> part of a total (component_of names the total key)
    role: str = ""
    component_of: str = ""
    required: bool = False        # if a report of this type should always contain it


@dataclass(frozen=True)
class ReportType:
    key: str
    label: str
    entity_kind: str              # fund | joint_venture | portfolio_company | direct_investment
    metrics: tuple[MetricSpec, ...]
    # filename / header keywords that hint a file is of this report type
    hints: tuple[str, ...] = ()

    def metric_by_key(self, key: str) -> MetricSpec | None:
        for m in self.metrics:
            if m.key == key:
                return m
        return None


# --------------------------------------------------------------------------
# 1) FUND FINANCIALS
# --------------------------------------------------------------------------
FUND_FINANCIALS = ReportType(
    key="fund_financials",
    label="Fund Financials",
    entity_kind="fund",
    hints=("fund financial", "statement of assets", "statement of operations",
           "schedule of investments", "nav", "net asset value", "capital account"),
    metrics=(
        MetricSpec("total_assets", "Total Assets", Unit.CURRENCY,
                   ("total assets",), role="total", required=True),
        MetricSpec("total_liabilities", "Total Liabilities", Unit.CURRENCY,
                   ("total liabilities",), required=True),
        MetricSpec("nav", "Net Asset Value (Net Assets)", Unit.CURRENCY,
                   ("net asset value", "net assets", "total net assets", "nav",
                    "partners' capital", "members' equity"), required=True),
        MetricSpec("capital_called", "Capital Called / Contributions", Unit.CURRENCY,
                   ("capital called", "contributions", "paid-in capital",
                    "drawn capital", "called capital")),
        MetricSpec("distributions", "Distributions", Unit.CURRENCY,
                   ("distributions", "distributed to partners")),
        MetricSpec("management_fees", "Management Fees", Unit.CURRENCY,
                   ("management fee",)),
        MetricSpec("net_income", "Net Income / (Loss)", Unit.CURRENCY,
                   ("net income", "net profit", "net loss", "net increase in net assets")),
        MetricSpec("realized_gain", "Realized Gain / (Loss)", Unit.CURRENCY,
                   ("realized gain", "realized loss", "net realized")),
        MetricSpec("unrealized_gain", "Unrealized Gain / (Loss)", Unit.CURRENCY,
                   ("unrealized gain", "unrealized loss", "change in unrealized",
                    "net change in unrealized")),
        MetricSpec("total_income", "Total Investment Income", Unit.CURRENCY,
                   ("total income", "total investment income", "investment income")),
        MetricSpec("total_expenses", "Total Expenses", Unit.CURRENCY,
                   ("total expenses", "total operating expenses")),
    ),
)

# --------------------------------------------------------------------------
# 2) JOINT VENTURE (balance-sheet / P&L level for a JV entity)
# --------------------------------------------------------------------------
JOINT_VENTURE = ReportType(
    key="joint_venture",
    label="Joint Venture Financials",
    entity_kind="joint_venture",
    hints=("joint venture", "jv financial", "jv balance", "jv income",
           "ownership", "our share", "equity interest"),
    metrics=(
        MetricSpec("ownership_pct", "Ownership %", Unit.PERCENT,
                   ("ownership", "our share", "equity interest", "% held",
                    "percentage held", "interest held"), required=True),
        MetricSpec("jv_total_assets", "JV Total Assets", Unit.CURRENCY,
                   ("total assets",)),
        MetricSpec("jv_total_liabilities", "JV Total Liabilities", Unit.CURRENCY,
                   ("total liabilities",)),
        MetricSpec("jv_total_equity", "JV Total Equity", Unit.CURRENCY,
                   ("total equity", "net equity", "shareholders' equity",
                    "members' equity")),
        MetricSpec("jv_revenue", "JV Revenue", Unit.CURRENCY,
                   ("revenue", "total revenue", "turnover", "sales")),
        MetricSpec("jv_net_income", "JV Net Income / (Loss)", Unit.CURRENCY,
                   ("net income", "net profit", "net loss", "profit for the",
                    "loss for the"), required=True),
        MetricSpec("carrying_value", "Carrying Value of Investment", Unit.CURRENCY,
                   ("carrying value", "carrying amount", "investment in jv",
                    "equity method investment", "book value of investment")),
    ),
)

# --------------------------------------------------------------------------
# 3) JV EQUITY INCOME / (LOSS)  — our recognized share under equity method
# --------------------------------------------------------------------------
JV_EQUITY_INCOME = ReportType(
    key="jv_equity_income",
    label="JV Equity Income / (Loss)",
    entity_kind="joint_venture",
    hints=("equity income", "equity in earnings", "equity in net", "share of profit",
           "share of loss", "equity method", "equity pickup", "equity pick-up"),
    metrics=(
        MetricSpec("ownership_pct", "Ownership %", Unit.PERCENT,
                   ("ownership", "our share", "% held", "equity interest"), required=True),
        MetricSpec("jv_net_income", "JV 100% Net Income / (Loss)", Unit.CURRENCY,
                   ("net income", "net profit", "net loss", "profit for", "loss for"),
                   role="total"),
        MetricSpec("equity_income", "Equity Income / (Loss) — Our Share", Unit.CURRENCY,
                   ("equity income", "equity in earnings", "equity in net income",
                    "share of profit", "share of loss", "equity pickup",
                    "equity pick-up", "our share of", "equity in net loss"),
                   role="component", component_of="jv_net_income", required=True),
        MetricSpec("dividends_received", "Dividends / Distributions Received", Unit.CURRENCY,
                   ("dividends received", "distributions received", "cash received")),
        MetricSpec("carrying_value", "Investment Carrying Value", Unit.CURRENCY,
                   ("carrying value", "carrying amount", "closing balance",
                    "investment balance")),
    ),
)

# --------------------------------------------------------------------------
# 4) DIRECT INVESTMENT
# --------------------------------------------------------------------------
DIRECT_INVESTMENT = ReportType(
    key="direct_investment",
    label="Direct Investment",
    entity_kind="direct_investment",
    hints=("direct investment", "portfolio company", "cost basis", "fair value",
           "invested capital", "holding"),
    metrics=(
        MetricSpec("cost_basis", "Cost / Invested Capital", Unit.CURRENCY,
                   ("cost", "cost basis", "invested capital", "acquisition cost",
                    "amount invested"), required=True),
        MetricSpec("fair_value", "Fair Value", Unit.CURRENCY,
                   ("fair value", "fair market value", "current value",
                    "estimated value", "valuation"), required=True),
        MetricSpec("ownership_pct", "Ownership %", Unit.PERCENT,
                   ("ownership", "% held", "fully diluted", "stake")),
        MetricSpec("unrealized_gain", "Unrealized Gain / (Loss)", Unit.CURRENCY,
                   ("unrealized gain", "unrealized loss", "unrealized"),
                   role="total"),
        MetricSpec("realized_proceeds", "Realized Proceeds", Unit.CURRENCY,
                   ("realized proceeds", "proceeds", "exit value", "realized value")),
        MetricSpec("moic", "MOIC", Unit.RATIO,
                   ("moic", "multiple", "gross multiple", "tvpi")),
        MetricSpec("irr", "IRR", Unit.PERCENT,
                   ("irr", "internal rate of return")),
    ),
)


# --------------------------------------------------------------------------
# 5) INVESTMENT ROLL-FORWARD (capital-account / "Summary for BSR" quarterly roll)
#    beginning balance -> activity lines -> ending balance. This is the shape of
#    partner-capital-account statements and internal investment roll-forwards
#    (e.g. the "JV Equity Income/(Loss) Fund" capital-account workbooks). Column
#    letters mirror the standard summary workbook layout.
# --------------------------------------------------------------------------
INVESTMENT_ROLL_FORWARD = ReportType(
    key="investment_roll_forward",
    label="Investment Roll-Forward",
    entity_kind="fund",
    hints=("roll forward", "rollforward", "capital account", "capital account statement",
           "beginning capital", "ending capital account balance", "summary for bsr",
           "changes in capital", "changes in net assets", "partners' capital",
           "change in capital balance", "equity income"),
    metrics=(
        MetricSpec("beginning_balance", "Beginning Balance", Unit.CURRENCY,
                   ("beginning capital account balance", "beginning balance",
                    "opening balance", "beginning partner's capital",
                    "partners' capital, beginning", "beginning of the period",
                    "beginning of period", "opening value"),
                   role="total", required=True),
        MetricSpec("funding", "Funding / Contributions", Unit.CURRENCY,
                   ("capital contributions", "capital contribution", "capital called",
                    "capital call", "contributions by partner", "paid-in capital",
                    "invested capital", "funding")),
        MetricSpec("equity_income_loss", "Equity Income / (Loss)", Unit.CURRENCY,
                   ("equity income", "equity income / (loss)", "net investment income",
                    "net operating gain", "net operating loss", "net surplus",
                    "change in net assets resulting from operations",
                    "profit/(loss) for the period")),
        MetricSpec("return_of_capital", "Distributions — Return of Capital", Unit.CURRENCY,
                   ("return of capital", "return of investment principal",
                    "non-recallable distribution", "recallable distribution",
                    "capital distributions", "capital distribution")),
        MetricSpec("dividends", "Distributions — Dividends", Unit.CURRENCY,
                   ("dividend income", "dividends", "income distribution")),
        MetricSpec("other", "Other", Unit.CURRENCY,
                   ("other income", "other expenses", "foreign currency gain",
                    "foreign exchange gain", "foreign exchange loss", "management fees",
                    "management fee", "carried interest", "unrealized carried interest",
                    "legal accounting and professional fees",
                    "legal, accounting and professional fees", "tax expenses",
                    "transaction related expenses", "provision for foreign witholding tax",
                    "foreign withholding tax")),
        MetricSpec("investment_mtm", "Investment MTM", Unit.CURRENCY,
                   ("unrealized appreciation", "unrealised appreciation",
                    "unrealized depreciation", "unrealised depreciation",
                    "unrealized appreciation (depreciation) on investments",
                    "net change in fair value", "investment valuation movements",
                    "revaluation reserve", "investment mtm")),
        MetricSpec("sale_of_investment", "Sale of Investment", Unit.CURRENCY,
                   ("sale of investment", "proceeds from sale",
                    "realized gain on investments", "realised gain on investments",
                    "realized loss on investments", "realised loss on investments")),
        MetricSpec("net_realized_gain", "Net Realized Gain / (Loss)", Unit.CURRENCY,
                   ("net realized gain (loss) from investments", "net realized gain",
                    "net realized loss", "net realized")),
        MetricSpec("accrued_interest", "Accrued Interest", Unit.CURRENCY,
                   ("accrued interest", "interest receivable", "interest income")),
        MetricSpec("note_conversion", "Note Conversion", Unit.CURRENCY,
                   ("note conversion", "convertible note conversion", "conversion of note")),
        MetricSpec("gain_loss_on_conversion", "Gain / (Loss) on Conversion", Unit.CURRENCY,
                   ("gain on conversion", "loss on conversion", "conversion gain",
                    "conversion loss")),
        MetricSpec("acquisition_impact", "Acquisition Impact", Unit.CURRENCY,
                   ("acquisition impact", "purchase accounting adjustment")),
        MetricSpec("impairment", "Impairment", Unit.CURRENCY,
                   ("impairment", "write-down", "write off", "write-off")),
        MetricSpec("change", "Change in Capital Balance", Unit.CURRENCY,
                   ("change in capital balance", "change in net assets", "net change",
                    "changes during the period", "change in capital balance (sum of a)")),
        MetricSpec("ending_balance", "Ending Balance", Unit.CURRENCY,
                   ("ending capital account balance", "ending balance", "closing value",
                    "closing balance", "ending partner's capital",
                    "capital balance as of", "capital account at fair value",
                    "net assets attributable to limited partner"),
                   role="total", required=True),
    ),
)


REPORT_TYPES: dict[str, ReportType] = {
    rt.key: rt
    for rt in (FUND_FINANCIALS, JOINT_VENTURE, JV_EQUITY_INCOME, DIRECT_INVESTMENT,
               INVESTMENT_ROLL_FORWARD)
}


def all_report_types() -> list[ReportType]:
    return list(REPORT_TYPES.values())


def guess_report_type(text_blob: str, filename: str = "") -> tuple[str, float]:
    """Score the document text + filename against each report type's hints.

    Returns (report_type_key, confidence 0..1). Confidence is deliberately
    conservative; a low score routes the file to the human to confirm the type.
    """
    hay = f"{filename}\n{text_blob}".lower()
    # Weight each hint by specificity (word count): distinctive multi-word phrases
    # like "equity in earnings" outweigh generic single words like "ownership".
    scores: dict[str, float] = {}
    for rt in REPORT_TYPES.values():
        scores[rt.key] = sum(len(h.split()) for h in rt.hints if h in hay)
    if not scores or max(scores.values()) == 0:
        return ("fund_financials", 0.0)  # default; 0.0 confidence -> human confirms
    best = max(scores, key=scores.get)
    total = sum(scores.values()) or 1
    # confidence = share of weighted hint-hits going to the winner, capped
    conf = min(0.9, scores[best] / total)
    return (best, round(conf, 3))
