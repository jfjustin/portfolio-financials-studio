"""End-to-end smoke test (deterministic, no LLM).

Run:  python tests/test_e2e.py
Exits non-zero on any assertion failure.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "none")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                     # noqa: E402
from app.config import settings        # noqa: E402
from app.pipeline import process_path  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    # fresh DB
    if settings.db_path.exists():
        settings.db_path.unlink()
    db.init_db()

    samples = ROOT / "samples"
    if not (samples / "fund_financials_q4.xlsx").exists():
        import subprocess
        subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)

    # --- fund financials: balance sheet & income statement reconcile ---
    r = process_path(samples / "fund_financials_q4.xlsx", allow_duplicate=True)
    check(r["report_type"] == "fund_financials", "fund → fund_financials")
    checks = {c["name"]: c["ok"] for c in r["checks"]}
    check(checks.get("balance_sheet"), "fund balance sheet reconciles")
    check(checks.get("income_statement"), "fund income statement reconciles")

    # --- JV holdings ---
    r = process_path(samples / "jv_holdings.xlsx", allow_duplicate=True)
    check(r["report_type"] == "joint_venture", "jv → joint_venture")
    check(r["entity_name"].startswith("Harbor"), "jv entity name detected")

    # --- direct investment: unrealized & MOIC recompute ---
    r = process_path(samples / "direct_investment.xlsx", allow_duplicate=True)
    check(r["report_type"] == "direct_investment", "direct → direct_investment")
    checks = {c["name"]: c["ok"] for c in r["checks"]}
    check(checks.get("unrealized_recompute"), "unrealized = FV − cost")
    check(checks.get("moic_recompute"), "MOIC = FV / cost")

    # --- native PDF: equity income recompute ---
    r = process_path(samples / "jv_equity_income.pdf", allow_duplicate=True)
    check(r["extractor"] == "pdf_native", "pdf uses native extractor")
    check(r["report_type"] == "jv_equity_income", "pdf → jv_equity_income")
    checks = {c["name"]: c["ok"] for c in r["checks"]}
    check(checks.get("equity_income_recompute"),
          "equity income = ownership × JV net income")

    # --- duplicate guard ---
    dup = process_path(samples / "fund_financials_q4.xlsx")
    check(dup["status"] == "duplicate", "duplicate file is rejected")

    # --- audit trail exists ---
    check(len(db.get_audit(limit=5)) > 0, "audit rows written")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
