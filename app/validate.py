"""Validation rules — the backbone of the accuracy guarantee.

These run after extraction. Any fact that participates in a failed check is
downgraded to `needs_review` (never silently trusted). Checks are deterministic
arithmetic reconciliations, not model calls.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .config import settings
from .models import FactStatus, FinancialFact


class Check:
    def __init__(self, name: str, ok: bool, detail: str, severity: str = "warn"):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.severity = severity  # "error" | "warn" | "info"

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "severity": self.severity}


def _by_key(facts: list[FinancialFact]) -> dict[str, FinancialFact]:
    return {f.metric_key: f for f in facts if f.value is not None}


def _flag(facts: list[FinancialFact], keys: list[str], msg: str) -> None:
    for f in facts:
        if f.metric_key in keys:
            if msg not in f.flags:
                f.flags.append(msg)
            if f.status == FactStatus.AUTO:
                f.status = FactStatus.NEEDS_REVIEW


def validate_facts(report_type: str, facts: list[FinancialFact]) -> list[Check]:
    checks: list[Check] = []
    m = _by_key(facts)
    tol = Decimal(str(settings.reconcile_tolerance))

    # --- required-fields present ---
    from .report_types import REPORT_TYPES
    rt = REPORT_TYPES.get(report_type)
    if rt:
        present = {f.metric_key for f in facts if f.value is not None}
        for spec in rt.metrics:
            if spec.required and spec.key not in present:
                checks.append(Check(
                    f"required:{spec.key}", False,
                    f"Required metric '{spec.label}' not found.", "error"))

    # --- ownership % sanity ---
    if "ownership_pct" in m:
        v = m["ownership_pct"].value
        ok = Decimal(0) <= v <= Decimal(100)
        checks.append(Check("ownership_range", ok,
                            f"Ownership = {v}{'%' if ok else ' (outside 0–100!)'}",
                            "info" if ok else "error"))
        if not ok:
            _flag(facts, ["ownership_pct"], "ownership_out_of_range")

    # --- fund balance sheet: assets ≈ liabilities + net assets ---
    if {"total_assets", "total_liabilities", "nav"} <= set(m):
        a = m["total_assets"].value
        diff = a - (m["total_liabilities"].value + m["nav"].value)
        ok = abs(diff) <= tol
        checks.append(Check(
            "balance_sheet", ok,
            f"Assets − (Liabilities + Net Assets) = {diff}"
            + ("" if ok else "  ← does not reconcile"),
            "info" if ok else "error"))
        if not ok:
            _flag(facts, ["total_assets", "total_liabilities", "nav"],
                  "balance_sheet_mismatch")

    # --- fund: total income − total expenses ≈ net income ---
    if {"total_income", "total_expenses", "net_income"} <= set(m):
        diff = (m["total_income"].value - m["total_expenses"].value) - m["net_income"].value
        ok = abs(diff) <= tol
        checks.append(Check("income_statement", ok,
                            f"(Income − Expenses) − Net Income = {diff}",
                            "info" if ok else "warn"))
        if not ok:
            _flag(facts, ["net_income"], "income_statement_mismatch")

    # --- equity method: equity_income ≈ ownership% × JV net income ---
    if {"equity_income", "ownership_pct", "jv_net_income"} <= set(m):
        expected = m["jv_net_income"].value * (m["ownership_pct"].value / Decimal(100))
        got = m["equity_income"].value
        # tolerance scales with size (rounding, minority adjustments)
        rel_tol = max(tol, abs(expected) * Decimal("0.02"))
        ok = abs(got - expected) <= rel_tol
        checks.append(Check(
            "equity_income_recompute", ok,
            f"Equity income {got} vs ownership×JV-NI {expected} "
            f"(Δ {got - expected})",
            "info" if ok else "warn"))
        if not ok:
            _flag(facts, ["equity_income"], "equity_income_recompute_mismatch")

    # --- direct investment: unrealized ≈ fair value − cost ---
    if {"fair_value", "cost_basis", "unrealized_gain"} <= set(m):
        expected = m["fair_value"].value - m["cost_basis"].value
        diff = m["unrealized_gain"].value - expected
        ok = abs(diff) <= tol
        checks.append(Check("unrealized_recompute", ok,
                            f"Unrealized {m['unrealized_gain'].value} vs "
                            f"(FV − Cost) {expected} (Δ {diff})",
                            "info" if ok else "warn"))
        if not ok:
            _flag(facts, ["unrealized_gain"], "unrealized_recompute_mismatch")

    # --- direct investment: MOIC ≈ fair value / cost ---
    if {"moic", "fair_value", "cost_basis"} <= set(m) and m["cost_basis"].value:
        expected = m["fair_value"].value / m["cost_basis"].value
        ok = abs(m["moic"].value - expected) <= Decimal("0.05")
        checks.append(Check("moic_recompute", ok,
                            f"MOIC {m['moic'].value} vs FV/Cost {expected:.3f}",
                            "info" if ok else "warn"))
        if not ok:
            _flag(facts, ["moic"], "moic_recompute_mismatch")

    # --- investment roll-forward: beginning + activity ≈ ending ---
    if {"beginning_balance", "ending_balance"} <= set(m):
        beg = m["beginning_balance"].value
        end = m["ending_balance"].value
        implied_change = end - beg
        # (a) an explicit "change" line ideally equals end−beg — but note some
        #     statements report change as a subtotal that EXCLUDES capital flows
        #     (contributions/distributions), so a mismatch is a warning, not an error.
        if "change" in m:
            ok = abs(m["change"].value - implied_change) <= tol
            checks.append(Check(
                "roll_forward_change", ok,
                f"Reported change {m['change'].value} vs (Ending − Beginning) "
                f"{implied_change} (Δ {m['change'].value - implied_change})"
                + ("" if ok else "  ← differs; may exclude capital flows — review"),
                "info" if ok else "warn"))
            if not ok:
                _flag(facts, ["change"], "roll_forward_change_review")
        # (b) if we mapped the activity lines, their sum should equal the change
        activity_keys = ["funding", "equity_income_loss", "return_of_capital",
                         "dividends", "other", "investment_mtm", "sale_of_investment",
                         "net_realized_gain", "accrued_interest", "note_conversion",
                         "gain_loss_on_conversion", "acquisition_impact", "impairment"]
        present_activity = [k for k in activity_keys if k in m]
        if present_activity:
            activity_sum = sum((m[k].value for k in present_activity), Decimal(0))
            # scale tolerance with size — big books round at the dollar/thousand level
            rel_tol = max(tol, abs(implied_change) * Decimal("0.01"))
            ok = abs(activity_sum - implied_change) <= rel_tol
            checks.append(Check(
                "roll_forward_activity", ok,
                f"Sum of activity {activity_sum} vs (Ending − Beginning) "
                f"{implied_change} (Δ {activity_sum - implied_change})"
                + ("" if ok else "  ← some activity lines may be unmapped"),
                "info" if ok else "warn"))
            if not ok:
                _flag(facts, present_activity, "roll_forward_activity_mismatch")

    # --- currency consistency ---
    currencies = {f.currency for f in facts if f.value is not None}
    if len(currencies) > 1:
        checks.append(Check("currency_consistency", False,
                            f"Multiple currencies detected: {sorted(currencies)}",
                            "warn"))

    # --- LLM-sourced values always need review ---
    llm_facts = [f for f in facts if "llm_suggested" in f.flags]
    if llm_facts:
        checks.append(Check("llm_suggestions", False,
                            f"{len(llm_facts)} value(s) suggested by local LLM — "
                            "confirm against source.", "warn"))

    return checks
