"""End-to-end smoke test (deterministic, no LLM).

Run:  python tests/test_e2e.py
Exits non-zero on any assertion failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "none")
# The tests never touch a real database or storage: they always run against a
# fresh throwaway SQLite file, whatever DATA_DIR / DATABASE_URL / blob storage
# the shell or a .env points at. Clearing real data is only ever done by the
# app's "Clear data" button.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="pfs-e2e-")
os.environ["DATABASE_URL"] = ""
os.environ["AZURE_STORAGE_ACCOUNT_URL"] = ""
os.environ["AZURE_STORAGE_CONNECTION_STRING"] = ""
os.environ["AUTH_MODE"] = "none"

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
    print(f"(test database: {settings.db_path})")
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

    roll_forward_continuity(samples)
    manual_period()
    statement_formats(samples)
    browser_caching()
    clear_data(samples)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def roll_forward_continuity(samples: Path) -> None:
    """Year-to-date PCAPs: each quarter follows on from the last, whatever the
    document prints, and whichever order the files arrive in."""
    from decimal import Decimal

    from app import carry_forward
    from app.extractors.base import normalize_period

    check(normalize_period("Fund 1 - 03-31-2026 results") == "2026-03-31",
          "m/d/y date with day > 12 parses (03-31-2026)")
    check(normalize_period("6/30/2026") == "2026-06-30", "day is not clamped to 28")

    q1 = samples / "Fund 99_2026 Q1 PCAP_Sample Holdings LP.pdf"
    q2 = samples / "Fund 99_2026 Q2 PCAP_Sample Holdings LP.pdf"
    if not q2.exists():
        import subprocess
        subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)

    # Q2 arrives first: no Q1 to roll from yet
    r2 = process_path(q2, allow_duplicate=True)
    check(r2["period"] == "2026-Q2", "YTD statement ending Jun 30 → 2026-Q2 (not H1/FY)")
    check(r2["n_facts"] >= 6, "PCAP without dot leaders parses deterministically")
    check(any("not on file" in w for w in r2["warnings"]), "missing Q1 is reported")

    r1 = process_path(q1, allow_duplicate=True)
    check(r1["period"] == "2026-Q1", "Q1 statement → 2026-Q1")

    def q2_facts() -> dict:
        return {f["metric_key"]: f for f in
                db.facts_for_quarter("Fund 99", carry_forward.RF, "2026-Q2")}

    f = q2_facts()
    val = lambda k: Decimal(f[k]["value"]) if k in f and f[k]["value"] is not None else None
    check(val("beginning_balance") == Decimal("452000"),
          "Q2 beginning recorded as Q1 ending (452,000), not the printed Jan 1 balance")
    check("carried_forward" in f["beginning_balance"]["flags"]
          and Decimal(f["beginning_balance"]["reported_value"]) == Decimal("500000"),
          "carried-forward beginning keeps the printed 500,000 and is flagged")
    check(val("return_of_capital") == 0, "YTD distributions de-cumulated (Q2 = 0)")
    check(val("investment_mtm") == Decimal("20000"), "YTD MTM de-cumulated (+20,000)")
    check(val("other") == Decimal("-1200"), "YTD fees/tax/carry summed and de-cumulated")
    check("funding" not in f and val("cumulative_contributions") == Decimal("550000"),
          "since-inception contributions never become period funding")
    check("reconciling_plug" not in f["other"]["flags"], "Q2 reconciles with no plug")
    check(f["beginning_balance"]["status"] == "needs_review", "derived values go to review")
    flows = sum(val(k) or 0 for k in carry_forward.FLOW_KEYS)
    check(val("beginning_balance") + flows == val("ending_balance"),
          "Q2 beginning + activity = ending")

    # a confirmed value is never re-derived; unconfirmed ones follow the prior quarter
    db.update_fact(f["beginning_balance"]["id"], status="confirmed", note="test")
    q1_end = next(x for x in db.facts_for_quarter("Fund 99", carry_forward.RF, "2026-Q1")
                  if x["metric_key"] == "ending_balance")
    db.update_fact(q1_end["id"], value="452500", status="confirmed", note="test restatement")
    carry_forward.rederive_following("Fund 99", "2026-Q1")
    f = q2_facts()
    check(Decimal(f["beginning_balance"]["value"]) == Decimal("452000"),
          "confirmed Q2 beginning untouched by a later Q1 edit")

    inv = db.get_investments()
    rows = [q for q in inv["quarters"] if q["entity_name"] == "Fund 99"]
    check([q["period_label"] for q in rows] == ["2026-Q1", "2026-Q2"],
          "roll-forward rows are consecutive calendar quarters")
    check([q["fiscal_label"] for q in rows] == ["FY2026 Q2", "FY2026 Q3"],
          "Jan–Mar 2026 = FY2026 Q2, Apr–Jun 2026 = FY2026 Q3 (FY ends September)")
    check(db.fiscal_info("2025-Q4")["fiscal_label"] == "FY2026 Q1"
          and db.fiscal_info("2026-Q3")["fiscal_label"] == "FY2026 Q4",
          "Oct–Dec 2025 = FY2026 Q1, Jul–Sep 2026 = FY2026 Q4")


def manual_period() -> None:
    """Reviewer corrects a submission's period from the review screen."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    sub = next(s for s in db.list_submissions()
               if s["entity_name"] == "Fund 99" and s["period"] == "2026-Q2")
    check(client.post(f"/api/submissions/{sub['id']}/period",
                      json={"period": "June"}).status_code == 400,
          "invalid period is refused")
    r = client.post(f"/api/submissions/{sub['id']}/period", json={"period": "2026-q3"}).json()
    facts_ = db.get_facts(sub["id"])
    check(r.get("period") == "2026-Q3" and r.get("fiscal_label") == "FY2026 Q4"
          and db.get_submission(sub["id"])["period"] == "2026-Q3"
          and all(f["period"] == "2026-Q3" and f["period_start"] == "2026-07-01"
                  for f in facts_),
          "period change re-dates the submission and all its values (FY2026 Q4)")
    check(any(a["field"] == "period" and a["old_value"] == "2026-Q2"
              and a["new_value"] == "2026-Q3" for a in db.get_audit(sub["id"])),
          "period change is in the audit trail")
    client.post(f"/api/submissions/{sub['id']}/period", json={"period": "2026-Q2"})
    rows = [q for q in db.get_investments()["quarters"] if q["entity_name"] == "Fund 99"]
    check([(q["period_label"], q["fiscal_label"]) for q in rows]
          == [("2026-Q1", "FY2026 Q2"), ("2026-Q2", "FY2026 Q3")],
          "moving it back restores the consecutive Q1 → Q2 roll-forward")


class _FakeModel:
    """Stands in for Azure OpenAI in CI: answers like the real model would."""
    name, supports_vision = "fake", True

    def __init__(self) -> None:
        self.mode, self.prompts = "investor", []

    def complete_json(self, messages: list) -> dict:
        self.prompts.append(messages[0]["content"][0]["text"])
        if self.mode == "fund_level":
            return {"document_kind": "fund_level", "fields": {},
                    "notes": "only partnership totals"}
        c = lambda label, value: {"components": [{"label": label, "value": value}]}
        return {"document_kind": "investor_statement",
                "investor_column": "Sample Holdings LP", "period_column": "Q1, 2026",
                "period_end": "2026-03-31", "period_start": "2026-01-01",
                "currency": "EUR", "number_format": "1,234.56",
                "fields": {
                    "funding": c("Called in Q1, 2026", "40,000"),
                    "investment_mtm": {"components": [
                        {"label": "Capital Loss Q1, 2026", "value": "(30,000)"},
                        {"label": "Capital FX Gain Q1, 2026", "value": "10,000"}]},
                    "other": c("GP Share and Other Expenses Q1, 2026", "(5,000)"),
                    "ending_balance": c("Capital Accounts at Q1", "2,015,000"),
                    "commitment": c("Total committed", "2,000,000"),
                    "cumulative_contributions": c("Total Capital Contributions called",
                                                  "2,040,000")}}


def statement_formats(samples: Path) -> None:
    """Statements in layouts other than the ones the parser was built around."""
    from decimal import Decimal

    from app.extractors import registry
    from app.extractors.base import detect_statement_dates
    from app.llm import capital_account
    from app.models import detect_decimal_comma, parse_number

    # European numbers, decided per document
    eu = "Investments at value 420.318.775 Cash 8.214.009 Management fee 612.500 12,35%"
    check(detect_decimal_comma(eu) and not detect_decimal_comma("Total 2,468,135 (51,790)"),
          "document number style detected (1.234.567 vs 1,234,567)")
    check(parse_number("612.500", True)[0] == 612500 and parse_number("-381.240", True)[0] == -381240
          and parse_number("12,35%", True)[0] == Decimal("12.35")
          and parse_number("420.318.775")[0] == 420318775,
          "European-format values parse to the right magnitude")
    # date forms seen in real statements
    check(detect_statement_dates("", "Fund 3 Capital account Mar 31 2026 - LP.pdf").quarter == "2026-Q1"
          and detect_statement_dates("3 months to 31-Mar-26", "x.pdf").quarter == "2026-Q1"
          and detect_statement_dates("Quarterly report –31 March 2026", "x.pdf").quarter == "2026-Q1"
          and detect_statement_dates("", "Fund 8_Q1_2026_Redacted.pdf").quarter == "2026-Q1",
          "'Mar 31 2026', '31-Mar-26', '31 March 2026', 'Q1_2026' all → 2026-Q1")

    lp = samples / "Fund 77_Q1 2026 Statement_Sample Holdings LP.pdf"
    if not lp.exists():
        import subprocess
        subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)

    # without a model: the two-column lines are flagged, not trusted
    r = process_path(lp, allow_duplicate=True)
    facts_ = db.get_facts(r["submission_id"])
    check(r["report_type"] == "investment_roll_forward" and r["period"] == "2026-Q1",
          "LP statement recognised as a roll-forward for 2026-Q1")
    check(facts_ and all(any(fl.startswith("multi_column") for fl in f["flags"])
                         and f["status"] == "needs_review" for f in facts_),
          "two-column picks are flagged for review without a model")

    # with a model: our investor's column and the quarter's lines
    fake = _FakeModel()
    saved = (settings.llm_provider, registry.get_provider, capital_account.get_provider)
    settings.llm_provider = "azure"
    registry.get_provider = capital_account.get_provider = lambda: fake
    try:
        r = process_path(lp, allow_duplicate=True)
        by = {f["metric_key"]: f for f in db.get_facts(r["submission_id"])}
        v = lambda k: Decimal(by[k]["value"]) if k in by and by[k]["value"] is not None else None
        check("Sample Holdings LP" in fake.prompts[-1] or "Lam Research" in fake.prompts[-1],
              "model is told which investor is ours (LP_NAMES)")
        check(r["report_type"] == "investment_roll_forward" and by["funding"]["currency"] == "EUR"
              and v("funding") == 40000 and v("investment_mtm") == -20000
              and v("other") == -5000 and v("ending_balance") == 2015000,
              "model reading: investor column, quarter lines, components summed by code")
        check("parser_agrees" in by["other"]["flags"]
              and all("llm_suggested" in f["flags"] and f["status"] == "needs_review"
                      for k, f in by.items() if k != "beginning_balance"),
              "model values are suggestions; parser agreement is recorded")
        check(v("beginning_balance") == 2000000
              and "derived_from_activity" in by["beginning_balance"]["flags"],
              "unprinted opening balance = ending − quarter activity (flagged)")
        check(v("cumulative_contributions") == 2040000 and "funding" in by
              and v("funding") != 2040000, "cumulative total kept out of quarter funding")

        n_calls = len(fake.prompts)
        process_path(samples / "Fund 99_2026 Q1 PCAP_Sample Holdings LP.pdf", allow_duplicate=True)
        check(len(fake.prompts) == n_calls,
              "a statement the parser reads completely never calls the model")

        fake.mode = "fund_level"
        r = process_path(lp, allow_duplicate=True)
        check(r["report_type"] == "fund_financials"
              and any("fund-level" in w for w in r["warnings"]),
              "fund-level reports stay out of the investor's roll-forward, with a note")
    finally:
        settings.llm_provider, registry.get_provider, capital_account.get_provider = saved


def browser_caching() -> None:
    """New HTML must never run against a browser's cached old JavaScript."""
    import re

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    page = client.get("/")
    check(page.headers.get("cache-control") == "no-cache", "HTML is revalidated on every load")
    srcs = re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text)
    check(srcs and all("?v=" in u for u in srcs), "every static asset URL is versioned")
    js = client.get(next(u for u in srcs if "app.js" in u))
    check(js.status_code == 200 and "immutable" in js.headers.get("cache-control", ""),
          "versioned assets are cached long-term")
    check(client.get("/static/js/app.js").headers.get("cache-control") == "no-cache",
          "unversioned asset URLs are revalidated")


def clear_data(samples: Path) -> None:
    """The header's "Clear data" button: empties the data, keeps the audit trail."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    n_audit = len(db.get_audit(limit=100000))
    check(client.post("/api/admin/clear", json={}).status_code == 400,
          "clear refuses without the typed confirmation")
    check(db.list_submissions() != [], "nothing cleared by a refused request")

    res = client.post("/api/admin/clear", json={"confirm": "CLEAR"})
    body = res.json()
    check(res.status_code == 200 and body["cleared_submissions"] > 0, "clear succeeds")
    check(db.list_submissions() == [] and db.get_facts() == []
          and db.get_investments() == {"entities": [], "quarters": []},
          "submissions, facts and investment views are empty")
    audit_rows = db.get_audit(limit=100000)
    check(len(audit_rows) == n_audit + 1 and audit_rows[0]["action"] == "clear_data",
          "audit trail kept, with a clear_data entry")
    check(body["backup"] and (settings.data_dir / "backups" / body["backup"]).exists(),
          "a backup snapshot is saved before clearing")
    r = process_path(samples / "fund_financials_q4.xlsx")
    check(r["status"] == "ok", "a previously uploaded file can be uploaded again")


if __name__ == "__main__":
    sys.exit(main())
