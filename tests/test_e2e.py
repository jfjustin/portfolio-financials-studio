"""End-to-end smoke test (deterministic, no LLM).

Run:  python tests/test_e2e.py
Exits non-zero on any assertion failure.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from decimal import Decimal
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
os.environ["LAKE_BACKEND"] = "none"          # lake_flow() turns on a local lake itself
os.environ["LAKE_AUTO_SYNC"] = "false"       # tests sync explicitly

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
    format_memory(samples)
    fund_level_pro_rata(samples)
    currency_controls()
    lake_flow(samples)
    browser_caching()
    deliverable_helpers()
    direct_investment_financials(samples)
    investment_portfolio(samples)
    manual_entry(samples)
    portfolio_summary()
    pdf_reading(samples)
    easy_auth_user()
    password_gate()
    master_workbook()
    equity_investments_workbook()
    currencies_and_fx()
    accounting_tabs_cash_flows()
    accounting_workbook_is_the_record()
    repeated_rows()
    xirr_roots()
    irr_section()
    irr_windows_and_precedence()
    period_ranges()
    deletes_and_odd_names()
    history_section()
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
    picked = [f for f in facts_ if "derived_from_activity" not in f["flags"]]
    check(picked and all(any(fl.startswith("multi_column") for fl in f["flags"])
                         and f["status"] == "needs_review" for f in picked),
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


def format_memory(samples: Path) -> None:
    """The app learns a fund's statement wording from review and reuses it —
    for the parser (no model needed) and as context for the model."""
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from app import carry_forward
    from app.extractors import registry
    from app.llm import capital_account
    from app.main import app

    client = TestClient(app)
    q1 = samples / "Fund 78_Q1 2026 Statement_Sample Holdings LP.pdf"
    q2 = samples / "Fund 78_Q2 2026 Statement_Sample Holdings LP.pdf"
    if not q2.exists():
        import subprocess
        subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)

    class Model(_FakeModel):
        """Reads Q1 like a model would — and misses one expense line."""
        def complete_json(self, messages: list) -> dict:
            self.prompts.append(messages[0]["content"][0]["text"])
            c = lambda sec, label, value: {"components": [
                {"section": sec, "label": label, "value": value}]}
            return {"document_kind": "investor_statement",
                    "investor_column": "Sample Holdings LP", "period_column": "Q1, 2026",
                    "period_end": "2026-03-31", "currency": "EUR",
                    "number_format": "1,234.56", "fields": {
                        "funding": c("", "Called in Q1, 2026", "20,000"),
                        "accrued_interest": c("Income", "Equalisation interest Q1, 2026", "500"),
                        "investment_mtm": c("Unrealised Capital Gains", "Capital Loss Q1, 2026",
                                            "(10,000)"),
                        "other": c("GP Share and Other Expenses (Net of income)",
                                   "GP Share and Other Expenses Q1, 2026", "(2,000)"),
                        "ending_balance": c("", "Capital Accounts at Q1", "1,007,500")}}

    fake = Model()
    saved = (settings.llm_provider, registry.get_provider, capital_account.get_provider)

    def use_model(on: bool) -> None:
        settings.llm_provider = "azure" if on else "none"
        registry.get_provider = capital_account.get_provider = (lambda: fake) if on else saved[1]

    try:
        # Q1: read by the model; a reviewer teaches the line it missed, then confirms
        use_model(True)
        r1 = process_path(q1, allow_duplicate=True)
        use_model(False)
        sub1 = r1["submission_id"]
        unm = {u["label"]: u for u in json.loads(db.get_submission(sub1)["unmapped"])}
        miss = unm.get("Fund expenses allocated Q1, 2026")
        check(miss is not None and miss["section"].startswith("GP Share"),
              "missed line is offered for teaching, with its heading")
        res = client.post(f"/api/submissions/{sub1}/teach", json={
            "label": miss["label"], "section": miss["section"], "value_raw": miss["value_raw"],
            "metric_key": "other", "scope": "fund"})
        by = {f["metric_key"]: f for f in db.get_facts(sub1)}
        check(res.status_code == 200 and Decimal(by["other"]["value"]) == -3000
              and "Fund expenses allocated Q1, 2026" not in
              {u["label"] for u in json.loads(db.get_submission(sub1)["unmapped"])},
              "teaching records the value now (Other −2,000 → −3,000)")
        check(Decimal(by["beginning_balance"]["value"]) == 1000000,
              "quarter re-derived after teaching (opening balance 1,000,000)")
        for f in db.get_facts(sub1):
            if f["status"] != "confirmed":
                db.update_fact(f["id"], status="confirmed", note="test review")

        mem = client.get("/api/funds/Fund 78/memory").json()
        check(any(e["label"] == "Equalisation interest Q1, 2026"
                  and e["metric_key"] == "accrued_interest" for e in mem["learned"])
              and mem["columns"][0]["investor"] == "Sample Holdings LP"
              and mem["currency"] == "EUR",
              "confirmed values become this fund's format memory")

        # Q2, no model: the parser alone reads it from what Q1 taught
        r2 = process_path(q2, allow_duplicate=True)
        by = {f["metric_key"]: f for f in db.get_facts(r2["submission_id"])}
        v = lambda k: Decimal(by[k]["value"]) if k in by and by[k]["value"] is not None else None
        check(v("accrued_interest") == 700 and "learned" in by["accrued_interest"]["provenance"],
              "Q2: wording learned from Q1's confirmed values (Equalisation interest)")
        check(v("other") == -3000 and "taught_fund" in by["other"]["provenance"],
              "Q2: wording taught in review is reused (Other = both expense lines)")
        check(v("investment_mtm") == 15000 and v("funding") == 30000,
              "Q2: new quarter numbers and Loss→Gain rewording still recognised")
        check(v("beginning_balance") == 1007500 and v("ending_balance") == 1050200
              and "reconciling_plug" not in by.get("other", {}).get("flags", []),
              "Q2 follows on from Q1 and reconciles — no model needed")

        # Q2 with the model: it is shown what the app knows about this fund
        client.post("/api/funds/Fund 78/profile",
                    json={"investor_aliases": "Shareholder Q", "notes": "figures in EUR"})
        use_model(True)
        process_path(q2, allow_duplicate=True)
        use_model(False)
        prompt = fake.prompts[-1]
        check("Equalisation interest Q1, 2026" in prompt and "Fund expenses allocated" in prompt
              and "last confirmed quarter 2026-Q1" in prompt and "1,007,500" in prompt
              and '"Sample Holdings LP"' in prompt,
              "model prompt carries the fund's memory (lines, column, last quarter)")
        check("Shareholder Q" in prompt and "figures in EUR" in prompt,
              "fund parameters (investor alias, notes) reach the model")

        # terms API: add, list, forget
        tid = client.post("/api/terms", json={"label": "Placement adjustment Q1, 2026",
                                              "metric_key": "ignore"}).json()["id"]
        listed = client.get("/api/terms").json()
        check(any(t["id"] == tid for t in listed["taught"]) and len(listed["presets"]) > 40,
              "terms API lists pre-loaded and taught wording")
        check(client.delete(f"/api/terms/{tid}").status_code == 200
              and not any(t["id"] == tid for t in client.get("/api/terms").json()["taught"]),
              "a taught term can be forgotten")
        check(any(a["action"] == "teach_term" for a in db.get_audit(limit=500)),
              "teaching is in the audit trail")
    finally:
        settings.llm_provider, registry.get_provider, capital_account.get_provider = saved


def fund_level_pro_rata(samples: Path) -> None:
    """A fund-level report (whole-fund statements side by side, investors in a
    table with redacted names): our quarter = fund quarter x our share, once our
    commitment is known; until then, fund-level values only."""
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from app.extractors import registry
    from app.extractors.pdf_native import extract_pdf
    from app.llm import capital_account
    from app.main import app

    client = TestClient(app)
    report = samples / "Fund 79_Q1 2026_Quarterly report.pdf"
    statement = samples / "Fund 79_Q1 2026 Statement_Sample Holdings LP.pdf"
    if not report.exists():
        import subprocess
        subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)

    items, _t = extract_pdf(report)
    blocks = {i.provenance.split(" text")[0] for i in items if "block" in i.provenance}
    by_label = {i.label: i.value_raw for i in items}
    check(len(blocks) >= 3 and by_label.get("Beginning of the period") == "73.000.000"
          and by_label.get("Total expenses") == "510.000",
          "side-by-side statements are read block by block")

    class Model(_FakeModel):
        def complete_json(self, messages: list) -> dict:
            prompt = messages[0]["content"][0]["text"]
            self.prompts.append(prompt)
            c = lambda label, value: {"components": [{"label": label, "value": value}]}
            if "Statement_Sample" in prompt:          # the investor's own statement
                return {"document_kind": "investor_statement", "currency": "EUR",
                        "investor_column": "Sample Holdings LP", "period_column": "Q1, 2026",
                        "period_end": "2026-03-31", "number_format": "1,234.56",
                        "fields": {"funding": c("Called in Q1, 2026", "200,000"),
                                   "ending_balance": c("Capital Accounts at Q1", "1,699,000")}}
            known = "commitment to Fund 79 is 2,000,000" in prompt
            return {"document_kind": "fund_level", "currency": "EUR",
                    "period_column": "31 March 2026", "period_end": "2026-03-31",
                    "number_format": "1.234,56", "fields": {},
                    "fund_fields": {
                        "beginning_balance": c("Beginning of the period", "73.000.000"),
                        "funding": c("CAPITAL TRANSACTION", "10.000.000"),
                        "return_of_capital": c("DISTRIBUTIONS TO SHAREHOLDERS", "0"),
                        "accrued_interest": c("Total investment income", "40.000"),
                        "other": c("Total expenses", "510.000"),   # as printed
                        "investment_mtm": c("Changes in net unrealized", "2.420.000"),
                        "ending_balance": c("End of the period", "84.950.000"),
                        "commitment": c("Total commitments", "100.000.000")},
                    "investor_row": ({"row_label": "Shareholder B", "commitment": "2.000.000",
                                      "paid_in": "1.600.000", "capital_account": "1.699.000"}
                                     if known else None)}

    fake = Model()
    saved = (settings.llm_provider, registry.get_provider, capital_account.get_provider)
    settings.llm_provider = "azure"
    registry.get_provider = capital_account.get_provider = lambda: fake
    try:
        # 1. nothing known about our stake: fund-level values only, and a clear ask
        r = process_path(report, allow_duplicate=True)
        sid = r["submission_id"]
        by = {f["metric_key"]: f for f in db.get_facts(sid)}
        check(r["report_type"] == "fund_financials"
              and Decimal(by["nav"]["value"]) == Decimal("84950000")
              and "model_agrees" in by["nav"]["flags"],
              "no stake known: the fund's own figures, parser and model agreeing")
        check(any("Fund parameters" in w and "commitment" in w for w in r["warnings"]),
              "it asks for our commitment (or row) to compute our share")

        # 2. commitment set, file re-read: our quarter, pro-rata, checked against our row
        client.post("/api/funds/Fund 79/profile", json={"commitment": "2,000,000"})
        res = client.post(f"/api/submissions/{sid}/reread")
        # our roll-forward values; the fund as a whole (its size, its schedule of
        # investments) is recorded alongside, flagged fund_wide
        by = {f["metric_key"]: f for f in db.get_facts(sid)
              if f["status"] != "rejected" and "fund_wide" not in f["flags"]}
        fund_wide = {f["metric_key"]: f for f in db.get_facts(sid) if "fund_wide" in f["flags"]}
        v = lambda k: Decimal(by[k]["value"]) if k in by and by[k]["value"] is not None else None
        check(res.status_code == 200 and db.get_submission(sid)["report_type"]
              == "investment_roll_forward", "re-read with the commitment → our roll-forward")
        check(v("beginning_balance") == 1460000 and v("funding") == 200000
              and v("accrued_interest") == 800 and v("other") == -10200
              and v("investment_mtm") == 48400 and v("ending_balance") == 1699000,
              "our quarter = fund quarter x 2% (1,460,000 → 1,699,000)")
        check(all("pro_rata" in f["flags"] and f["status"] == "needs_review" for f in by.values())
              and "parser_agrees" in by["beginning_balance"]["flags"]
              and not any(fl.startswith("pro_rata_mismatch") for fl in by["ending_balance"]["flags"]),
              "pro-rata values are flagged, corroborated, and match our row")
        warns = json.loads(db.get_submission(sid)["warnings"])
        check(by["ending_balance"]["currency"] == "EUR" and any("pro-rata" in w for w in warns),
              "the share and its basis are stated for the reviewer")
        check(any("taken as outflows" in w for w in warns),
              "expenses printed as positive amounts get the sign that reconciles the fund")
        check("fund_size" in fund_wide and Decimal(fund_wide["fund_size"]["value"]) == 100_000_000
              and fund_wide["fund_size"]["status"] == "needs_review",
              "the fund's total commitments are kept as its fund size, for review")

        # 3. our own statement for the quarter arrives: it replaces the pro-rata values
        r2 = process_path(statement, allow_duplicate=True)
        left = [f for f in db.get_facts(sid) if f["status"] != "rejected"]
        check(r2["report_type"] == "investment_roll_forward"
              and all("fund_wide" in f["flags"] for f in left) and left,
              "the investor's own statement supersedes the pro-rata values "
              "(the fund-wide size stays)")
    finally:
        settings.llm_provider, registry.get_provider, capital_account.get_provider = saved


def currency_controls() -> None:
    """The currency a document reports in, and the reviewer's override."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import detect_currency

    check(detect_currency("Portfolio: Acme USD, Beta USD, Gamma USD. (all figures in EUR) "
                          "Fair Value (EUR) Cost (EUR)") == "EUR",
          "stated reporting currency beats currencies merely listed")
    check(detect_currency("Ending balance EUR EUR EUR. China CNY B/S Rate 6.8 (RMB) I/S Rate")
          == "EUR", "currencies in an FX-rate table are ignored")

    client = TestClient(app)
    sub = next(s for s in db.list_submissions() if s["entity_name"] == "Fund 79")
    money = [f for f in db.get_facts(sub["id"]) if f["unit"] == "currency"]
    check(money and all(f["currency"] == "EUR" for f in money),
          "fund-level values carry the report's currency (EUR)")
    fid = money[0]["id"]
    client.post(f"/api/facts/{fid}", json={"currency": "usd"})
    check(db.get_fact(fid)["currency"] == "USD"
          and any(a["field"] == "currency" and a["new_value"] == "USD"
                  for a in db.get_audit(sub["id"])),
          "a value's currency can be changed (audited)")
    res = client.post(f"/api/submissions/{sub['id']}/currency", json={"currency": "GBP"})
    check(res.status_code == 200 and db.get_submission(sub["id"])["currency"] == "GBP"
          and all(f["currency"] == "GBP" for f in db.get_facts(sub["id"]) if f["unit"] == "currency"),
          "a whole file's currency can be set in one step")
    check(client.post(f"/api/submissions/{sub['id']}/currency",
                      json={"currency": "euro"}).status_code == 400,
          "currency must be a 3-letter code")


def lake_flow(samples: Path) -> None:
    """The medallion flow on a local lake: Land -> Bronze -> Silver -> Gold,
    with only approved values reaching Gold."""
    from fastapi.testclient import TestClient

    from app import lake
    from app.main import app

    settings.lake_backend = "local"
    settings.publish_auto_accepted = False      # strict mode first: approved values only
    try:
        client = TestClient(app)
        q1 = samples / "Fund 80_Q1 2026 Statement_Sample Holdings LP.pdf"
        if not q1.exists():
            import subprocess
            subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)
        r = process_path(q1, allow_duplicate=True)
        sid = r["submission_id"]
        doc = lake.document_id(db.get_submission(sid)["file_hash"])
        reg = [x for x in lake.read_table("land_document_registry") if x["submission_id"] == sid]
        landed = Path(reg[0]["landed_path"]) if reg else None
        check(reg and reg[0]["document_id"] == doc and reg[0]["revision"] == 1
              and landed is not None and landed.exists() and landed.read_bytes() == q1.read_bytes(),
              "02 Land: original landed as received, document registered")
        raw = [x for x in lake.read_table("bronze_raw_records") if x["submission_id"] == sid]
        bronze = settings.data_dir / "lake" / "Files" / "bronze" / doc / "extraction_r1.json"
        check(raw and any(x["mapped_metric"] == "funding" for x in raw)
              and all(x["locator"] for x in raw) and bronze.exists()
              and json.loads(bronze.read_text())["document_id"] == doc,
              "04 Bronze: every line read, with page evidence, plus the raw extraction")

        status = client.post("/api/lake/sync").json()
        silver = lake.read_table("silver_financial_metric")
        gold = lake.read_table("gold_fact_financial_metric")
        n_all = len(db.get_facts())
        n_approved = len(db.trusted_facts())
        check(not status["last_error"] and len(silver) == n_all,
              "04 Silver: every standardized value, in every review state")
        check(len(gold) == n_approved and all(g["review_status"] == "confirmed" for g in gold)
              and not any(g["document_id"] == doc for g in gold),
              "04 Gold: approved values only — this unreviewed file isn't there")

        # approve the file -> its values reach Gold at reporting version 1
        client.post(f"/api/submissions/{sid}/confirm")
        for f in db.get_facts(sid):
            if f["status"] == "needs_review":
                db.update_fact(f["id"], status="confirmed", note="test approval")
        lake.sync()
        gold = [g for g in lake.read_table("gold_fact_financial_metric") if g["document_id"] == doc]
        check(gold and {g["reporting_version"] for g in gold} == {1},
              "approving a file publishes it to Gold (reporting version 1)")
        lake.sync()
        check({g["reporting_version"] for g in lake.read_table("gold_fact_financial_metric")
               if g["document_id"] == doc} == {1},
              "re-syncing unchanged data doesn't bump the version")
        f0 = next(f for f in db.get_facts(sid) if f["metric_key"] == "funding")
        db.update_fact(f0["id"], value="20001", note="test restatement")
        lake.sync()
        check({g["reporting_version"] for g in lake.read_table("gold_fact_financial_metric")
               if g["document_id"] == doc} == {2},
              "changing approved data publishes a new reporting version")
        dims = {t: lake.read_table(t) for t in ("gold_dim_entity", "gold_dim_period",
                                                "gold_dim_metric", "gold_fact_investment_quarter",
                                                "gold_publication")}
        check(all(dims.values()) and any(p["fiscal_label"] == "FY2026 Q2"
                                         for p in dims["gold_dim_period"]),
              "Gold dimensions and the roll-forward are written for Power BI")

        # default mode: auto-accepted values are published too, unreviewed never
        settings.publish_auto_accepted = True
        lake.sync()
        gold_all = lake.read_table("gold_fact_financial_metric")
        statuses = {g["review_status"] for g in gold_all}
        check(len(gold_all) == len(db.trusted_facts()) and "auto" in statuses
              and "needs_review" not in statuses,
              "default: Gold publishes approved + auto-accepted values, never unreviewed")
        settings.publish_auto_accepted = False

        # files uploaded before the lake existed are landed by the backfill
        res = client.post("/api/lake/backfill").json()
        registered = {x["submission_id"] for x in lake.read_table("land_document_registry")}
        check(res["landed"] > 0 and registered >= {s["id"] for s in db.list_submissions()},
              "backfill lands every file uploaded before the lake was switched on")

        # re-read: revision 2 registered, original not landed twice
        client.post(f"/api/submissions/{sid}/reread")
        revs = sorted(x["revision"] for x in lake.read_table("land_document_registry")
                      if x["submission_id"] == sid)
        check(revs == [1, 2] and (bronze.parent / "extraction_r2.json").exists(),
              "a re-read is registered as revision 2 (original kept once)")
    finally:
        settings.lake_backend = "none"
        settings.publish_auto_accepted = True


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


def _approve(client, sid: int) -> None:
    """A reviewer confirms every value of a submission."""
    for f in db.get_facts(sid):
        if f["status"] == "needs_review" and f["value"] is not None:
            client.post(f"/api/facts/{f['id']}", json={"status": "confirmed"})
    client.post(f"/api/submissions/{sid}/confirm")


def deliverable_helpers() -> None:
    """Statement dates, figure columns and stated units, read the way the
    deliverables need them."""
    from datetime import date

    from app.extractors.base import column_fit, detect_statement_dates
    from app.extractors.registry import assemble
    from app.models import detect_scale

    d = detect_statement_dates("Profit and Loss\nJanuary 1-March 31, 2026", "statements.xlsx")
    check(d.statement_end == date(2026, 3, 31) and d.quarter == "2026-Q1",
          '"January 1-March 31, 2026" ends March 31, 2026 (not "1-March 31" read as 2031)')
    q = "2026-Q1"
    check(column_fit("Total", q, balance=False) > column_fit("Mar 2026", q, balance=False)
          and column_fit("Three Months Ended March 31, 2026", q, balance=False)
          > column_fit("Five Months Ended May 31, 2025", q, balance=False)
          and column_fit("Q1'26 Actual", q, balance=False) > column_fit("Q1'26 Budget", q, balance=False)
          and column_fit("Mar 2026", q, balance=True) > column_fit("Jan 2026", q, balance=True),
          "the period's column: a quarter's Total, never one month, another year or a budget; "
          "balances at the quarter's end")
    check(detect_scale("Company A(in $000)") == 1000 and detect_scale("(in thousands)") == 1000
          and detect_scale("figures in $ millions") == 1_000_000
          and detect_scale("Total for Income 12,345.67") == 1,
          '"in $000" / "in thousands" / "in millions" are recognised; plain figures are not scaled')
    r = assemble([], "Lam's pro-rata share of equity income; net change in fair value; holding",
                 "JV Equity Income Loss Fund 83 - 03-31-2026 results.xlsx", "x", "excel",
                 allow_llm=False)
    check(r.detected_report_type not in ("direct_investment", "company_financials"),
          "a file named for a fund is never filed as a direct investment")


def direct_investment_financials(samples: Path) -> None:
    """Lam's "Direct Investment Financials": a portfolio company's own P&L,
    balance sheet and cash flow -> the five figures, burn and runway, budgets,
    and the workbook in Lam's layout."""
    import io

    import openpyxl
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    files = {k: samples / n for k, n in (
        ("qb", "Company 90A_Financials_Q1_2026.xlsx"),
        ("audited", "Company 90B_Consolidated_Financial_Statements_Mar26.xlsx"),
        ("cf_pdf", "Company 90C_Q1_2026_Quarterly_CF.pdf"),
        ("report", "Direct Investment Financials Q1 2026 - Company 90D.csv"))}
    if not all(f.exists() for f in files.values()):
        import subprocess
        subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)
    res = {k: process_path(f, allow_duplicate=True) for k, f in files.items()}

    def vals(key):
        return {f["metric_key"]: f for f in db.get_facts(res[key]["submission_id"])}

    def v(by, k):
        return Decimal(by[k]["value"]) if k in by and by[k]["value"] is not None else None

    qb = vals("qb")
    check(res["qb"]["report_type"] == "company_financials" and res["qb"]["entity_name"]
          == "Company 90A" and res["qb"]["period"] == "2026-Q1",
          "a company's own statements → Direct Investment Financials, Company 90A, 2026-Q1")
    check(v(qb, "revenue") == 75000 and v(qb, "operating_expense") == 1050000,
          "P&L: Total for Income / Total for Expenses from the quarter's Total column")
    check(v(qb, "operating_cash_flow") == -960000 and v(qb, "capex") == -30000
          and "Security Deposit" not in qb["capex"]["raw_label"],
          "cash flow: operating cash flow; CapEx = fixed-asset purchases, not the security deposit")
    check(v(qb, "ending_cash") == 2400000 and "Mar 2026" in qb["ending_cash"]["provenance"]
          and "agrees with the cash-flow statement" in qb["ending_cash"]["provenance"],
          "balance sheet: Total for Bank Accounts at the quarter end, agreeing with the cash flow")
    check(qb["revenue"]["raw_label"] == "Total for Income"
          and not any("Deferred Revenue" in f["raw_label"] or "Department of Revenue" in f["raw_label"]
                      for f in qb.values()),
          "liability lines that merely say 'Revenue' are not revenue")
    au = vals("audited")
    check(v(au, "operating_cash_flow") == -1000000 and v(au, "capex") == -60000
          and v(au, "ending_cash") == 5000000 and v(au, "revenue") == 400000
          and v(au, "operating_expense") == 1200000,
          "multi-period statements: the 2026 quarter's column, not the prior year to its right")
    cf = vals("cf_pdf")
    check(v(cf, "capex") == Decimal("-31912.30") and "adds up to the investing total"
          in cf["capex"]["provenance"] and v(cf, "operating_cash_flow") == Decimal("-500250.00"),
          "PDF lines with numbers in their labels ('Lab Equipment 2026', '12-20') stay labels")
    check(any("Not in this file: Revenue, Operating Expense" in w for w in res["cf_pdf"]["warnings"]),
          "a cash-flow-only file says which figures it doesn't carry")
    rp = vals("report")
    check(v(rp, "revenue") == 120000 and "scaled_x1000" in rp["revenue"]["flags"]
          and v(rp, "operating_cash_flow") == -900000,
          "a finished report in $000 reads back in whole dollars (flagged), actuals not budgets")

    pre = {c["company"]: c["columns"]["2026-Q1"]
           for c in client.get("/api/direct-investments?year=2026").json()["companies"]}
    check(any(i["kind"] == "review" and i["submission_id"] == res["report"]["submission_id"]
              for i in pre["Company 90D"]["issues"]),
          "figures waiting for review are flagged, with the file to open")
    for key in files:
        _approve(client, res[key]["submission_id"])
    rep = client.get("/api/direct-investments?year=2026").json()
    by_co = {c["company"]: c for c in rep["companies"]}
    a = by_co["Company 90A"]["columns"]["2026-Q1"]["actual"]
    check(round(a["gross_monthly_burn"]) == 350000 and a["gross_runway"] == 7
          and round(a["net_monthly_burn"]) == 330000 and a["net_runway"] == 7,
          "burn and runway by Lam's definitions (opex ÷ 3; −(OCF + CapEx) ÷ 3; cash ÷ burn)")
    check(by_co["Company 90D"]["columns"]["2026-Q1"]["checks"] == [],
          "a finished report's own burn and runway agree with the computation")
    year = by_co["Company 90A"]["columns"]["2026"]["actual"]
    check(Decimal(str(year["revenue"])) == 75000 and year["gross_runway"] == 7,
          "the year column carries the year so far")
    ok = client.post("/api/budgets", json={"entity": "Company 90A", "period": "2026-Q1",
                                           "metric": "operating_expense", "value": "900",
                                           "scale": 1000})
    bad = client.post("/api/budgets", json={"entity": "Company 90A", "period": "Q1",
                                            "metric": "operating_expense", "value": "1"})
    b = client.get("/api/direct-investments?year=2026").json()
    b = {c["company"]: c for c in b["companies"]}["Company 90A"]["columns"]["2026-Q1"]["budget"]
    check(ok.status_code == 200 and bad.status_code == 400
          and round(b["gross_monthly_burn"]) == 300000,
          "a budget is entered (in $000), validated, and burns like the actuals")
    x = client.get("/api/exports/direct-investment-financials.xlsx?year=2026&quarter=1")
    wb = openpyxl.load_workbook(io.BytesIO(x.content))
    ws = wb["Company 90A"]
    check(x.status_code == 200 and 'Direct Investment Financials Q1 2026.xlsx'
          in x.headers["content-disposition"]
          and ws["A1"].value == "Company 90A(in $000)" and ws["B1"].value == "Q1'26"
          and ws["J1"].value == "2026" and ws["B2"].value == "Budget" and ws["C2"].value == "Actual"
          and [ws.cell(r, 1).value for r in range(3, 12)] == [
              "Revenue", "Operating Cash Flow", "CapEx", "Ending Cash", "Operating Expense",
              "Gross Monthly Burn", "Gross Runway", "Net Monthly Burn", "Net Runway"],
          "the workbook is Lam's layout: Company(in $000), Q1'26…2026 × Budget | Actual")
    check(ws["C3"].value == 75 and ws["C4"].value == -960 and ws["C9"].value == 7
          and ws["C9"].number_format == '0" Months"' and ws["B7"].value == 900
          and "$" in ws["C3"].number_format,
          "figures in $000 with Lam's formats; runway in months; the budget beside the actual")
    src = [r for r in wb["Sources"].iter_rows(values_only=True) if r and r[0] == "Company 90A"]
    check(any(r[5] == files["qb"].name and r[6] == "Total for Income" for r in src),
          "a Sources sheet names the file and line behind every actual")


def investment_portfolio(samples: Path) -> None:
    """Lam's "Investment Portfolio": approved figures + entered master data ->
    per-fund sheets in Lam's layout and the linked Summary-Cash Flows sheet."""
    import io

    import openpyxl
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    for name in ("Fund 82_Q1 2026 PCAP_Sample Holdings LP.pdf",
                 "Fund 82_Q1 2026 Financial Statements.pdf"):
        r = process_path(samples / name, allow_duplicate=True)
        _approve(client, r["submission_id"])
    det = client.post("/api/portfolio/Fund 82/details", json={
        "display_name": "Example Growth Fund I", "region_sector": "US/Software",
        "fund_size": "150m", "vintage": "2019", "stats_count": "14", "gl_account": "<gl>",
        "initial_investment_date": "4/14/2019"})
    calls = client.post("/api/portfolio/Fund 82/calls", json={
        "lines": "4/14/2019\tDrawdown Notice #1\t0.500\n2/10/2026; Drawdown Notice #9; 0.500",
        "scale": 1000000})
    bad = client.post("/api/portfolio/Fund 82/details", json={"vintage": "soon"})
    client.post("/api/portfolio/Company 90A/details", json={
        "theme": "Robotics", "current_round": "Series A", "total_invested": "3m",
        "projected_investment": "1m", "operating_status": "Active"})
    client.post("/api/portfolio/Company 90B/details", json={
        "current_round": "M&A", "security": "Series B", "total_invested": "2.4m",
        "operating_status": "Inactive", "exit_date": "6/15/2026", "exit_value": "7.5m",
        "exit_proceeds": "6.9m", "exit_escrow": "0.6m"})
    check(det.status_code == 200 and calls.json().get("added") == 2 and bad.status_code == 400,
          "fund details and pasted dated calls are saved, bad input refused")

    rep = client.get("/api/portfolio?as_of=2026-Q1").json()
    f = {x["entity"]: x for x in rep["funds"]}["Fund 82"]
    D = lambda x: Decimal(str(x)) if x is not None else None
    check(f["investment_name"] == "Example Growth Fund I" and f["investment_type"] == "LP"
          and f["vintage"] == 2019 and D(f["fund_size"]) == 150_000_000
          and f["region_sector"] == "US/Software",
          "master data: name, type, vintage, fund size, region / sector")
    check(D(f["commitment"]) == 5_000_000 and D(f["drawn_to_date"]) == 4_000_000
          and D(f["distribution_to_date"]) == 1_000_000 and D(f["future_draw"]) == 1_000_000
          and D(f["investment_balance"]) == 4_200_000 and not f["estimated"],
          "from the statement: commitment, drawn and distributed since inception, remaining "
          "commitment; balance from the roll-forward")
    st = f["stats"]
    check(st["count"] is not None and int(D(st["count"])) == 14 and D(st["cost"]) == 40_000_000
          and D(st["value"]) == 55_000_000 and abs(st["gain_pct"] - 37.5) < 1e-9,
          "Latest Stats: the schedule's grand total (cost, value, gain %) + the entered count")
    inv = f["invested"]
    check(len(inv) == 2 and inv[0]["date"] == "2019-04-14" and D(inv[0]["amount"]) == 500_000
          and all(r["source"] == "entered" for r in inv),
          "Invested: dated calls; a quarter with an entered call isn't repeated from the "
          "roll-forward")
    t = rep["totals"]
    check(D(t["net_cash_flow"]) == D(t["cash_inflow"]) + D(t["cash_outflow"]) + D(t["committed"])
          and D(t["cash_inflow"]) == D(t["fund_distributions"]) + D(t["exit_proceeds"])
          and D(t["exit_proceeds"]) == 6_900_000,
          "cash flows: inflow = distributions + exit proceeds; net = inflow + outflow + committed")

    x = client.get("/api/exports/investment-portfolio.xlsx?as_of=2026-Q1")
    wb = openpyxl.load_workbook(io.BytesIO(x.content))
    ws = wb["Fund 82"]
    check(x.status_code == 200 and "Investment Portfolio Mar'26.xlsx" in x.headers["content-disposition"]
          and ws["B2"].value == "Example Growth Fund I" and ws["B3"].value == "LP"
          and ws["B5"].value == 2019 and ws["B6"].value == 150 and ws["B7"].value == 5
          and ws["B9"].value == 1 and ws["B10"].value == "US/Software"
          and ws["B11"].value == 4.2 and ws["B13"].value == 4 and ws["F13"].value == -1,
          "fund sheet: Lam's cells (B2 name … B13 drawn, F13 distribution) in $M")
    check(ws["A15"].value == "Latest Stats" and ws["C15"].value == 14 and ws["D15"].value == 40
          and ws["E15"].value == 55 and abs(ws["F15"].value - 0.375) < 1e-9
          and ws["A17"].value == "Invested" and ws["C17"].value == "Drawdown Notice #1",
          "fund sheet: Latest Stats row and the Invested history")
    s = wb["Summary-Cash Flows"]
    row = next(r for r in range(1, s.max_row + 1) if s.cell(r, 1).value == "Fund 82")
    check(s["C3"].value == "$B$2" and s["F3"].value == "$B$13"
          and s.cell(row, 3).value == "='Fund 82'!$B$2" and s.cell(row, 6).value == "='Fund 82'!$B$13"
          and s.cell(row, 7).value == "='Fund 82'!$B$9" and s.cell(row, 9).value == "=-'Fund 82'!$F$13",
          "summary: every fund figure is a reference into its sheet, as in Lam's workbook")
    labels = {s.cell(r, 3).value: r for r in range(1, s.max_row + 1) if s.cell(r, 3).value}
    check(all(k in labels for k in ("Total Fund Investments", "Total Exited Investments",
                                    "Total Direct Investments", "Cash inflow",
                                    "Actual cash outflow", "Committed investments",
                                    "Total net cash flow"))
          and str(s.cell(labels["Total net cash flow"], 6).value).startswith("=SUM("),
          "summary: fund, exited and direct sections with Lam's cash-flow totals")
    ex = next(r for r in range(1, s.max_row + 1) if s.cell(r, 1).value == "Company 90B"
              and s.cell(r, 4).value == "='Company 90B'!$B$9")
    check(s.cell(ex, 9).value == 6.9, "exited investments carry their proceeds")


def manual_entry(samples: Path) -> None:
    """A statement that's empty, unreadable or missing a figure: the gap is
    flagged, and a person types the figure in from the dashboard (or settles a
    mismatch). Typed figures are confirmed, marked as entered, and audited."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    def col(company):
        rep = client.get("/api/direct-investments?year=2026").json()["companies"]
        return {c["company"]: c for c in rep}[company]["columns"]["2026-Q1"]

    def manual(**kw):
        return client.post("/api/manual-values", json={
            "report_type": "company_financials", "period": "2026-Q1", "scale": 1000, **kw})

    miss = [i for i in col("Company 90C")["issues"] if i["kind"] == "missing"]
    check(miss and set(miss[0]["metrics"]) == {"revenue", "operating_expense"},
          "figures that no approved file shows are flagged as missing")
    r = manual(entity="Company 90C", metric="revenue", value="45", note="per the company's CFO")
    k = col("Company 90C")
    typed = [f for f in db.get_facts(r.json().get("submission_id")) if f["metric_key"] == "revenue"]
    check(r.status_code == 200 and Decimal(str(k["actual"]["revenue"])) == 45000
          and k["sources"]["revenue"]["manual"]
          and not any("revenue" in (i.get("metrics") or []) for i in k["issues"])
          and typed and typed[0]["status"] == "confirmed" and "manual_entry" in typed[0]["flags"]
          and any(a["action"] == "manual_entry" for a in db.get_audit(limit=20)),
          "a figure typed in by hand fills the gap — confirmed, marked as entered, audited")
    manual(entity="Company 90C", metric="revenue", value="")
    check(col("Company 90C")["actual"].get("revenue") is None,
          "an entered figure can be withdrawn (and the gap is flagged again)")
    manual(entity="Company 90A", metric="revenue", value="80")
    clash = [i for i in col("Company 90A")["issues"] if i["kind"] == "mismatch"]
    check(clash and "75,000.00" in clash[0]["message"] and "80,000.00" in clash[0]["message"],
          "a typed figure that disagrees with an approved file is flagged as a mismatch")
    manual(entity="Company 90A", metric="revenue", value="")
    sub = next(s for s in db.list_submissions() if s["filename"].startswith("Company 90C_"))
    added = client.post(f"/api/submissions/{sub['id']}/facts",
                        json={"metric": "operating_expense", "value": "1,100,000"})
    check(added.status_code == 200
          and Decimal(str(col("Company 90C")["actual"]["operating_expense"])) == 1100000,
          "while reviewing a file, a value it doesn't show can be added")
    bad = [manual(entity="Company 90C", metric="nope", value="1").status_code,
           manual(entity="Company 90C", metric="revenue", value="1", period="Q1").status_code,
           manual(entity="Company 90C", metric="revenue", value="lots").status_code]
    check(bad == [400, 400, 400], "typed input is validated (figure, period, number)")
    manual(entity="Company 90E", metric="ending_cash", value="2,000", note="scan unreadable")
    e = col("Company 90E")
    check(Decimal(str(e["actual"]["ending_cash"])) == 2000000
          and any(i["kind"] == "missing" for i in e["issues"]),
          "a company whose files couldn't be read can be entered by hand, gaps flagged")
    scan = samples / "Company 90G_Q1_2026_Financial_Statements.pdf"
    if not scan.exists():
        import subprocess
        subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)
    r = process_path(scan, allow_duplicate=True)
    g = col("Company 90G")
    check(any("scanned" in w for w in r["warnings"])
          and any(i["kind"] == "unread" and i["submission_id"] == r["submission_id"]
                  for i in g["issues"])
          and any(i["kind"] == "missing" and len(i["metrics"]) == 5 for i in g["issues"]),
          "a file nothing could be read from is flagged — its company appears, every figure open "
          "to be typed in")
    import shutil
    pcap = Path(tempfile.mkdtemp()) / "Fund 86_Q1 2026 PCAP_Sample Holdings LP.pdf"
    shutil.copy(samples / "Fund 82_Q1 2026 PCAP_Sample Holdings LP.pdf", pcap)
    settings.publish_auto_accepted = False       # strict: only reviewer-confirmed values publish
    try:
        r = process_path(pcap, allow_duplicate=True)
        funds = {f["entity"]: f for f in client.get("/api/portfolio?as_of=2026-Q1").json()["funds"]}
        f86 = funds.get("Fund 86")
        check(f86 is not None and f86["investment_balance"] is None
              and any(i["kind"] == "review" and i["submission_id"] == r["submission_id"]
                      for i in f86["issues"]),
              "a fund whose values all wait for review is still listed, flagged, nothing published")
    finally:
        settings.publish_auto_accepted = True
    client.post("/api/portfolio/Fund 84/details", json={"kind": "fund"})
    funds = {f["entity"]: f for f in client.get("/api/portfolio?as_of=2026-Q1").json()["funds"]}
    check(any(i["kind"] == "missing" and "investment_balance" in i["fields"]
              for i in funds["Fund 84"]["issues"]),
          "a fund with no statements on file is flagged as missing its figures")
    client.post("/api/portfolio/Fund 84/details", json={"investment_balance": "2.5m"})
    client.post("/api/portfolio/Fund 82/details", json={"drawn_to_date": "4.5m"})
    funds = {f["entity"]: f for f in client.get("/api/portfolio?as_of=2026-Q1").json()["funds"]}
    check(Decimal(str(funds["Fund 84"]["investment_balance"])) == 2500000
          and funds["Fund 84"]["sources"]["investment_balance"] == "entered"
          and any(i["kind"] == "mismatch" and "4,000,000.00" in i["message"]
                  for i in funds["Fund 82"]["issues"]),
          "fund figures can be entered by hand; one that disagrees with a statement is flagged")
    client.post("/api/portfolio/Fund 82/details", json={"drawn_to_date": ""})

    # a quarter whose approved balances don't add up is flagged
    for metric, value in (("beginning_balance", "100,000"), ("funding", "0"),
                          ("ending_balance", "90,000")):
        client.post("/api/manual-values", json={
            "entity": "Fund 88", "report_type": "investment_roll_forward", "period": "2026-Q1",
            "metric": metric, "value": value})
    funds = {f["entity"]: f for f in client.get("/api/portfolio?as_of=2026-Q1").json()["funds"]}
    check(any(i["kind"] == "mismatch" and "doesn't add up" in i["message"]
              for i in funds["Fund 88"]["issues"]),
          "a quarter whose beginning + activity ≠ ending is flagged")

    # a fund with only a whole-fund report: ask for Lam's commitment, re-read the report
    report = Path(tempfile.mkdtemp()) / "Fund 87_Q1 2026_Quarterly report.pdf"
    shutil.copy(samples / "Fund 79_Q1 2026_Quarterly report.pdf", report)
    r = process_path(report, allow_duplicate=True)
    funds = {f["entity"]: f for f in client.get("/api/portfolio?as_of=2026-Q1").json()["funds"]}
    f87 = funds["Fund 87"]
    check(r["report_type"] == "fund_financials" and f87["fund_level_ids"] == [r["submission_id"]]
          and any(i["kind"] == "commitment" and i["reread_ids"] == [r["submission_id"]]
                  for i in f87["issues"])
          and any(i["kind"] == "missing" and [x["id"] for x in i.get("files", [])]
                  == [r["submission_id"]] for i in f87["issues"]),
          "a whole-fund report asks for Lam's commitment; missing figures offer a re-read of the file")
    rr = client.post(f"/api/submissions/{r['submission_id']}/reread")
    check(rr.status_code == 200 and db.get_submission(r["submission_id"])["revision"] == 2,
          "a file on record can be re-read with today's reading")

    # a hand-added entry with nothing on file can be taken off the report
    client.post("/api/portfolio/Fund 89/details", json={"kind": "fund"})
    listed = {f["entity"]: f for f in client.get("/api/portfolio?as_of=2026-Q1").json()["funds"]}
    gone = client.post("/api/portfolio/Fund 89/details", json={"kind": ""})
    after = {f["entity"] for f in client.get("/api/portfolio?as_of=2026-Q1").json()["funds"]}
    check(listed["Fund 89"]["removable"] and gone.status_code == 200 and "Fund 89" not in after
          and not listed["Fund 82"]["removable"],
          "a hand-added fund with nothing on file can be removed; one with files can't")


def portfolio_summary() -> None:
    """The team's dashboard table: direct investments by status, funds, and all
    investments by group — gains, TVPI, DPI and AUM computed one way throughout."""
    import io

    import openpyxl
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    P = "/api/portfolio/"
    client.post(P + "Company 91A/details", json={"kind": "direct_investment", "total_invested": "2m",
                                                 "total_value": "3.5m", "portfolio_group": "Life Sciences"})
    client.post(P + "Company 91B/details", json={"kind": "direct_investment", "total_invested": "3m",
                                                 "total_value": "1m", "impairment": "2.5m"})
    client.post(P + "Company 91C/details", json={"kind": "direct_investment", "total_invested": "1m",
                                                 "exit_date": "5/1/2026", "exit_value": "3m",
                                                 "exit_proceeds": "2.4m"})
    client.post(P + "Fund 92/details", json={"kind": "fund", "investment_balance": "5m",
                                             "drawn_to_date": "4m", "distribution_to_date": "1m"})
    for metric, value in (("ending_balance", "1,000,000"), ("cumulative_contributions", "900,000")):
        client.post("/api/manual-values", json={
            "entity": "Fund 93", "report_type": "investment_roll_forward", "period": "2026-Q1",
            "metric": metric, "value": value, "currency": "EUR"})
    S = client.get("/api/portfolio/summary").json()
    line = {ln["entity"]: ln for ln in S["lines"]}
    near = lambda a, b: a is not None and abs(float(a) - float(b)) < 0.01
    a, b, c, f = line["Company 91A"], line["Company 91B"], line["Company 91C"], line["Fund 92"]
    check(a["status"] == "current" and near(a["unrealized"], 1_500_000) and near(a["aum"], 3_500_000)
          and near(a["book"], 2_000_000) and a["group"] == "Life Sciences",
          "a held company: unrealized = value − cost, book value = cost, AUM = value")
    check(b["status"] == "impaired" and near(b["impairment"], -2_500_000) and near(b["book"], 500_000)
          and near(b["unrealized"], -2_000_000),
          "an impaired company: impairment shown as a write-down, book value = cost − impairment")
    check(c["status"] == "exited" and near(c["realized"], 1_400_000) and near(c["unrealized"], 600_000)
          and near(c["aum"], 0) and near(c["proceeds"], 2_400_000),
          "an exit: realized = proceeds − cost, unrealized = value − proceeds, no AUM")
    check(near(f["value"], 6_000_000) and near(f["realized"], 1_000_000)
          and near(f["unrealized"], 1_000_000) and near(f["aum"], 5_000_000) and near(f["book"], 5_000_000),
          "a fund: value = NAV + distributions, unrealized = NAV − paid-in, AUM = NAV")
    check("Fund 93" not in line and any(n["entity"] == "Fund 93" and "EUR" in n["message"]
                                        for n in S["notes"]),
          "a fund reporting in EUR stays out of the USD totals until it has a USD rate")
    client.post(P + "Fund 93/details", json={"usd_rate": "1.1"})
    S = client.get("/api/portfolio/summary").json()
    line = {ln["entity"]: ln for ln in S["lines"]}
    check("Fund 93" in line and near(line["Fund 93"]["aum"], 1_100_000)
          and near(line["Fund 93"]["paid_in"], 990_000),
          "with its USD rate, the EUR fund is converted and included")
    rows = {r["label"]: r for r in S["rows"]}
    tot = {r["label"]: r for r in S["totals"]}
    direct = rows["Total Direct Investments"]
    parts = [rows[k] for k in ("Current Direct Investments", "Impaired Direct Investments",
                               "Exited Direct Investments")]
    allin = tot["Total Investments"]
    check(near(direct["paid_in"], sum(float(r["paid_in"]) for r in parts))
          and direct["count"] == sum(r["count"] for r in parts)
          and near(allin["value"], float(direct["value"]) + float(rows["Fund Investments"]["value"]))
          and near(allin["aum"], float(direct["aum"]) + float(rows["Fund Investments"]["aum"]))
          and abs(allin["tvpi"] - float(allin["value"]) / float(allin["paid_in"])) < 1e-9
          and abs(allin["dpi"] - float(allin["proceeds"]) / float(allin["paid_in"])) < 1e-9
          and near(allin["total_gain"], float(allin["realized"]) + float(allin["unrealized"]))
          and "Life Sciences Investments" in tot and "Core Investments" in tot,
          "totals add up: direct = current + impaired + exited; all = direct + funds; by group")
    wb = openpyxl.load_workbook(io.BytesIO(client.get("/api/exports/investment-portfolio.xlsx").content))
    dash = wb["Dashboard"] if "Dashboard" in wb.sheetnames else None
    check(dash is not None and any(dash.cell(r, 1).value == "Total Investments"
                                   for r in range(1, dash.max_row + 1)),
          "the Investment Portfolio workbook carries the summary as a Dashboard sheet")

    # funds: realized gain leaves out the capital that came back
    client.post(P + "Fund 92/details", json={"returned_capital": "0.6m"})
    S = client.get("/api/portfolio/summary").json()
    f = {ln["entity"]: ln for ln in S["lines"]}["Fund 92"]
    check(near(f["realized"], 400_000) and near(f["unrealized"], 1_600_000)
          and near(float(f["realized"]) + float(f["unrealized"]), 2_000_000),
          "a fund's realized gain = distributions − returned capital; total gain unchanged")

    # a USD rate per quarter wins over the single rate
    bad = client.post(P + "Fund 93/details", json={"usd_rates": "about 1.2"})
    client.post(P + "Fund 93/details", json={"usd_rates": "2026-Q1=1.2, 2025 Q4: 1.05"})
    S = client.get("/api/portfolio/summary?as_of=2026-Q1").json()
    check(bad.status_code == 400
          and near({ln["entity"]: ln for ln in S["lines"]}["Fund 93"]["aum"], 1_200_000),
          "USD rates per quarter: the quarter's own rate converts it, unreadable rates are refused")
    S2 = client.get("/api/portfolio/summary?as_of=2026-Q2").json()
    check(near({ln["entity"]: ln for ln in S2["lines"]}["Fund 93"]["aum"], 1_200_000),
          "a 2026-Q1 statement converts at 2026-Q1's rate, whatever quarter the report is as of")
    client.post(P + "Fund 93/details", json={"usd_rates": "2025 Q4: 1.05"})
    S3 = client.get("/api/portfolio/summary?as_of=2026-Q1").json()
    check(near({ln["entity"]: ln for ln in S3["lines"]}["Fund 93"]["aum"], 1_050_000)
          and any(n["entity"] == "Fund 93" and "2025-Q4 USD rate" in n["message"] for n in S3["notes"]),
          "a quarter without its own rate uses the latest earlier one, and says so")
    client.post(P + "Fund 93/details", json={"usd_rates": "2026-Q1=1.2, 2025 Q4: 1.05"})

    # cells resting on an estimate are marked, row by row
    S = client.get("/api/portfolio/summary").json()
    fund_row = next(r for r in S["rows"] if r["kind"] == "fund")
    est_keys = {k for ln in S["lines"] if ln["kind"] == "fund" for k in ln["estimated_keys"]}
    check(est_keys <= set(fund_row["estimated"])
          and all(isinstance(r["estimated"], list) for r in S["rows"] + S["totals"])
          and all(not r["estimated"] for r in S["rows"] if r["kind"] == "status"),
          "summary rows list the cells that rest on an estimate; direct investments have none")

    # Overview's TVPI / DPI are the summary's fund row
    perf = client.get("/api/performance").json()["portfolio"]
    check(perf["tvpi"] == round(fund_row["tvpi"], 2) and perf["dpi"] == round(fund_row["dpi"], 2)
          and near(perf["total_nav"], fund_row["aum"]) and perf["n_funds"] == fund_row["count"],
          "Overview's TVPI, DPI and NAV are the Portfolio Summary's fund row")

    # one holdings sheet fills every company's holding record
    sheet = ("Portfolio holdings\n"
             "Company,Cost ($M),Fair Value ($M),Round,Status,Group,Last investment date,Impairment ($M),Notes\n"
             "Company 91D,2.5,4.0,Series B,active,Life Sciences,3/15/2025,,\n"
             "Company 91E,1.0,0.25,Seed,Active,,2024-11-02,0.75,\n"
             "Company 91F,lots,1,Seed,Active,,,,\n"
             "Total,3.5,4.25,,,,,,\n")
    r = client.post("/api/portfolio/holdings", files={"file": ("holdings.csv", sheet.encode())})
    res = r.json()
    S = client.get("/api/portfolio/summary").json()
    line = {ln["entity"]: ln for ln in S["lines"]}
    check(r.status_code == 200 and res["updated"] == 2 and len(res["errors"]) == 1
          and res["errors"][0]["company"] == "Company 91F" and res["ignored_columns"] == ["Notes"],
          "holdings sheet: good rows imported, an unreadable row reported and skipped, totals row ignored")
    d, e = line.get("Company 91D"), line.get("Company 91E")
    check(d is not None and near(d["paid_in"], 2_500_000) and near(d["value"], 4_000_000)
          and d["group"] == "Life Sciences" and e is not None and e["status"] == "impaired"
          and near(e["impairment"], -750_000) and "Company 91F" not in line,
          "imported holdings feed the summary: cost and value in $M, group, impairment")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Company", "Total Investment to Date", "Total Value"])
    ws.append(["company 91d", 2_600_000, None])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/portfolio/holdings", files={"file": ("holdings.xlsx", buf.getvalue())})
    line = {ln["entity"]: ln for ln in client.get("/api/portfolio/summary").json()["lines"]}
    back = client.get("/api/portfolio/holdings.csv").text
    check(r.json().get("updated") == 1 and near(line["Company 91D"]["paid_in"], 2_600_000)
          and near(line["Company 91D"]["value"], 4_000_000) and "Company 91D" in back
          and client.post("/api/portfolio/holdings",
                          files={"file": ("x.csv", b"a,b\n1,2\n")}).status_code == 400,
          "an xlsx sheet updates a company by name, empty cells keep what is on file; "
          "a sheet without a Company column is refused")

    # the team's own Summary sheet: a fund block and a company block, $M without a
    # unit, "NA" cells, cell-reference rows, each block closed by its Total row,
    # the dashboard block underneath (made-up figures)
    sheet = ("Cell References,,$B$2,$B$3,$B$5,$D$15,$E$15,$F$15,$B$13,$B$11,$B$11,$F$13\n"
             "Worksheet Reference,,Fund,Type,Vintage,#,$,Fair Market Value,Draw,Q4'25 NAV,Q1'26 NAV,Distribution\n"
             "Fund 94,,Fund 94,LP,2019,12 ,$150.0M,$210.0M,$6.0M,$5.0M,$5.5M,$1.0M\n"
             "Fund 95,,Fund 95,LP,2021,8 ,$80.0M,$90.0M,$2.0M,$1.8M,$1.9M,$0.0M\n"
             ",,Total,,,,$230.0M,$300.0M,$8.0M,$6.8M,$7.4M,$1.0M\n"
             "Cell References,,$B$2,$B$10,,,,$B$9,$B$8,$F$14,$G$14,$E$14\n"
             ",,,,,,,,,,Lam's Total,Lam's\n"
             "Worksheet Reference,,Company,Theme,Operating Status,Current/Impaired/Exit,Impairment Date,"
             "Round,Investment Date,Pre-Money Val,Investment,Ownership,Estimated FMV,Gain (%),Book Value,Proceeds,Impairment\n"
             "Company 94A,,Company 94A,Semi,Active,Current,,Seed,Sep 2022,$30.0,$2.0,6.82%,$3.0,50.0%,$2.0,,\n"
             "Company 94B,,Company 94B,Life Science,Active,Impaired,2025,Series A,Oct 2017,NA,$2.0,0.45%,$0.6,-70.0%,$0.6,,$1.4\n"
             "Company 94C,,Company 94C,I4.0,Inactive,Exit,,Series A,Jun 2021,$440.0,$7.0,4.48%,$19.7,181.8%,$12.6,$0.0,\n"
             "Company 94D,,Company 94D,Semi,Inactive,Exit,,Seed,Jul 2021,$0.0,$4.0,0.00%,$5.0,NA,$0.0,$5.0,\n"
             ",,Total Direct,,,,,,,,$15.0,,$28.3,,$15.2,$5.0,$1.4\n"
             ",Dashboard needed,,,,,,,,,,,,,,,\n"
             ",,Current Direct Investment,,1,$2.0M,$3.0M,$2.0M,$0.0M,$0.0M,$0.0M,$1.0M\n"
             ",,Total Direct Investment,,4,$15.0M,$28.3M,$15.2M,-$1.4M,$5.0M,-$10.0M,$13.3M\n")
    r = client.post("/api/portfolio/holdings", files={"file": ("summary.csv", sheet.encode())})
    res = r.json()
    S = client.get("/api/portfolio/summary").json()
    line = {ln["entity"]: ln for ln in S["lines"]}
    rep = client.get("/api/portfolio").json()
    f94 = {x["entity"]: x for x in rep["funds"]}.get("Fund 94")
    check(r.status_code == 200 and res["updated"] == 6 and not res["errors"]
          and len(res["notes"]) == 1 and "$B$2" not in line and "Current Direct Investment" not in line,
          "the team's Summary sheet: fund and company blocks read, each closed by its Total row, "
          "cell references and the dashboard rows ignored")
    check(f94 is not None and near(f94["drawn_to_date"], 6_000_000) and near(f94["investment_balance"], 5_500_000)
          and near(f94["distribution_to_date"], 1_000_000) and f94["vintage"] == 2019
          and f94["stats"]["count"] == 12 and near(f94["stats"]["cost"], 150_000_000),
          "fund block: draw, the latest quarter's NAV, distribution, vintage and latest stats")
    a, b, c, d = (line.get(k) for k in ("Company 94A", "Company 94B", "Company 94C", "Company 94D"))
    check(a and near(a["paid_in"], 2_000_000) and near(a["value"], 3_000_000) and a["status"] == "current"
          and b and b["status"] == "impaired" and near(b["impairment"], -1_400_000) and b["group"] == "Life Sciences"
          and c and c["status"] == "exited" and near(c["value"], 19_700_000) and near(c["book"], 12_600_000)
          and near(c["realized"], -7_000_000) and near(c["unrealized"], 19_700_000)
          and d and near(d["realized"], 1_000_000) and near(d["aum"], 0),
          "company block: $M amounts, NA skipped, Current / Impaired / Exit status, an exit valued at its FMV")
    rows = {r["label"]: r for r in S["rows"]}
    tot = {r["label"]: r for r in S["totals"]}
    check(near(rows["Total Direct Investments"]["alloc_pct"] + rows["Fund Investments"]["alloc_pct"], 100)
          and near(tot["Total Investments"]["alloc_pct"], 100)
          and rows["Current Direct Investments"]["alloc_pct"] is None,
          "% capital allocation: direct + funds = 100%, shown on the block and group rows only")


def easy_auth_user() -> None:
    """Behind Azure App Service Authentication the audit trail names the
    signed-in person; without it, entries stay "local"."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/api/portfolio/Company 96A/details", json={"kind": "direct_investment", "theme": "Semi"},
                headers={"X-MS-CLIENT-PRINCIPAL-NAME": "reviewer@example.com"})
    client.post("/api/portfolio/Company 96A/details", json={"theme": "Devices"})
    users = [a["user"] for a in db.get_audit(limit=20) if a["action"] == "entity_details"
             and a["field"] == "Company 96A"]
    check(users[:2] == ["local", "reviewer@example.com"],
          "audit entries carry the Easy Auth user when the platform names one, else local")


def password_gate() -> None:
    """AUTH_MODE=password: every page and API call needs the session; the cookie
    is HTTPS-only; wrong passwords are limited per address; a gated workspace
    refuses to start on the repository's default session secret."""
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import app

    saved = (settings.auth_mode, settings.app_password, settings.session_secret)
    settings.auth_mode, settings.app_password = "password", "test-password-only"
    try:
        bad = None
        try:
            auth.check_configuration()
        except RuntimeError as e:
            bad = str(e)
        settings.session_secret = "x" * 48
        auth.check_configuration()
        check(bad is not None and "SESSION_SECRET" in bad,
              "a gated workspace refuses to start on the default session secret")
        client = TestClient(app, follow_redirects=False)
        home, api = client.get("/"), client.get("/api/portfolio")
        check(home.status_code == 302 and home.headers["location"] == "/login"
              and api.status_code == 401,
              "without a session: pages redirect to login, the API answers 401")
        auth._FAILURES.clear()
        results = [client.post("/api/login", data={"password": "wrong"}).headers["location"]
                   for _ in range(settings.login_max_failures)]
        right_but_locked = client.post("/api/login", data={"password": "test-password-only"})
        check(results[0] == "/login?error=1" and results[-1] == "/login?error=locked"
              and right_but_locked.headers["location"] == "/login?error=locked",
              f"{settings.login_max_failures} wrong passwords lock that address out, even for the right one")
        auth._FAILURES.clear()
        ok = client.post("/api/login", data={"password": "test-password-only"})
        cookie = ok.headers.get("set-cookie", "")
        check(ok.status_code == 302 and ok.headers["location"] == "/" and "Secure" in cookie
              and "HttpOnly" in cookie,
              "the right password opens a session: HttpOnly, HTTPS-only cookie")
        check(client.get("/api/health").status_code == 200
              and client.get("/", cookies={auth.COOKIE: cookie.split(";")[0].split("=", 1)[1]}).status_code == 200,
              "with the session the workspace opens; the health check stays public")
        token = cookie.split(";")[0].split("=", 1)[1]
        check(api.json().get("detail", "").startswith("Your session has ended")
              and client.get("/", cookies={auth.COOKIE: token}).headers.get("set-cookie") is None,
              "a 401 says why in its detail; a fresh session is not re-issued")
        import json as _json
        old_payload = _json.dumps({"u": "workspace", "t": int(__import__("time").time()) - 7 * 3600}).encode()
        aged = client.get("/api/health", cookies={auth.COOKIE: auth._sign(old_payload)})
        aged = client.get("/", cookies={auth.COOKIE: auth._sign(old_payload)})
        check(aged.status_code == 200 and auth.COOKIE in (aged.headers.get("set-cookie") or ""),
              "a session past half its life is renewed on use, so a working day never ends in 'Not saved'")
    finally:
        settings.auth_mode, settings.app_password, settings.session_secret = saved
        auth._FAILURES.clear()


def master_workbook() -> None:
    """The team's Investment Portfolio workbook (one sheet per fund / company
    in its layout, the accounting block beside, a Summary-Portfolio Value tab
    and a planning tab) fills every entity in one upload; nothing is invented
    from a stray heading; a re-read adds no duplicate investments."""
    import io

    import openpyxl
    from fastapi.testclient import TestClient

    from app.extractors.base import guess_entity_name
    from app.main import app
    from app.pipeline import reprocess_submission

    client = TestClient(app)
    wb = openpyxl.Workbook()
    aop = wb.active
    aop.title = "Investments-AOP"
    aop.append(["Follow-on 1 Company Q EU/ Devices H1 2026 1 25"])
    aop.append(["Planned", "H1 2026", 1.0])
    f = wb.create_sheet("Fund 97")
    for r, label, value in ((2, "Investment Name", "Example Partners III"), (3, "Investment Type", "LP"),
                            (4, "Current / Most Recent Round", "-"), (5, "Vintage", 2019),
                            (6, "Fund Size", 150.0), (7, "Commitment", 5.0), (9, "Future Draw", 1.0),
                            (10, "Region / Sector Focus", "US/Software"), (11, "Investment Balance", 4.2),
                            (13, "Drawn to Date", 4.0)):
        f.cell(r, 1, label); f.cell(r, 2, value)
    f.cell(13, 5, "Distribution to Date"); f.cell(13, 6, -1.0)
    for c, h in zip("BCDEF", ("Date", "# of investments", "Cost Basis", "Value", "Gain / (Loss) %")):
        f[f"{c}14"] = h
    f["A15"] = "Latest Stats"; f["B15"] = "2026-03-31"; f["C15"] = 14; f["D15"] = 40.0; f["E15"] = 55.0
    for c, h in zip("BCDEF", ("Date", "Current Round", "Amount Invested", "Total Raise", "Pre Money Valuation")):
        f[f"{c}16"] = h
    f.append([]); f["A17"] = "Invested"; f["B17"] = "2019-04-14"; f["C17"] = "Drawdown #1"; f["D17"] = 0.5
    f["A18"] = "Invested"; f["B18"] = "2026-02-10"; f["C18"] = "Drawdown #9"; f["D18"] = 0.5
    f["H2"] = "Equity Investments Summary"
    for r, label, value in ((3, "Entity Type", "Limited Partnership"), (4, "Method of Accounting", "Equity Method"),
                            (5, "GL Account", "1234"), (6, "Company Code", "C9"), (7, "% Ownership", 0.025),
                            (8, "Initial Investment Date", "2019-04-14"), (9, "Terms", "10 years"),
                            (10, "Total Commitment", 5_000_000), (11, "Remaining Commitment", 1_000_000),
                            (12, "Total Funding", 4_000_000), (13, "Total Equity Income/(Loss)", 1_200_000),
                            (14, "Total Distributions", -1_000_000), (15, "Investment Balance", 4_200_000)):
        f.cell(r, 8, label); f.cell(r, 9, value)
    co = wb.create_sheet("Company 97A")
    for r, label, value in ((2, "Investment Name", "Example Robotics Inc."), (3, "Investment Type", "Direct"),
                            (4, "Current / Most Recent Round", "Series A"), (5, "Most Recent Investment Date", "2026-03-01"),
                            (6, "Total Investment to Date", 2.0), (7, "Projected Future Investments", 1.0),
                            (9, "Investments", "Series A Preferred"), (10, "Theme", "Robotics"),
                            (11, "Total Value", 3.0), (12, "Operating Status", "Active")):
        co.cell(r, 1, label); co.cell(r, 2, value)
    for c, h in zip("BCDEF", ("Date", "Current Round", "Amount Invested", "Total Raise", "Pre Money Valuation")):
        co[f"{c}16"] = h
    co["A17"] = "Invested"; co["B17"] = "2024-06-01"; co["C17"] = "Seed"; co["D17"] = 0.5; co["E17"] = 4.0; co["F17"] = 12.0
    co["A18"] = "Invested"; co["B18"] = "2026-03-01"; co["C18"] = "Series A"; co["D18"] = 1.5; co["E18"] = 20.0; co["F18"] = 60.0
    sv = wb.create_sheet("Summary-Portfolio Value")
    sv.append(["Worksheet Reference", None, "Company", "Theme", "Operating Status", "Current/Impaired/Exit", "Book Value"])
    sv.append(["Company 97A", None, "Company 97A", "Advanced robotics", "Active", "Current", 1.8])
    sv.append([None, None, "Total Direct", None, None, None, 1.8])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/upload", files={"files": ("Investment Portfolio Jun'26 (test).xlsx", buf.getvalue())})
    res = r.json()["results"][0]
    check(res["status"] == "ok" and res["report_type"] == "master_workbook" and res["period"] == "2026-Q2"
          and {e["entity"] for e in res["entities"]} == {"Fund 97", "Company 97A"} and res["calls_added"] == 4
          and any("Investments-AOP" in w for w in res["warnings"]),
          "a master workbook is recognised: both entities filled, 4 dated investments, the planning tab named as not read")
    rep = client.get("/api/portfolio?as_of=2026-Q2").json()
    names = {x["entity"] for x in rep["funds"] + rep["companies"]}
    check(not any(n.startswith("Follow-on") for n in names) and "Follow-on 1 Company Q" not in str(db.all_details()),
          "no entity is invented from the planning tab's first line")
    fund = {x["entity"]: x for x in rep["funds"]}["Fund 97"]
    D = lambda x: Decimal(str(x)) if x is not None else None
    check(fund["investment_name"] == "Example Partners III" and fund["investment_type"] == "LP"
          and fund["vintage"] == 2019 and D(fund["fund_size"]) == 150_000_000
          and D(fund["commitment"]) == 5_000_000 and D(fund["future_draw"]) == 1_000_000
          and D(fund["investment_balance"]) == 4_200_000 and D(fund["drawn_to_date"]) == 4_000_000
          and D(fund["distribution_to_date"]) == 1_000_000 and fund["region_sector"] == "US/Software"
          and fund["stats"]["count"] == 14 and D(fund["stats"]["cost"]) == 40_000_000
          and fund["accounting"]["gl_account"] == "1234" and D(fund["accounting"]["ownership_pct"]) == Decimal("2.5")
          and len([c for c in fund["invested"] if c["source"] == "entered"]) == 2,
          "fund sheet: $M figures, commitment, latest stats, accounting block (whole units, % ownership) and calls")
    com = {x["entity"]: x for x in rep["companies"]}["Company 97A"]
    det = com["details"]
    check(com["investment_name"] == "Example Robotics Inc." and com["current_round"] == "Series A"
          and com["security"] == "Series A Preferred" and D(com["total_invested"]) == 2_000_000
          and D(com["total_value"]) == 3_000_000 and com["operating_status"] == "Active"
          and com["theme"] == "Robotics" and D(det["book_value"]) == 1_800_000
          and D(com["invested"][1]["total_raise"]) == 20_000_000 and D(com["invested"][1]["pre_money"]) == 60_000_000,
          "company sheet: type, round, security, cost, value, status; the summary tab fills book value "
          "without overriding the sheet's theme; rounds carry total raise and pre-money")
    again = reprocess_submission(res["submission_id"])
    rep = client.get("/api/portfolio?as_of=2026-Q2").json()
    n_calls = len([c for c in {x["entity"]: x for x in rep["funds"]}["Fund 97"]["invested"] if c["source"] == "entered"])
    check(again["status"] == "ok" and n_calls == 2, "re-reading the workbook adds no duplicate investments")
    check(guess_entity_name(["Follow-on 1 Company Q EU/ Devices H1 2026 1 25", "Planned H1 2026"],
                            "Investment Portfolio.xlsx") == "Investment Portfolio",
          "a planning line never becomes an entity name in the general reader either")


def equity_investments_workbook() -> None:
    """The accounting team's Equity Investments Summary workbook (made-up
    figures): the Investments Summary table — open equity investments, funds,
    notes, closed investments — plus a backup tab per entity with the
    accounting labels and dated funding / securities tables."""
    import datetime as dt
    import io

    import openpyxl
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    near = lambda a, b: a is not None and abs(float(a) - float(b)) < 0.01
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Investments Summary"
    ws.append([None, "Equity Investments Summary Workbook", "Summarized presentation"])
    ws.append([None, dt.datetime(2026, 6, 28)])
    ws.append([])
    head = [None, "Investment", "Investment Balance", "Gross Investment / Funding", "Equity Income/(Loss)",
            "Return of Capital/Other Cash Proceeds", "MTM & Conversion Gain/(Loss)", "Acquisition Impact",
            "Cumulative Impairment", "Total Commitment", "Remaining Commitment", "Ownership %", "Initial Investment Date"]
    ws.append(head)
    ws.append([None, "Equity Investments"])
    ws.append(["Company 98A (2022)", "Company 98A", 2_979_349.49, 2_239_872.67, 0, 0, 739_476.82, 0, 0, 0, 0, 0.0682, dt.datetime(2022, 9, 21)])
    ws.append(["Company 98B (2017)", "Company 98B", 599_994, 1_999_994, 0, 0, 0, 0, -1_400_000, 0, 0, "< 1%", dt.datetime(2017, 10, 5)])
    ws.append(["Company 98G (Pre 2014)", "Company 98G", 0, 74_088, 0, 0, 0, 0, -74_088, 0, 0, "< 1%", "Pre 2014"])
    ws.append([None, None, 3_579_343.49, 4_313_954.67])
    ws.append([None, "Funds"])
    ws.append(["Fund 98 (2016)", "Fund 98", 4_831_951.58, 5_000_000, -168_048.42, 0, 0, 0, 0, 5_000_000, 0, 0.07298, dt.datetime(2016, 12, 15)])
    ws.append(["Fund 97 (2019)", "Fund 97", 5_623_205.10, 6_871_259, 91_307, -1_339_361, 0, 0, 0, 5_530_950, 0, 0.0192, dt.datetime(2019, 12, 20)])
    ws.append([None, None, 10_455_156.68])
    ws.append([None, "Note Receivable"])
    ws.append([None, "Investment", "Investment Balance", "Gross Investment / Funding", "Accrued Interest", "Return of Capital",
               "Note Conversion / Repayment", "Cumulative Impairment", None, "Initial Investment Date"])
    ws.append(["Note 9B (2025)", "Note 9B", 1_000_000, 1_000_000, 0, 0, 0, 0, 0, dt.datetime(2025, 10, 17)])
    ws.append([None, "Total Equity Investments", 15_034_500])
    ws.append([None, "Check", 0])
    ws.append([None, "CLOSED INVESTMENTS"])
    ws.append(head)
    ws.append([None, "Equity Investments"])
    ws.append(["Closed 9Q (2018)", "Closed 9Q", 1.16e-10, 2_000_000, 0, -2_797_000, 797_000, 0, 0, 0, 0, None, None])
    ws.append([None, "Other"])
    ws.append([None, "Closed 9Z", 0, 59_021_700, 5_602_260, 0, 0, -64_624_000, 0, 0, 0, None, None])
    ws.append([None, "Note Receivable"])
    ws.append([None, "Investment", "Investment Balance", "Gross Investment / Funding", "Accrued Interest", "Return of Capital",
               "Note Conversion / Repayment", "Cumulative Impairment"])
    ws.append(["Company 98A (2021 Convertible Note)", "Company 98A", 0, 1_000_000, 21_041, 0, -1_021_041, 0])
    f = wb.create_sheet("Fund 98")
    for r in (["i"], [], ["Entity Type", "Exempted Limited Partnership"], ["Method of Accounting", "Equity Method"],
              ["GL Account", 190645], ["Company Code", 1220], ["% Ownership", 0.07298, "Confirmed"],
              ["Initial Investment Date", dt.datetime(2016, 12, 15)], ["Terms", "- $300K initial investment with $5M funding commitment"],
              ["Total Commitment", 5_000_000], ["Remaining Commitment", 0], ["Funding", "Amount", "Investment Date"],
              ["Initial Investment", 300_000, dt.datetime(2016, 12, 15)], ["Capital Call", 212_440, dt.datetime(2017, 11, 1)],
              ["Capital Call", 4_487_560, dt.datetime(2019, 1, 15)], ["Total Funding", 5_000_000],
              ["Equity Income/(Loss)", "Amount"], [dt.datetime(2017, 3, 31), -73_015.9], ["Total Equity Income/(Loss)", -168_048.42],
              ["Investment Balance", 4_831_951.58]):
        f.append(r)
    co = wb.create_sheet("Company 98A")
    for r in (["Company 98A"], [], ["Entity Type", "Corporation"], ["Method of Accounting", "Measurement Alternative"],
              ["GL Account", 190645], ["Company Code", 1220], ["% Ownership - initial investment", 0.0778],
              ["% Ownership - 6/30/2025", 0.074], ["% Ownership - 12/31/2025", 0.0682, "Cap table, October 2025"],
              ["Investment", "Investment Value", "Cash Outlay", "MTM of Shares", "Investment Date", "# of Equity Shares", "Price", "Terms"],
              ["Company 98A Series Seed-2 Preferred Shares", 1_097_254.9, 1_000_000, 97_254.9, dt.datetime(2022, 9, 21), 1_307_189, 0.765, "- Convertible"],
              ["Company 98A Series Seed-2 Preferred Shares", 642_222, 0, 642_222, dt.datetime(2025, 12, 1), 1_307_189, 0.8394, "- marked up"],
              ["Company 98A Series A Preferred Shares", 1_239_872.67, 1_239_872.67, None, dt.datetime(2025, 10, 7), 931_745, 1.3307, "- Convertible"],
              [None, 2_979_349.49, 2_239_872.67, 739_476.82]):
        co.append(r)
    two = wb.create_sheet("Closed 9Q & 9R")
    for r in (["Closed 9Q"], [], ["Entity Type", "Corporation"], ["Method of Accounting", "Measurement Alternative"],
              ["GL Account", 190645], ["Company Code", 1220], ["% Ownership", 0.009, "Cap table"],
              ["Investment", "Investment Balance", "Cash Outlay", "Return of Capital", "MTM Gain/(Loss)", "Investment Date"],
              ["Closed 9Q Series B-1 Preferred Shares", 1_333_333, 1_333_333, None, None, dt.datetime(2018, 6, 26)],
              ["Closed 9Q Series B-2 Preferred Shares", 666_667, 666_667, None, None, dt.datetime(2019, 3, 12)],
              ["Acquisition", -2_000_000, 0, -2_797_000, 797_000, dt.datetime(2021, 9, 20)],
              [None, 0, 2_000_000, -2_797_000, 797_000], [], [],
              ["Closed 9R"], [], ["Entity Type", "Corporation"], ["Method of Accounting", "MTM each reporting period"],
              ["GL Account", 190642], ["Company Code", 1220],
              ["Investment", "Total Investment", "Cash Outlay/Cash Proceeds", "MTM of Shares", "Value", "Investment Date"],
              ["Closed 9R Common Stock", 2_330_410, 0, 2_330_410, 2_330_410, dt.datetime(2021, 9, 20)],
              ["Closed 9R sale of all shares", -2_411_500, 2_032_570, -378_931, None, dt.datetime(2021, 12, 9)]):
        two.append(r)
    wrong = wb.create_sheet("Fund 97")
    for r in (["Fund 97"], [], ["Entity Type", "Limited Partnership"], ["Funding", "Amount", "Investment Date"],
              ["Capital Call", 60_000_000, dt.datetime(2020, 1, 1)], ["Capital Call", 78_000_000, dt.datetime(2021, 1, 1)],
              ["Total Funding", 138_000_000]):
        wrong.append(r)
    sap = wb.create_sheet("SAP")
    sap.append(["Update quarterly"]); sap.append(["Account", "P12 FY25", "P3 FY26"]); sap.append([190644, 13_353_400, 13_334_400])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/upload", files={"files": ("Equity Investments Summary Workbook - Jun26 (test).xlsx", buf.getvalue())})
    res = r.json()["results"][0]
    names = {e["entity"] for e in res.get("entities", [])}
    check(res["status"] == "ok" and res["report_type"] == "master_workbook" and res["period"] == "2026-Q2"
          and names == {"Company 98A", "Company 98B", "Company 98G", "Fund 98", "Fund 97", "Note 9B", "Closed 9Q"}
          and "Closed 9Z" not in names and "Closed 9R" not in names and "SAP" in " ".join(res["warnings"]),
          "Equity Investments Summary: open positions, funds, notes and closed investments read; the "
          "'Other' block, closed notes and tabs without a summary row left out; the period from the workbook date")
    rep = client.get("/api/portfolio?as_of=2026-Q2").json()
    funds = {x["entity"]: x for x in rep["funds"]}
    cos = {x["entity"]: x for x in rep["companies"]}
    D = lambda x: Decimal(str(x)) if x is not None else None
    f98, f97 = funds["Fund 98"], funds["Fund 97"]
    check(D(f98["drawn_to_date"]) == 5_000_000 and D(f98["commitment"]) == 5_000_000
          and D(f98["investment_balance"]) == Decimal("4831951.58") and D(f98["future_draw"]) == 0
          and D(f98["accounting"]["ownership_pct"]) == Decimal("7.298") and f98["accounting"]["gl_account"] == "190645"
          and f98["accounting"]["initial_investment_date"] == "2016-12-15"
          and len([c for c in f98["invested"] if c["source"] == "entered"]) == 3
          and D(f97["distribution_to_date"]) == 1_339_361 and D(f97["drawn_to_date"]) == 6_871_259
          and not [c for c in f97["invested"] if Decimal(str(c["amount"])) in (60_000_000, 78_000_000)]
          and any("Fund 97" in w and "calls not imported" in w for w in res["warnings"]),
          "funds: funding, commitment, balance, remaining commitment, distributions (sign dropped), ownership "
          "from a fraction, dated capital calls from the tab named after the fund; a tab whose calls don't "
          "add up to the summary's funding is named and its calls left out")
    a, b, g, q = cos["Company 98A"], cos["Company 98B"], cos["Company 98G"], cos["Closed 9Q"]
    da, db_, dq = a["details"], b["details"], q["details"]
    check(D(a["total_invested"]) == Decimal("2239872.67") and D(da["book_value"]) == Decimal("2979349.49")
          and da["holding_status"] == "Current" and D(da["ownership_pct"]) == Decimal("6.82")
          and a["security"] == "Series A Preferred Shares" and da["accounting_method"] == "Measurement Alternative"
          and len([c for c in a["invested"] if c["source"] == "entered"]) == 2
          and db_["holding_status"] == "Impaired" and D(db_["impairment"]) == 1_400_000 and "ownership_pct" not in db_
          and g["last_investment_date"] in (None, "") and "last_investment_date" not in g["details"],
          "companies: cost from funding, balance as book value, impairment, the latest dated ownership, "
          "the latest security, two real outlays as investments (a mark-up is not one), 'Pre 2014' is no date")
    check(q["exited"] and dq["holding_status"] == "Exit" and D(q["exit"]["proceeds"]) == 2_797_000
          and D(q["total_invested"]) == 2_000_000 and len([c for c in q["invested"] if c["source"] == "entered"]) == 2
          and dq.get("operating_status") == "Inactive",
          "a closed investment: exited with its proceeds, two outlays, the acquisition line not an investment")
    S = client.get("/api/portfolio/summary?as_of=2026-Q2").json()
    line = {ln["entity"]: ln for ln in S["lines"]}
    check(line["Closed 9Q"]["status"] == "exited" and near(line["Closed 9Q"]["realized"], 797_000)
          and line["Company 98B"]["status"] == "impaired" and near(line["Company 98B"]["book"], 599_994),
          "the summary places them: a closed position's realized gain, an impaired position's book value")


def period_ranges() -> None:
    """Direct Investment Financials over any run of quarters: flows added up,
    cash as of the last quarter, burn over the months covered, missing
    quarters named; the export carries the range as one more column."""
    import io

    import openpyxl
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    def manual(q, metric, value):
        client.post("/api/manual-values", json={"entity": "Company 95R", "report_type": "company_financials",
                                                "period": q, "metric": metric, "value": value, "scale": 1000})
    for q, rev, opex, ocf, capex, cash in (("2025-Q4", "100", "300", "(250)", "(50)", "2,000"),
                                           ("2026-Q1", "140", "330", "(280)", "(20)", "1,700")):
        for m, v in (("revenue", rev), ("operating_expense", opex), ("operating_cash_flow", ocf),
                     ("capex", capex), ("ending_cash", cash)):
            manual(q, m, v)
    R = client.get("/api/direct-investments?start=2025-Q4&end=2026-Q2").json()
    key = R["range"]["key"]
    co = next(c for c in R["companies"] if c["company"] == "Company 95R")
    col = co["columns"][key]
    a = col["actual"]
    D = lambda x: Decimal(str(x))
    check(R["year"] == 2026 and key == "2025-10..2026-06" and len(R["range"]["months"]) == 9
          and R["range"]["quarters"] == ["2025-Q4", "2026-Q1", "2026-Q2"]
          and key in R["periods"] and D(a["revenue"]) == 240_000 and D(a["operating_expense"]) == 630_000
          and D(a["ending_cash"]) == 1_700_000 and col["months"] == 6
          and col["missing"] == ["2026-04", "2026-05", "2026-06"],
          "a range of quarters across years: revenue and opex added up, cash as of the last month-end "
          "with one, six months covered, the months without figures named")
    check(D(a["gross_monthly_burn"]) == 105_000 and D(a["net_monthly_burn"]) == 100_000
          and a["gross_runway"] == 16 and a["net_runway"] == 17
          and any(i["kind"] == "missing" and "Apr'26" in i["message"] for i in col["issues"]),
          "burn = flows ÷ months covered, runway from the last cash; the gap is flagged")
    R2 = client.get("/api/direct-investments?start=2026-Q1&end=2025-Q4").json()
    check(R2["range"]["key"] == "2025-10..2026-03" and R2["companies"] and
          client.get("/api/direct-investments?start=2025-Q4&end=2026").status_code == 400
          and client.get("/api/direct-investments?start=2010-Q1&end=2026-Q1").status_code == 400,
          "from/to in either order; a bad period or a range over ten years is refused")
    x = client.get("/api/exports/direct-investment-financials.xlsx?start=2025-Q4&end=2026-Q2")
    wb = openpyxl.load_workbook(io.BytesIO(x.content))
    ws = next(w for w in wb.worksheets if w.title.startswith("Company 95R"))
    heads = [c.value for c in ws[1] if c.value]
    rev_row = next(r for r in range(3, 12) if ws.cell(r, 1).value == "Revenue")
    check("Oct'25–Jun'26" in heads and abs(float(ws.cell(rev_row, 13).value) - 240.0) < 0.001
          and "2025-10 to 2026-06" in x.headers.get("content-disposition", ""),
          "the workbook carries the range as a sixth Budget | Actual pair, in $000, named in the file")

    # any run of months: QuickBooks exports print a column per month, so a quarter
    # partly in the range counts month by month; a quarter-only figure is named
    samples = ROOT / "samples"
    for name in ("Company 90A_Financials_Q2_2025.xlsx", "Company 90A_Financials_Q3_2025.xlsx",
                 "Company 90A_Financials_Q4_2025.xlsx", "Company 90A_Financials_Q1_2026.xlsx"):
        if not (samples / name).exists():
            import subprocess
            subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)
        r = process_path(samples / name, allow_duplicate=True)
        for f in db.get_facts(r["submission_id"]):
            if f["status"] == "needs_review" and f["value"] is not None:
                db.update_fact(f["id"], status="confirmed")
    R = client.get("/api/direct-investments?start=2025-11&end=2026-02").json()
    key = R["range"]["key"]
    cols = {c["company"]: c["columns"][key] for c in R["companies"]}
    a90 = cols["Company 90A"]
    act = a90["actual"]
    check(key == "2025-11..2026-02" and R["range"]["label"] == "Nov'25–Feb'26" and a90["months"] == 4
          and D(act["revenue"]) == 18_000 + 22_000 + 20_000 + 25_000
          and D(act["operating_expense"]) == 330_000 * 2 + 350_000 * 2
          and D(act["ending_cash"]) == 2_700_000 and a90["cash_as_of"] == "2026-02"
          and a90["split_from"] == ["2025-Q4", "2026-Q1"] and not a90["issues"],
          "Nov'25 to Feb'26: two months from each quarter's monthly columns, cash as of the end of February")
    check(D(act["gross_monthly_burn"]) == 340_000 and D(act["net_monthly_burn"]) == 317_500,
          "burn over the four months: opex ÷ 4, −(operating cash flow + capex) ÷ 4")
    r95 = cols.get("Company 95R")
    check(r95 is not None and r95["months"] == 0 and r95["actual"]["revenue"] is None
          and D(r95["actual"]["ending_cash"]) == 2_000_000 and r95["cash_as_of"] == "2025-12"
          and sum("only as a quarter total" in i["message"] for i in r95["issues"]) == 2
          and any("as of Dec'25" in i["message"] for i in r95["issues"]),
          "a company whose quarters have no monthly columns: nothing guessed, the quarter named as unsplittable")
    R12 = client.get("/api/direct-investments?start=2025-04&end=2026-03").json()
    a12 = next(c for c in R12["companies"] if c["company"] == "Company 90A")["columns"][R12["range"]["key"]]
    check(a12["months"] == 12 and D(a12["actual"]["revenue"]) == 28_000 + 37_000 + 55_000 + 75_000
          and not [i for i in a12["issues"] if i["kind"] == "missing"],
          "a full year (Apr'25 to Mar'26): revenue across twelve months in one figure")


def xirr_roots() -> None:
    """The solver returns the economically meaningful rate when the flows have
    several roots, and the plain answer when they have one."""
    from datetime import date as D

    from app.performance import xirr

    one = xirr([(D(2020, 1, 1), -100.0)] + [(D(2020 + k, 1, 1), 10.0) for k in range(1, 5)]
               + [(D(2025, 1, 1), 110.0)])
    two = xirr([(D(2020, 1, 1), -100.0), (D(2021, 1, 1), 230.0), (D(2022, 1, 1), -132.0)])
    # years are days ÷ 365, so leap days move the answer a little from a round 10%
    check(one is not None and abs(one - 0.10) < 5e-3, "a 10% bond-like flow solves to 10%")
    check(two is not None and abs(two - 0.10) < 5e-3,
          "flows with two roots (10% and 20%) give the one nearest zero, never a −99% artefact")
    # many interleaved calls and distributions, each year's return modest: the
    # combined answer must sit near the per-fund answers, not at −99%
    flows = []
    for k in range(12):
        flows.append((D(2016 + k // 4, 1 + 3 * (k % 4), 1), -250_000.0))
        flows.append((D(2017 + k // 4, 1 + 3 * (k % 4), 1), 90_000.0))
    flows.append((D(2026, 6, 30), 2_600_000.0))
    r = xirr(flows)
    check(r is not None and -0.05 < r < 0.25, f"interleaved calls and distributions: {r} is a plausible rate")


def irr_section() -> None:
    """IRR from dated flows: an exit with a known answer, a fund from its
    calls, distributions and balance, the splits, and what is left out."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    P = "/api/portfolio/"
    client.post(P + "Company 96B/details", json={"kind": "direct_investment", "total_invested": "1m",
                                                 "exit_date": "2024-01-01", "exit_proceeds": "2m",
                                                 "exit_value": "2m", "portfolio_group": "Life Sciences"})
    client.post(P + "Company 96B/calls", json={"date": "2021-01-01", "description": "Series A", "amount": "1000000"})
    client.post(P + "Fund 96/details", json={"kind": "fund", "drawn_to_date": "4m", "distribution_to_date": "1m",
                                             "investment_balance": "5m", "initial_investment_date": "2020-01-01"})
    client.post(P + "Company 96C/details", json={"kind": "direct_investment", "theme": "no money in yet"})
    R = client.get("/api/irr?as_of=2026-Q2").json()
    ents = {e["entity"]: e for e in R["entities"]}
    b, f = ents.get("Company 96B"), ents.get("Fund 96")
    check(b is not None and abs(b["irr_pct"] - 26.0) < 0.3 and not b["estimated"] and b["status"] == "exited"
          and [x["kind"] for x in b["flows"]] == ["call", "proceeds"] and b["group"] == "Life Sciences",
          "an exit: $1M in on 2021-01-01, $2M out on 2024-01-01 → 26% IRR, from two dated flows")
    check(f is not None and f["estimated"] and f["irr_pct"] is not None and f["irr_pct"] > 0
          and {x["kind"] for x in f["flows"]} == {"call", "distribution", "value"}
          and any("initial investment date" in n for n in f["notes"]),
          "a fund with no dated calls: total invested placed on its initial date, undated distributions "
          "and the balance at the quarter end — marked as an estimate, with the reason")
    g = R["groups"]
    check(g["life_sciences"]["n"] >= 1 and "Company 96B" in g["life_sciences"]["members"]
          and "Fund 96" in g["funds"]["members"] and "Company 96B" in g["direct"]["members"]
          and g["all"]["n"] + len(g["all"]["no_rate"]) == len(R["entities"]) and g["all"]["irr_pct"] is not None
          and set(R["splits"]) == {"all", "group", "kind"}
          and g["core"]["n"] + g["life_sciences"]["n"] == g["all"]["n"]
          and g["direct"]["n"] + g["funds"]["n"] == g["all"]["n"],
          "groups: Core + Life Sciences = all = Direct + Funds, each with its own IRR")
    check(any(x["entity"] == "Company 96C" for x in R["left_out"]) and "Company 96C" not in ents,
          "an entity with no investment on file is listed as left out, not given a figure")
    g2 = R["groups"]
    ents_rates = [e["irr_pct"] for e in R["entities"] if e["irr_pct"] is not None]
    check(g2["all"]["irr_pct"] > -50 and g2["funds"]["irr_pct"] is None or g2["funds"]["irr_pct"] > -50,
          "group IRRs are not −99% artefacts when the members' own rates are ordinary")
    check("2026-Q2" in R["quarters"], "the quarters on offer include those of entered values and workbooks")
    bad = client.get("/api/irr?as_of=2026")
    check(bad.status_code == 400, "the quarter is validated")


def irr_windows_and_precedence() -> None:
    """The arithmetic behind held and closed positions checked by hand; IRR over
    a window of quarters; a vintage filter; a statement newer than a workbook
    supersedes its figures; Overview shows the IRR section's fund figure."""
    from datetime import date as DT

    from fastapi.testclient import TestClient

    from app.main import app
    from app.performance import xirr

    client = TestClient(app)
    P = "/api/portfolio/"
    # ---- held: $1M in on 2024-01-01, worth $1.5M at 2026-06-30 -> 1.5^(1/t) − 1
    client.post(P + "Company 97H/details", json={"kind": "direct_investment", "total_invested": "1m",
                                                 "total_value": "1.5m"})
    client.post(P + "Company 97H/calls", json={"date": "2024-01-01", "description": "Seed", "amount": "1000000"})
    t = (DT(2026, 6, 30) - DT(2024, 1, 1)).days / 365
    expect = (1.5 ** (1 / t) - 1) * 100
    R = client.get("/api/irr?as_of=2026-Q2").json()
    h = {e["entity"]: e for e in R["entities"]}["Company 97H"]
    check(abs(h["irr_pct"] - round(expect, 1)) < 0.11 and [f["kind"] for f in h["flows"]] == ["call", "value"]
          and h["invested"] == 1_000_000 and h["value"] == 1_500_000 and h["returned"] == 0,
          f"held: one call and the value at the quarter end → {expect:.1f}% by hand, {h['irr_pct']}% here")
    # ---- closed with escrow: proceeds on the exit date, escrow as still to come
    client.post(P + "Company 97X/details", json={"kind": "direct_investment", "total_invested": "2m",
                                                 "exit_date": "2025-06-30", "exit_proceeds": "3m", "exit_escrow": "0.3m"})
    client.post(P + "Company 97X/calls", json={"lines": "2022-06-30\tSeries A\t1,200,000\n2023-06-30\tSeries B\t800,000"})
    R = client.get("/api/irr?as_of=2026-Q2").json()
    x = {e["entity"]: e for e in R["entities"]}["Company 97X"]
    by_hand = xirr([(DT(2022, 6, 30), -1_200_000), (DT(2023, 6, 30), -800_000), (DT(2025, 6, 30), 3_000_000),
                    (DT(2026, 6, 30), 300_000)])
    check(x["status"] == "exited" and [f["kind"] for f in x["flows"]] == ["call", "call", "proceeds", "value"]
          and abs(x["irr_pct"] - round(by_hand * 100, 1)) < 0.11 and x["returned"] == 3_000_000 and x["value"] == 300_000,
          "closed: two calls, proceeds on the exit date, escrow still to come — matches the flows by hand")
    # ---- dated investments that don't add up to the paid-in are flagged
    client.post(P + "Company 97P/details", json={"kind": "direct_investment", "total_invested": "3m", "total_value": "3m"})
    client.post(P + "Company 97P/calls", json={"date": "2024-01-01", "description": "Seed", "amount": "1000000"})
    R = client.get("/api/irr?as_of=2026-Q2").json()
    pp = {e["entity"]: e for e in R["entities"]}["Company 97P"]
    calls = [f for f in pp["flows"] if f["kind"] == "call"]
    check(pp["estimated"] and any("add up to 1,000,000, but paid-in is 3,000,000" in n for n in pp["notes"])
          and len(calls) == 1 and calls[0]["amount"] == -3_000_000 and calls[0]["date"] == "2024-01-01"
          and pp["invested"] == 3_000_000,
          "calls that don't add up to the paid-in: the paid-in stands in on the first call's date, marked, with the reason")
    # a wildly wrong call must not blank the portfolio's figure
    client.post(P + "Company 97Z/details", json={"kind": "direct_investment", "total_invested": "2m", "total_value": "2.5m"})
    client.post(P + "Company 97Z/calls", json={"date": "2023-01-01", "description": "typo", "amount": "2000000000"})
    R = client.get("/api/irr?as_of=2026-Q2").json()
    z = {e["entity"]: e for e in R["entities"]}["Company 97Z"]
    check(z["invested"] == 2_000_000 and R["groups"]["all"]["irr_pct"] is not None and R["groups"]["direct"]["irr_pct"] is not None,
          "a call a thousand times too large does not reach the group IRRs")
    # ---- a fund over a window of quarters: opening and closing from the roll-forward
    for q, beg, fund, dist, end in (("2024-Q4", "0", "2,000,000", "0", "2,000,000"),
                                    ("2025-Q1", "2,000,000", "0", "(100,000)", "2,100,000"),
                                    ("2025-Q2", "2,100,000", "500,000", "0", "2,700,000"),
                                    ("2025-Q3", "2,700,000", "0", "(200,000)", "2,750,000")):
        for metric, value in (("beginning_balance", beg), ("funding", fund), ("return_of_capital", dist),
                              ("ending_balance", end)):
            client.post("/api/manual-values", json={"entity": "Fund 97W", "report_type": "investment_roll_forward",
                                                    "period": q, "metric": metric, "value": value})
    client.post(P + "Fund 97W/details", json={"kind": "fund", "initial_investment_date": "2024-11-15"})
    R = client.get("/api/irr?as_of=2025-Q3&start=2025-Q1&end=2025-Q2").json()
    w = {e["entity"]: e for e in R["entities"]}["Fund 97W"]
    kinds = [(f["kind"], f["date"], round(f["amount"])) for f in w["flows"]]
    by_hand = xirr([(DT(2025, 1, 1), -2_000_000), (DT(2025, 3, 31), 100_000), (DT(2025, 6, 30), -500_000),
                    (DT(2025, 6, 30), 2_700_000)])
    check(R["window"] == {"start": "2025-Q1", "end": "2025-Q2"}
          and kinds == [("opening", "2025-01-01", -2_000_000), ("distribution", "2025-03-31", 100_000),
                        ("call", "2025-06-30", -500_000), ("value", "2025-06-30", 2_700_000)]
          and abs(w["irr_pct"] - round(by_hand * 100, 1)) < 0.11,
          "a window Q1–Q2 2025: the balance at end-2024 opens it, the Q2 balance closes it, flows between")
    R = client.get("/api/irr?as_of=2025-Q3&start=2024-Q1&end=2024-Q2").json()
    check("Fund 97W" in [x["entity"] for x in R["left_out"]] or "Fund 97W" not in [e["entity"] for e in R["entities"]],
          "a window before the fund existed gives it no figure")
    bad = client.get("/api/irr?as_of=2025-Q3&start=2025-Q1")
    check(bad.status_code == 400, "a window needs both ends")
    # ---- vintage filter on the portfolio and the IRR
    R = client.get("/api/irr?as_of=2026-Q2&vintage_from=2024&vintage_to=2024").json()
    names = {e["entity"] for e in R["entities"]}
    check("Company 97H" in names and "Company 97P" in names and "Company 97X" not in names and "Fund 97W" in names
          and "Company 97X" in R["vintage"]["left_out"],
          "first invested in 2024 only: the 2024 positions stay, the 2022 one is left out")
    PF = client.get("/api/portfolio?as_of=2026-Q2&vintage_from=2022&vintage_to=2023").json()
    check({c["entity"] for c in PF["companies"]} == {"Company 97X"} or
          ("Company 97X" in {c["entity"] for c in PF["companies"]} and "Company 97H" not in {c["entity"] for c in PF["companies"]}),
          "the Investment Portfolio filtered to 2022–2023 vintages")
    # ---- a statement newer than the workbook supersedes the workbook's figure
    client.post(P + "Fund 97W/details", json={"investment_balance": "2,000,000", "drawn_to_date": "2,500,000"})
    db.set_details("Fund 97W", {"workbook_as_of": "2024-Q4", "workbook_fields": ["investment_balance", "drawn_to_date"]})
    PF = client.get("/api/portfolio?as_of=2025-Q3").json()
    f = {x["entity"]: x for x in PF["funds"]}["Fund 97W"]
    check(Decimal(str(f["investment_balance"])) == 2_750_000 and "investment_balance" in f["superseded"]
          and "newer than the workbook" in f["sources"]["investment_balance"]
          and not any(i["kind"] == "mismatch" and "Investment Balance" in i["message"] for i in f["issues"]),
          "a roll-forward for 2025-Q3 beats the workbook's 2024-Q4 balance, without a mismatch flag")
    R = client.get("/api/irr?as_of=2025-Q3").json()
    w = {e["entity"]: e for e in R["entities"]}["Fund 97W"]
    check(round(w["value"]) == 2_750_000, "and the IRR uses the newer balance, so a new statement moves the IRR")
    # ---- every figure states what it covers, document by document
    R = client.get("/api/irr?as_of=2025-Q3").json()
    w = {e["entity"]: e for e in R["entities"]}["Fund 97W"]
    cov = {(c["kind"], c["document"]) for c in w["coverage"]}
    check(w["first_date"] == "2024-12-31" and w["last_date"] == "2025-09-30"
          and any(k == "call" and "roll-forward" in d for k, d in cov)
          and any(k == "distribution" for k, _d in cov) and any(k == "value" for k, _d in cov),
          "the IRR row says which dates and documents its calls, distributions and balance come from")
    client.post(P + "Fund 97W/calls", json={"date": "2024-11-15", "description": "Initial", "amount": "2000000"})
    R = client.get("/api/irr?as_of=2025-Q3").json()
    w = {e["entity"]: e for e in R["entities"]}["Fund 97W"]
    hand = next(c for c in w["coverage"] if c["document"] == "entered by hand")
    check(hand["kind"] == "call" and hand["from"] == "2024-11-15" and w["first_date"] == "2024-11-15",
          "a call typed in is named as entered by hand, with its date")
    # ---- the Investment Portfolio's window: activity within those quarters
    PF = client.get("/api/portfolio?as_of=2025-Q3&start=2025-Q1&end=2025-Q2").json()
    f = {x["entity"]: x for x in PF["funds"]}["Fund 97W"]
    check(PF["window"] == {"start": "2025-Q1", "end": "2025-Q2"} and Decimal(str(f["window"]["drawn"])) == 500_000
          and Decimal(str(f["window"]["distributed"])) == 100_000 and f["window"]["quarters"] == ["2025-Q1", "2025-Q2"]
          and Decimal(str(f["investment_balance"])) == 2_750_000,
          "a window on the Investment Portfolio: drawn and distributed within it; the balance stays as of the quarter")
    # ---- Overview shows the IRR section's fund figure
    perf = client.get("/api/performance").json()["portfolio"]
    R = client.get("/api/irr").json()
    check(perf["irr_pct"] == R["groups"]["funds"]["irr_pct"] and perf["irr_pct"] is not None,
          "Overview's IRR is the IRR section's fund figure, from the same flows")


def currencies_and_fx() -> None:
    """The accounting workbook's EUR | FX Rate | USD tables: a euro fund and a
    TWD fund (quoted the other way round) keep their own currency; each
    transaction's rate is USD ÷ native; quarterly rates come from the equity-
    income rows; the entity's IRR is in its own currency, the groups in USD;
    the Investment Portfolio workbook's USD figures never override it; one
    investment listed by both workbooks is one row."""
    import datetime as dt
    import io

    import openpyxl
    from fastapi.testclient import TestClient

    from app.main import app
    from app.performance import xirr

    client = TestClient(app)
    D = lambda x: Decimal(str(x))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Investments Summary"
    ws.append([None, "Equity Investments Summary Workbook"]); ws.append([None, dt.datetime(2026, 6, 28)]); ws.append([])
    ws.append([None, "Investment", "Investment Balance", "Gross Investment / Funding", "Equity Income/(Loss)",
               "Return of Capital/Other Cash Proceeds", "MTM & Conversion Gain/(Loss)", "Acquisition Impact",
               "Cumulative Impairment", "Total Commitment", "Remaining Commitment", "Ownership %", "Initial Investment Date"])
    ws.append([None, "Funds"])
    # summary figures are USD
    ws.append(["Fund 95E (2022)", "Fund 95E", 2_300_000, 1_700_000, 600_000, 0, 0, 0, 0, 2_200_000, 550_000, 0.02, dt.datetime(2022, 3, 1)])
    ws.append(["Fund 95T (2022)", "Fund 95T", 1_000_000, 1_000_000, 0, 0, 0, 0, 0, 1_000_000, 0, 0.05, dt.datetime(2022, 6, 1)])
    e = wb.create_sheet("Fund 95E")
    for r in (["Fund 95E"], [], ["Entity Type", "Limited Partnership"], ["Method of Accounting", "Equity Method"],
              ["Initial Investment Date", dt.datetime(2022, 3, 1)], [],
              [None, "EUR", "FX Rate", "USD"], ["Total Commitment", 2_000_000, 1.1, 2_200_000],
              ["Remaining Commitment", 500_000, 1.1, 550_000], [],
              ["Funding", "EUR", "FX Rate", "USD", "Investment Date"],
              ["Initial Investment", 1_000_000, 1.0, 1_000_000, dt.datetime(2022, 3, 1)],
              ["Capital Call", 500_000, 1.4, 700_000, dt.datetime(2024, 3, 1)], [],
              ["Total Funding", 1_500_000, None, 1_700_000], [],
              ["Equity Income/(Loss)", "EUR", "FX Rate", "USD"],
              [dt.datetime(2025, 12, 31), 200_000, 1.15, 230_000], [dt.datetime(2026, 3, 31), 100_000, 1.2, 120_000]):
        e.append(r)
    t = wb.create_sheet("Fund 95T")
    for r in (["Fund 95T"], [], ["Entity Type", "Limited Partnership"], ["Method of Accounting", "Equity Method"], [],
              [None, "TWD", "FX Rate", "USD"], ["Total Commitment", 30_000_000, 30.0, 1_000_000],
              ["Remaining Commitment", 0, 30.0, 0], [],
              ["Funding", "TWD", "FX Rate", "USD", "Investment Date"],
              ["Initial Investment", 30_000_000, 30.0, 1_000_000, dt.datetime(2022, 6, 1)],
              ["Total Funding", 30_000_000, None, 1_000_000]):
        t.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    res = client.post("/api/upload", files={"files": ("Equity Investments Summary - Jun26 (fx test).xlsx", buf.getvalue())}).json()["results"][0]
    det = db.get_details("Fund 95E")
    rates = {q: D(v) for q, v in (x.split("=") for x in det["usd_rates"].split("; "))}
    check(res["status"] == "ok" and det["currency"] == "EUR" and db.get_details("Fund 95T")["currency"] == "TWD"
          and rates["2022-Q1"] == 1 and rates["2024-Q1"] == D("1.4") and rates["2025-Q4"] == D("1.15")
          and rates["2026-Q1"] == D("1.2"),
          "the tab's currency is kept; each transaction's and each quarter's rate is USD ÷ native")
    calls = {c["call_date"]: c for c in db.get_capital_calls("Fund 95T")}
    check(calls["2022-06-01"]["currency"] == "TWD" and D(calls["2022-06-01"]["amount"]) == 30_000_000
          and D(calls["2022-06-01"]["amount_usd"]) == 1_000_000,
          "a TWD call (quoted TWD per dollar) is kept as TWD 30,000,000 = USD 1,000,000, not $30M")
    rep = client.get("/api/portfolio?as_of=2026-Q2").json()
    f = {x["entity"]: x for x in rep["funds"]}
    fe, ft = f["Fund 95E"], f["Fund 95T"]
    check(fe["currency"] == "USD" and fe["native_currency"] == "EUR" and D(fe["commitment"]) == 2_200_000
          and D(fe["drawn_to_date"]) == 1_700_000 and D(fe["future_draw"]) == 550_000
          and D(fe["investment_balance"]) == 2_300_000
          and ft["currency"] == "USD" and ft["native_currency"] == "TWD" and D(ft["drawn_to_date"]) == 1_000_000,
          "the Investment Portfolio shows every fund in USD — the accounting summary's figures — "
          "naming the currency it reports in (EUR, TWD)")
    calls_usd = [c for c in fe["invested"] if c["source"] == "entered"]
    check([round(D(c["amount"])) for c in calls_usd] == [1_000_000, 700_000]
          and all(c["currency"] == "USD" and c["original"]["currency"] == "EUR" for c in calls_usd),
          "its euro calls are listed in USD at their recorded rates, the euro amount kept alongside")
    nat = fe.get("native") or {}
    check(nat.get("currency") == "EUR" and D(nat["commitment"]) == 2_000_000 and D(nat["drawn_to_date"]) == 1_500_000
          and D(nat["future_draw"]) == 500_000 and [round(D(c["amount"])) for c in nat["invested"]] == [1_000_000, 500_000]
          and "investment_balance" in nat["converted"] and "commitment" not in nat["converted"]
          and (ft.get("native") or {}).get("currency") == "TWD" and D(ft["native"]["drawn_to_date"]) == 30_000_000,
          "each fund's original figures ride along: the accounting tab's own EUR / TWD figures, exactly; a "
          "figure it gives only in USD is converted, and says so")
    h95 = client.get("/api/history/Fund 95E?as_of=2026-Q2").json()
    money = lambda facts: {x["label"]: round(float(x["value"])) for x in facts if x["kind"] == "money"}
    check(h95["currency"] == "USD" and money(h95["facts"])["Commitment"] == 2_200_000
          and h95["native"]["currency"] == "EUR" and money(h95["native"]["facts"])["Commitment"] == 2_000_000
          and money(h95["native"]["facts"])["Drawn to date"] == 1_500_000,
          "the company history has the same two views: US dollars, and the fund's own euros")
    # a window of quarters: a euro statement's funding and distributions in USD, like every figure
    from app import portfolio as pf
    real = pf.db.get_investments
    pf.db.get_investments = lambda: {"quarters": [{"entity_name": "Fund 95E", "period_label": "2026-Q1",
                                                   "period_order": 202601, "funding": 100_000.0,
                                                   "return_of_capital": -20_000.0, "dividends": 0.0}]}
    try:
        w = pf.with_window({"funds": [fe], "companies": [], "statement_currency": {"Fund 95E": "EUR"}},
                           "2026-Q1", "2026-Q1")["funds"][0]
    finally:
        pf.db.get_investments = real
    check(round(w["window"]["drawn"]) == 120_000 and round(w["window"]["distributed"]) == 24_000
          and round(w["native"]["window"]["drawn"]) == 100_000,
          "a window's funding and distributions from a euro statement are in USD at the quarter's rate, "
          "the euros alongside")
    usd_id = client.post("/api/portfolio/Fund 95E/calls", json={"date": "2026-08-01", "description": "Typed in dollars",
                                                                "amount": "1200000"}).json()["ids"][0]
    eur_id = client.post("/api/portfolio/Fund 95E/calls", json={"date": "2026-08-02", "description": "Typed in euros",
                                                                "amount": "1000000", "currency": "EUR"}).json()["ids"][0]
    typed = {c["id"]: c for c in db.get_capital_calls("Fund 95E")}
    bad = client.post("/api/portfolio/Fund 95E/calls", json={"date": "2026-08-03", "amount": "1", "currency": "dollars"})
    check(typed[usd_id]["currency"] == "USD" and typed[eur_id]["currency"] == "EUR" and bad.status_code == 400,
          "a row typed in by hand keeps the currency it was typed in: US dollars unless the form says "
          "otherwise (a euro fund's $1.2M is not taken for €1.2M)")
    for i in (usd_id, eur_id):
        client.delete(f"/api/portfolio/calls/{i}")
    R = client.get("/api/irr?as_of=2026-Q2").json()
    ent = {x["entity"]: x for x in R["entities"]}
    e95 = ent["Fund 95E"]
    usd_flows = [(dt.date.fromisoformat(x["date"]), x["amount"]) for x in e95["flows"]]
    check(e95["currency"] == "USD" and e95["native_currency"] == "EUR"
          and abs(e95["irr_pct"] - round(xirr(usd_flows) * 100, 1)) < 0.11
          and [round(x["usd"]) for x in e95["usd_flows"] if x["kind"] == "call"] == [-1_000_000, -700_000]
          and e95["usd"]["invested"] == 1_700_000 and e95["invested"] == 1_700_000
          and "Fund 95E" in R["groups"]["funds"]["members"] and "EUR" in R["groups"]["funds"]["currencies"],
          "IRR in USD: each euro call at its recorded rate, the fund's own return and its part of the groups alike")
    check([x["amount"] for x in e95["flows"]] == [x["usd"] for x in e95["usd_flows"]],
          "a fund's own IRR and the USD groups rest on the same flows")
    # the Investment Portfolio workbook (USD) never overrides the accounting workbook
    ip = openpyxl.Workbook()
    sh = ip.active
    sh.title = "Fund 95E"
    for r_, label, value in ((2, "Investment Name", "Example Euro Fund"), (3, "Investment Type", "LP"),
                             (7, "Commitment", 2.2), (13, "Drawn to Date", 1.7), (11, "Investment Balance", 2.3)):
        sh.cell(r_, 1, label); sh.cell(r_, 2, value)
    for c, h in zip("BCDEF", ("Date", "Current Round", "Amount Invested", "Total Raise", "Pre Money Valuation")):
        sh[f"{c}16"] = h
    sh["A17"] = "Invested"; sh["B17"] = dt.datetime(2022, 3, 31); sh["C17"] = "Initial"; sh["D17"] = 1.0
    sh["A18"] = "Invested"; sh["B18"] = dt.datetime(2024, 3, 31); sh["C18"] = "Call"; sh["D18"] = 0.7
    buf = io.BytesIO()
    ip.save(buf)
    client.post("/api/upload", files={"files": ("Investment Portfolio Jun'26 (fx test).xlsx", buf.getvalue())})
    rep = client.get("/api/portfolio?as_of=2026-Q2").json()
    fe = {x["entity"]: x for x in rep["funds"]}["Fund 95E"]
    entered = [c for c in fe["invested"] if c["source"] == "entered"]
    check(D(fe["drawn_to_date"]) == 1_700_000 and D(fe["commitment"]) == 2_200_000
          and fe["investment_name"] == "Example Euro Fund" and len(entered) == 2
          and all(c["doc_kind"] == "equity" for c in entered),
          "the Investment Portfolio workbook adds its name but leaves the accounting workbook's figures, "
          "and its two calls are recognised as the same investments (dates within weeks, USD within 1%)")
    S = client.get("/api/portfolio/summary?as_of=2026-Q2").json()
    funds_row = next(r for r in S["rows"] if r["kind"] == "fund")
    total = next(r for r in S["totals"] if r["label"] == "Total Investments")
    keys = [c["key"] for c in S["columns"]]
    check("irr_pct" in keys and "currencies" in keys and funds_row["irr_pct"] is not None
          and "EUR" in funds_row["currencies"] and "TWD" in funds_row["currencies"]
          and total["irr_pct"] is not None,
          "the Portfolio Summary carries an IRR per row and the currencies each row converts to USD")
    import openpyxl as _ox
    wbx = _ox.load_workbook(io.BytesIO(client.get("/api/exports/investment-portfolio.xlsx?as_of=2026-Q2").content))
    dash = wbx["Dashboard"]
    heads = [dash.cell(3, c).value for c in range(1, dash.max_column + 1)]
    check("IRR" in heads and "Currencies (→ USD)" in heads, "and so does the workbook's Dashboard sheet")


def accounting_tabs_cash_flows() -> None:
    """Every layout of the accounting workbook's backup tabs gives its dated cash
    flows, reconciled with the summary's funding: an "Investment Value" table
    (no Cash Outlay column) restated after anti-dilution, a note converting into
    shares (only its interest reconciles it), a conversion whose note is not on
    file and a repaid note, an undated purchase, a USD-first local-currency
    table; a EUR fund's distributions table; closed positions by disposition."""
    import datetime as dt
    import io
    from decimal import Decimal

    import openpyxl
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    D = lambda x: Decimal(str(x))
    dt_ = dt.datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Investments Summary"
    head = [None, "Investment", "Investment Balance", "Gross Investment / Funding", "Equity Income/(Loss)",
            "Return of Capital/Other Cash Proceeds", "MTM & Conversion Gain/(Loss)", "Acquisition Impact",
            "Cumulative Impairment", "Total Commitment", "Remaining Commitment", "Ownership %",
            "Initial Investment Date", "Impairment Date", "Disposition Date"]
    for r in ([None, "Equity Investments Summary Workbook"], [None, dt_(2026, 6, 28)], [], head,
              [None, "Equity Investments"],
              ["Company 71A (2017)", "Company 71A", 600_000, 2_000_000, 0, 0, 0, 0, -1_400_000, 0, 0, 0.01,
               dt_(2017, 10, 5), dt_(2025, 6, 25)],
              ["Company 71B (2019)", "Company 71B", 3_000_000, 2_120_000, 0, 0, 880_000, 0, 0, 0, 0, 0.02,
               dt_(2019, 11, 27)],
              ["Company 71C (Pre 2014)", "Company 71C", 0, 75_000, 0, 0, 0, 0, -75_000, 0, 0, None,
               "Pre 2014", dt_(2023, 3, 3)],
              ["Company 71E (2015)", "Company 71E", 2_500_000, 2_000_000, 0, 0, 500_000, 0, 0, 0, 0, 0.03,
               dt_(2015, 1, 10)],
              [None, "Held-to-Maturity (HTM) Debt Security"],
              ["Company 71D (2024)", "Company 71D", 500_000, 500_000, 0, 0, 0, 0, 0, 0, 0, 0.04, dt_(2024, 11, 20)],
              [None, "Funds"],
              ["Fund 71F (2022)", "Fund 71F", 2_000_000, 1_700_000, 600_000, -300_000, 0, 0, 0, 2_200_000, 550_000,
               0.02, dt_(2022, 3, 1)],
              [], [None, "CLOSED INVESTMENTS"], head, [None, "Equity Investments"],
              ["Closed 71X (2018)", "Closed 71X", 0, 2_000_000, 0, -2_800_000, 800_000, 0, 0, 0, 0, None, None,
               None, dt_(2021, 9, 20)],
              ["Closed 71Y (2021)", "Closed 71Y (Closed 71X Acq.)", 0, 0, 0, -1_000_000, 1_000_000, 0, 0, 0, 0,
               None, None, None, dt_(2023, 12, 11)],
              [None, "Note Receivable"],
              [None, "Investment", "Investment Balance", "Gross Investment / Funding", "Accrued Interest",
               "Return of Capital", "Note Conversion / Repayment", "Cumulative Impairment", "Other",
               "Initial Investment Date", "Impairment Date", "Disposition Date"],
              ["Closed 71Z (2016 Convertible Note)", "Closed 71Z", 0, 500_000, 50_000, 0, 0, -550_000, 0,
               dt_(2016, 9, 28), dt_(2019, 6, 30)],
              ["Company 71B (2019 Convertible Note)", "Company 71B", 0, 1_000_000, 120_000, 0, -1_120_000, 0, 0,
               dt_(2019, 11, 27)]):
        ws.append(r)

    def tab(name: str, rows: list) -> None:
        t = wb.create_sheet(name)
        for r in rows:
            t.append(r)
    entity = [["Entity Type", "Corporation"], ["Method of Accounting", "Measurement Alternative"], []]
    tab("Company 71A", [["Company 71A"], []] + entity + [
        ["Investment", "Investment Value", "Investment Date", "# of Equity Shares", "Price Per Share", "Terms"],
        ["Original investment"],
        ["Company 71A, Series A Preferred Shares, 1st tranche", 1_000_000, dt_(2017, 10, 5), 100, 1, "- Conversion into common"],
        ["Company 71A, Series A Preferred Shares, 2nd tranche", 1_000_000, dt_(2018, 8, 21), 100, 1, "- Conversion into common"],
        ["Anti-dilution clause triggered"],
        ["Company 71A, Series A Preferred Shares, 1st tranche", 999_998, dt_(2017, 10, 5), 300, 0.33],
        ["Company 71A, Series A Preferred Shares, 2nd tranche", 999_998, dt_(2018, 8, 21), 300, 0.33],
        ["Rounding", 4], ["Partial Impairment", -1_400_000, dt_(2025, 6, 25)], [None, 600_000]])
    tab("Company 71B", [["Company 71B"], [], ["Method of Accounting", "Note Receivable"],
        ["Initial Investment Date", dt_(2019, 11, 27)], [],
        ["Funding", "Amount", "Investment Date"], ["Initial note investment", 1_000_000, dt_(2019, 11, 27)],
        ["Total Funding", 1_000_000], [], ["Accrued Interest", "Amount"],
        [dt_(2019, 12, 31), 50_000], [dt_(2020, 9, 30), 70_000], ["Total Accrued Interest", 120_000], [],
        ["Investment Balance", 1_120_000, "Balance at conversion"], []] + entity + [
        ["Investment", "Investment Value", "Cash Outlay", "Gain on Conversion", "MTM of Shares", "Investment Date", "Terms"],
        ["Company 71B Series Pre-A Preferred Shares", 1_300_000, 1_120_000, 180_000, 0, dt_(2020, 11, 19),
         "- Note conversion to Series Pre-A"],
        ["Company 71B Series A Preferred Shares", 1_000_000, 1_000_000, 0, 0, dt_(2021, 9, 28), "- Convertible"],
        ["Company 71B Series A Preferred Shares", 700_000, None, 0, 700_000, dt_(2024, 6, 27), "- Jun'24: mark-up"],
        [None, 3_000_000, 2_120_000, 180_000, 700_000]])
    tab("Company 71C", [["Company 71C"], []] + entity + [
        ["Investment", "Amount", "Investment Date", "# of Equity Shares", "Price Per Share", "Terms"],
        ["Company 71C Series B Preferred Shares", 75_000, None, 100, 750],
        ["Impairment", -75_000, dt_(2023, 3, 3)], [None, 0]])
    tab("Company 71E", [["Company 71E"], []] + entity + [
        ["Investment", "Total Investment", "Cash Outlay", "Gain/(Loss) on Conversion", "Investment Date", "Terms"],
        ["Company 71E - Series A Preferred Shares", 1_000_000, 1_000_000, 0, dt_(2015, 1, 10), "- Conversion into common"],
        ["Company 71E - Series B Preferred Shares", 750_000, 700_000, 50_000, dt_(2016, 4, 27), "- $700K promissory notes"],
        ["Company 71E - Series C Preferred Shares", 330_000, 300_000, 30_000, dt_(2019, 8, 13),
         "- Promissory note conversion to Series C"], [None, 2_080_000, 2_000_000, 80_000]])
    tab("Company 71E (Note)", [["Company 71E"], [], ["Method of Accounting", "Note Receivable"], [],
        ["Funding", "Amount", "Investment Date"], ["Initial Investment", 300_000, dt_(2019, 4, 24)],
        ["Total Funding", 300_000], [], ["Accrued Interest", "Amount"], [dt_(2019, 6, 30), 10_000],
        ["Total Accrued Interest", 10_000], [], ["Conversion to Series C", -310_000], [],
        ["Method of Accounting", "Note Receivable"], [],
        ["Funding", "Amount", "Investment Date"], ["Initial Investment", 200_000, dt_(2022, 1, 10)],
        ["Total Funding", 200_000], [], ["Accrued Interest", "Amount"], [dt_(2022, 3, 14), 2_000],
        ["Total Accrued Interest", 2_000], [], ["Repayment of Note, 3/14/2022", -202_000]])
    tab("Company 71D", [["Company 71D"], []] + entity + [
        ["Investment", "Investment Value (USD)", "FX Rate", "Investment Value (KRW)", "Investment Date", "Terms"],
        ["Company 71D Series Pre-A Preferred Shares", 500_000, 1400, 700_000_000, dt_(2024, 11, 20)],
        [None, 500_000, None, 700_000_000]])
    tab("Fund 71F", [["Fund 71F"], [], ["Entity Type", "Limited Partnership"], ["Method of Accounting", "Equity Method"], [],
        [None, "EUR", "FX Rate", "USD"], ["Total Commitment", 2_000_000, 1.1, 2_200_000],
        ["Remaining Commitment", 500_000, 1.1, 550_000], [],
        ["Funding", "EUR", "FX Rate", "USD", "Investment Date"],
        ["Initial Investment", 1_000_000, 1.0, 1_000_000, dt_(2022, 3, 1)],
        ["Capital Call", 500_000, 1.4, 700_000, dt_(2024, 3, 1)], [], ["Total Funding", 1_500_000, None, 1_700_000], [],
        ["Distributions", "EUR", "FX Rate", "USD"],
        [dt_(2023, 6, 30), -100_000, 1.1, -110_000, "Return of capital"],
        [dt_(2025, 6, 30), -160_000, 1.1875, -190_000, "Return of capital"],
        ["Total Distributions", -260_000, None, -300_000]])
    tab("Closed 71X", [["Closed 71X"], []] + entity + [
        ["Investment", "Investment Value", "Cash Outlay", "Investment Date", "Terms"],
        ["Closed 71X Series B Preferred Shares", 2_000_000, 2_000_000, dt_(2018, 6, 26)], [None, 2_000_000, 2_000_000]])
    tab("Closed 71Z - Note", [["Closed 71Z"], [], ["Method of Accounting", "Note Receivable"], [],
        ["Funding", "Amount", "Investment Date"], ["Initial Investment", 500_000, dt_(2016, 9, 28)],
        ["Total Funding", 500_000], [], ["Accrued Interest", "Amount"], [dt_(2017, 12, 31), 50_000],
        ["Total Accrued Interest", 50_000]])
    buf = io.BytesIO()
    wb.save(buf)
    res = client.post("/api/upload", files={"files": ("Equity Investments Summary - Jun26 (cash flow test).xlsx",
                                                      buf.getvalue())}).json()["results"][0]
    calls = lambda e: sorted((c["call_date"], D(c.get("amount_usd") or c["amount"])) for c in db.get_capital_calls(e))
    check(res["status"] == "ok"
          and calls("Company 71A") == [("2017-10-05", 1_000_000), ("2018-08-21", 1_000_000)],
          "an 'Investment Value' table (no Cash Outlay): the two purchases, not their anti-dilution "
          "restatement, the rounding or the impairment")
    check(calls("Company 71B") == [("2019-11-27", 1_000_000), ("2021-09-28", 1_000_000)],
          "a note converting into shares: the note's cash once, not the conversion again — the summary's "
          "funding (2.12M) is the cash plus the note's interest")
    check(calls("Company 71E") == [("2015-01-10", 1_000_000), ("2016-04-27", 700_000), ("2019-04-24", 300_000)],
          "across a shares tab and a note tab: a conversion whose note is on file is left out, one whose "
          "note is not stays at the conversion, a repaid note is no investment")
    check(calls("Company 71D") == [("2024-11-20", 500_000)],
          "a 'Value (USD) | FX Rate | Value (KRW)' table: the USD amount")
    check(calls("Company 71C") == [("", 75_000)] and calls("Closed 71Z") == [("2016-09-28", 500_000)],
          "an undated purchase is kept; a closed note of its own (never converted) comes in with its funding")
    # a row saved before sources were recorded: a re-read names its file, and adds no twin
    with db.get_engine().begin() as conn:
        conn.execute(db.capital_calls.update().where(db.capital_calls.c.entity_name == "Company 71D")
                     .values(source=None, doc_kind=None))
    again = client.post(f"/api/submissions/{res['submission_id']}/reread").json()
    d71 = db.get_capital_calls("Company 71D")
    check(again["status"] == "ok" and len(d71) == 1 and d71[0]["doc_kind"] == "equity"
          and d71[0]["source"].startswith("Equity Investments Summary"),
          "re-reading the workbook names the file of a row saved before sources were recorded, without a twin")
    client.post("/api/portfolio/Company 71C/details", json={"last_investment_date": "Jan 2015"})
    client.post("/api/portfolio/Closed 71X/details", json={"theme": "Life Science"})
    rep = client.get("/api/portfolio?as_of=2026-Q2").json()
    co = {c["entity"]: c for c in rep["companies"]}
    g = co["Company 71C"]["invested"]
    check(len(g) == 1 and g[0]["date"] == "2015-01-01" and "no date in the accounting tab" in g[0]["date_note"],
          "the undated purchase is dated from the investment date on file, and says so")
    det = lambda e: co[e]["details"]
    check(det("Closed 71X")["holding_status"] == "Exit" and det("Closed 71X")["exit_date"] == "2021-09-20"
          and det("Closed 71Z")["holding_status"] == "Impaired" and det("Company 71A")["impairment_date"] == "2025-06-25"
          and co["Closed 71Y"]["investment_name"] == "Closed 71Y (Closed 71X Acq.)"
          and co["Closed 71Y"]["theme"] == "Life Science",
          "closed positions: disposed of → Exit on the disposition date; written off with none → Impaired; "
          "a company's acquisition proceeds on their own line take its theme")
    R = client.get("/api/irr?as_of=2026-Q2").json()
    ents = {e["entity"]: e for e in R["entities"]}
    y = ents.get("Closed 71Y")
    check(y is not None and y["irr_pct"] is None and "Closed 71Y" in R["groups"]["life_sciences"]["members"],
          "proceeds with no investment of their own: no IRR of their own, but counted in their group")
    dist = [(f["date"], round(f["amount"])) for f in ents["Fund 71F"]["flows"] if f["kind"] == "distribution"]
    check(dist == [("2023-06-30", 110_000), ("2025-06-30", 190_000)] and not ents["Fund 71F"]["estimated"],
          "a fund's distributions are dated as the accounting workbook dates them (in USD), not lumped at the quarter end")
    wbx = openpyxl.load_workbook(io.BytesIO(client.get("/api/exports/investment-portfolio.xlsx?as_of=2026-Q2").content))
    dash = wbx["Dashboard"]
    col_a = [dash.cell(r, 1).value for r in range(1, dash.max_row + 1)]
    check("IRR Summary" in col_a and "IRR for Core Investments" in col_a and "IRR for Life Sciences Investments" in col_a,
          "the workbook's Dashboard sheet ends with the IRR Summary (Core, Life Sciences)")
    va = client.get("/api/history/Company 71A?as_of=2026-Q2").json()
    check([(p[0], round(p[1])) for p in va["value"] if "value change" in p[2]]
          == [("2017-10-05", 1_000_000), ("2018-08-21", 2_000_000), ("2025-06-25", 600_000)],
          "its value over time: the two purchases, then the write-down — the restated rows add nothing")
    h = client.get("/api/history/Company 71B?as_of=2026-Q2").json()
    facts = {x["label"]: x["value"] for x in h["facts"]}
    check(facts.get("First investment") == "2019-11-27" and facts.get("Investments made") == 2
          and round(facts.get("Total invested", 0)) == 2_120_000
          and facts.get("Accounting method") == "Measurement Alternative",
          "the company one-pager's facts: first investment, investments made, total invested, current accounting method")


def accounting_workbook_is_the_record() -> None:
    """The accounting workbook is the record of an investment up to its quarter.
    Reading it fills a company an older reader left without cash flows; a row an
    older reader saved that it doesn't list (a note's conversion into shares) and
    another document's row dated by then aren't counted, but are listed; reading it
    again removes its own rows it no longer lists; what is added on top — another
    document's row, one typed in by hand — counts only from its quarter on; a
    purchase with no date anywhere is
    listed, not dropped; a row dated after the report's quarter can be seen and
    deleted; a workbook an older reader read is read again at startup."""
    import datetime as dt
    import io
    from decimal import Decimal

    import openpyxl
    from fastapi.testclient import TestClient

    from app.extractors.master import READER_VERSION
    from app.main import app
    from app.pipeline import refresh_master_workbooks

    client = TestClient(app)
    D = lambda x: Decimal(str(x))
    dt_ = dt.datetime
    name = "Equity Investments Summary - Jun26 (record test).xlsx"
    # what a database an older reader filled holds: the note's conversion saved as
    # a purchase, before sources were recorded
    old = db.add_capital_call("Company 72A", "2020-11-19", D(1_120_000), "Company 72A Series Pre-A Preferred Shares")
    # and a euro company's purchase saved without its currency (shown as if dollars)
    eur = db.add_capital_call("Company 72D", "2023-12-14", D(1_000_000), "Initial Investment")
    with db.get_engine().begin() as conn:
        conn.execute(db.capital_calls.update().where(db.capital_calls.c.id.in_([old, eur]))
                     .values(source=None, doc_kind=None))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Investments Summary"
    head = [None, "Investment", "Investment Balance", "Gross Investment / Funding", "Equity Income/(Loss)",
            "Return of Capital/Other Cash Proceeds", "MTM & Conversion Gain/(Loss)", "Acquisition Impact",
            "Cumulative Impairment", "Total Commitment", "Remaining Commitment", "Ownership %",
            "Initial Investment Date", "Impairment Date", "Disposition Date"]
    for r in ([None, "Equity Investments Summary Workbook"], [None, dt_(2026, 6, 28)], [], head,
              [None, "Equity Investments"],
              ["Company 72A (2019)", "Company 72A", 2_300_000, 2_120_000, 0, 0, 180_000, 0, 0, 0, 0, 0.02,
               dt_(2019, 11, 27)],
              ["Company 72B (2017)", "Company 72B", 1_500_000, 1_500_000, 0, 0, 0, 0, 0, 0, 0, 0.01,
               dt_(2017, 10, 26)],
              ["Company 72C (Pre 2014)", "Company 72C", 74_088.04, 74_088.04, 0, 0, 0, 0, 0, 0, 0, None,
               "Pre 2014"],
              ["Company 72D (2023)", "Company 72D", 1_068_489.36, 1_083_930, -15_440.64, 0, 0, 0, 0, 0, 0, 0.09,
               dt_(2023, 12, 14)],
              [], [None, "CLOSED INVESTMENTS"], head, [None, "Note Receivable"],
              [None, "Investment", "Investment Balance", "Gross Investment / Funding", "Accrued Interest",
               "Return of Capital", "Note Conversion / Repayment", "Cumulative Impairment", "Other",
               "Initial Investment Date", "Impairment Date", "Disposition Date"],
              ["Company 72A (2019 Convertible Note)", "Company 72A", 0, 1_000_000, 120_000, 0, -1_120_000, 0, 0,
               dt_(2019, 11, 27)]):
        ws.append(r)

    def tab(name: str, rows: list) -> None:
        t = wb.create_sheet(name)
        for r in rows:
            t.append(r)
    entity = [["Entity Type", "Corporation"], ["Method of Accounting", "Measurement Alternative"], []]
    tab("Company 72A", [["Company 72A"], [], ["Method of Accounting", "Note Receivable"],
        ["Initial Investment Date", dt_(2019, 11, 27)], [],
        ["Funding", "Amount", "Investment Date"], ["Initial note investment", 1_000_000, dt_(2019, 11, 27)],
        ["Total Funding", 1_000_000], [], ["Accrued Interest", "Amount"],
        [dt_(2019, 12, 31), 50_000], [dt_(2020, 9, 30), 70_000], ["Total Accrued Interest", 120_000], [],
        ["Investment Balance", 1_120_000, "Balance at conversion"], []] + entity + [
        ["Investment", "Investment Value", "Cash Outlay", "Gain on Conversion", "MTM of Shares", "Investment Date", "Terms"],
        ["Company 72A Series Pre-A Preferred Shares", 1_300_000, 1_120_000, 180_000, 0, dt_(2020, 11, 19),
         "- Note conversion to Series Pre-A"],
        ["Company 72A Series A Preferred Shares", 1_000_000, 1_000_000, 0, 0, dt_(2021, 9, 28), "- Convertible"],
        [None, 2_300_000, 2_120_000, 180_000, 0]])
    tab("Company 72B", [["Company 72B"], []] + entity + [
        ["Investment", "Investment Value", "Cash Outlay", "Investment Date", "Terms"],
        ["Company 72B Series A-1 Preferred Shares", 1_500_000, 1_500_000, dt_(2017, 10, 26)],
        [None, 1_500_000, 1_500_000]])
    tab("Company 72C", [["Company 72C"], []] + entity + [
        ["Investment", "Amount", "Investment Date", "# of Equity Shares", "Price Per Share", "Terms"],
        ["Company 72C Series B Preferred Shares", 74_088.04, None, 100, 740.88], [None, 74_088.04]])
    tab("Company 72D", [["Company 72D"], [], ["Entity Type", "Limited Liability Company"],
        ["Method of Accounting", "Equity Method"], ["Initial Investment Date", dt_(2023, 12, 14)], [],
        ["Funding", "EUR", "FX Rate", "USD", "Investment Date"],
        ["Initial Investment", 1_000_000, 1.08393, 1_083_930, dt_(2023, 12, 14)], [],
        ["Total Funding", 1_000_000, None, 1_083_930], [],
        ["Equity Income/(Loss)", "EUR", "FX Rate", "USD"], [dt_(2023, 12, 31), -14_491.59, 1.0655, -15_440.64],
        ["Total Equity Income/(Loss)", -14_491.59, None, -15_440.64], [],
        ["Investment Balance", 985_508.41, None, 1_068_489.36]])
    buf = io.BytesIO()
    wb.save(buf)
    res = client.post("/api/upload", files={"files": (name, buf.getvalue())}).json()["results"][0]
    # added on top: an Investment Portfolio workbook's rounds within the accounting
    # workbook's quarter and after it, a row typed in by hand, a mistyped year
    port = "Investment Portfolio Sep'26 (record test).xlsx"
    db.add_capital_call("Company 72B", "2018-03-31", D(300_000), "Bridge", source=port, doc_kind="portfolio")
    db.add_capital_call("Company 72B", "2026-08-14", D(250_000), "Series A-2", source=port, doc_kind="portfolio")
    db.add_capital_call("Company 72B", "2019-05-05", D(50_000), "Typed in by hand")
    db.add_capital_call("Company 72B", "2026-09-10", D(40_000), "Typed in by hand, after the quarter")
    client.post("/api/portfolio/Company 72A/calls", json={"date": "2050-12-31", "description": "Series Z",
                                                          "amount": "1000000"})
    rep = lambda q: {c["entity"]: c for c in client.get(f"/api/portfolio?as_of={q}").json()["companies"]}
    rows = lambda c, key="invested": [(x["date"], round(x["amount"])) for x in c.get(key) or []]
    q2 = rep("2026-Q2")
    check(res["status"] == "ok"
          and rows(q2["Company 72A"]) == [("2019-11-27", 1_000_000), ("2021-09-28", 1_000_000)]
          and rows(q2["Company 72A"], "invested_superseded") == [("2020-11-19", 1_120_000)],
          "an older reader's row for a note's conversion isn't counted (the accounting workbook counts the "
          "note's own funding) — it's listed, so it can be deleted")
    check(rows(q2["Company 72B"]) == [("2017-10-26", 1_500_000)]
          and rows(q2["Company 72B"], "invested_superseded") == [("2018-03-31", 300_000), ("2019-05-05", 50_000)]
          and rows(q2["Company 72B"], "invested_later") == [("2026-08-14", 250_000), ("2026-09-10", 40_000)]
          and rows(rep("2026-Q3")["Company 72B"]) == [("2017-10-26", 1_500_000), ("2026-08-14", 250_000),
                                                       ("2026-09-10", 40_000)],
          "what is added on top — another document's row, one typed in by hand — counts only after the "
          "accounting workbook's quarter; before it, it's listed as not counted")
    d72 = q2["Company 72D"]["invested"]
    check([(x["date"], round(x["amount"]), x["currency"], (x["original"] or {}).get("currency")) for x in d72]
          == [("2023-12-14", 1_083_930, "USD", "EUR")]
          and [(c["currency"], D(c["amount_usd"])) for c in db.get_capital_calls("Company 72D")] == [("EUR", 1_083_930)],
          "a euro purchase an older reader saved without its currency is corrected by the workbook: "
          "€1,000,000 shown as $1,083,930, not as $1,000,000")
    n72 = q2["Company 72D"].get("native") or {}
    check(n72.get("currency") == "EUR" and round(n72["total_invested"]) == 1_000_000
          and [round(x["amount"]) for x in n72["invested"]] == [1_000_000]
          and round(float(n72["details"]["book_value"])) == 985_508,
          "and its original figures: €1,000,000 invested and its balance, as the accounting tab gives them")
    g = q2["Company 72C"]["invested"]
    h72c = client.get("/api/history/Company 72C?as_of=2026-Q2").json()
    check(len(g) == 1 and g[0]["date"] == "" and round(g[0]["amount"]) == 74_088
          and "no investment date on file" in (g[0]["date_note"] or "")
          and [i["date"] for i in h72c["investments"]] == [""]
          and not any(x["label"] == "First investment" for x in h72c["facts"]),
          "a purchase with no date anywhere is listed (no date), not dropped — in the company history too")
    typo = q2["Company 72A"].get("invested_later") or []
    gone = client.delete(f"/api/portfolio/calls/{typo[0]['id']}") if typo else None
    check([(x["date"], round(x["amount"])) for x in typo] == [("2050-12-31", 1_000_000)]
          and gone is not None and gone.status_code == 200 and not rep("2026-Q2")["Company 72A"]["invested_later"],
          "a row dated after the report's quarter (a mistyped year) is listed, and can be deleted")
    R = client.get("/api/irr?as_of=2026-Q2").json()
    ents = {e["entity"]: e for e in R["entities"]}
    h72a = client.get("/api/history/Company 72A?as_of=2026-Q2").json()
    check([f["date"] for f in ents["Company 72A"]["flows"] if f["kind"] == "call"] == ["2019-11-27", "2021-09-28"]
          and [i["date"] for i in h72a["investments"]] == ["2019-11-27", "2021-09-28"],
          "the IRR and the company history count the same rows: the conversion isn't counted twice")
    stale = db.add_capital_call("Company 72B", "2016-01-01", D(123_456), "An earlier reading's row",
                                source=name, doc_kind="equity")
    again = client.post(f"/api/submissions/{res['submission_id']}/reread").json()
    left = {c["id"] for c in db.get_capital_calls("Company 72B")}
    removed = [a for a in db.get_audit(limit=50) if a["action"] == "capital_call_delete" and a["field"] == "Company 72B"]
    check(again["status"] == "ok" and stale not in left and len(left) == 5
          and any("no longer listed" in (a.get("note") or "") for a in removed),
          "reading the workbook again removes its own row it no longer lists (audited); other documents' rows "
          "and a row typed in by hand stay")
    sid = res["submission_id"]
    with db.get_engine().begin() as conn:
        conn.execute(db.submissions.update().where(db.submissions.c.id == sid)
                     .values(reader_version=None, status="reviewed"))
        conn.execute(db.capital_calls.delete().where(db.capital_calls.c.entity_name == "Company 72C"))
    out = refresh_master_workbooks()
    sub = db.get_submission(sid)
    check([o["submission_id"] for o in out] == [sid] and out[0]["status"] == "ok"
          and sub["reader_version"] == READER_VERSION and sub["status"] == "reviewed"
          and [(c["call_date"], D(c["amount"])) for c in db.get_capital_calls("Company 72C")] == [("", D("74088.04"))]
          and refresh_master_workbooks() == [],
          "a workbook an older reader read is read again at startup (once), and stays reviewed")


def repeated_rows() -> None:
    """One round listed by both workbooks is one row; two equal calls weeks
    apart in one document stay two."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    P = "/api/portfolio/"
    client.post(P + "Company 94M/details", json={"kind": "direct_investment", "total_invested": "4m", "total_value": "6m"})
    for d_, desc, amt in (("2022-09-30", "Series B", "1500000"), ("2025-07-11", "Series B-1", "2500000")):
        db.add_capital_call("Company 94M", d_, Decimal(amt), desc, source="Investment Portfolio Jun'26.xlsx",
                            doc_kind="portfolio", currency="USD")
    for d_, desc, amt in (("2022-09-21", "Series B Preferred Shares", "1499987.5"), ("2025-07-17", "Series B-1 Preferred Shares", "2500000")):
        db.add_capital_call("Company 94M", d_, Decimal(amt), desc, source="Equity Investments Summary.xlsx",
                            doc_kind="equity", currency="USD", amount_usd=Decimal(amt))
    # legacy rows (no source): worded differently, same round
    db.add_capital_call("Company 94M", "2021-06-14", Decimal("2999999"), "Series A", source=None)
    db.add_capital_call("Company 94M", "2021-06-30", Decimal("3000000"), "Series A Preferred Shares", source=None)
    # two genuine calls of one size, three weeks apart, in one document
    db.add_capital_call("Fund 94Q", "2024-01-05", Decimal("500000"), "Capital Call", source="Equity Investments Summary.xlsx", doc_kind="equity")
    db.add_capital_call("Fund 94Q", "2024-01-26", Decimal("500000"), "Capital Call", source="Equity Investments Summary.xlsx", doc_kind="equity")
    client.post(P + "Fund 94Q/details", json={"kind": "fund", "drawn_to_date": "1m", "investment_balance": "1.1m"})
    rep = client.get("/api/portfolio?as_of=2026-Q2").json()
    m = {x["entity"]: x for x in rep["companies"]}["Company 94M"]
    rows = [(c["date"], c["description"]) for c in m["invested"]]
    check(rows == [("2021-06-14", "Series A"), ("2022-09-21", "Series B Preferred Shares"),
                   ("2025-07-17", "Series B-1 Preferred Shares")]
          and all(c.get("also_in") for c in m["invested"]),
          "each round once: the accounting workbook's row kept, the other listing named on it")
    q = {x["entity"]: x for x in rep["funds"]}["Fund 94Q"]
    check(len([c for c in q["invested"] if c["source"] == "entered"]) == 2,
          "two equal calls three weeks apart in one document stay two")


def history_section() -> None:
    """One company's or fund's history: quarterly figures, investments with
    the running total, and the carrying value over time."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    names = {e["entity"] for e in client.get("/api/history").json()["entities"]}
    check({"Company 98A", "Fund 95E", "Company 90A"} <= names, "the history list offers every fund and company")
    h = client.get("/api/history/Company 98A?as_of=2026-Q2").json()
    vals = [(p[0], round(p[1], 2)) for p in h["value"] if "value change" in p[2]]
    check(vals == [("2022-09-21", 1_097_254.9), ("2025-10-07", 2_337_127.57), ("2025-12-01", 2_979_349.57)]
          and [round(i["cumulative"], 2) for i in h["investments"]] == [1_000_000, 2_239_872.67]
          and "value changes" in h["value_basis"],
          "a company's carrying value over time: the accounting workbook's purchases and mark-ups added up")
    f = client.get("/api/history/Fund 95E?as_of=2026-Q2").json()
    check(f["currency"] == "USD" and f["native_currency"] == "EUR"
          and [round(i["cumulative"]) for i in f["investments"]] == [1_000_000, 1_700_000]
          and [round(i["amount_usd"]) for i in f["investments"]] == [1_000_000, 700_000]
          and any("income" in p[2] for p in f["value"]) and f["value"][-1][2] == "value now"
          and any(x["label"] == "Drawn to date" and round(x["value"]) == 1_700_000 for x in f["facts"]),
          "a euro fund's history in USD: calls at their recorded rates, income from the workbook, today's balance")
    a = client.get("/api/history/Company 90A?as_of=2026-Q2").json()
    check([r["period"] for r in a["financials"]][-4:] == ["2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1"]
          and round(a["financials"][-1]["revenue"]) == 75_000,
          "a company's own quarterly figures, oldest to latest")
    check(client.get("/api/history/Nobody 1").status_code == 404, "an unknown name is a 404")


def deletes_and_odd_names() -> None:
    """One file or one fund / company can be deleted on its own (audited), and
    an entity whose name holds a slash is still reachable."""
    from fastapi.testclient import TestClient

    from app import portfolio
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    odd = "Follow-on 1 Company Q EU/ Devices H1 2026 1 25"
    r = client.post(f"/api/portfolio/{odd}/details", json={"kind": "direct_investment", "theme": "x"})
    fp = client.get(f"/api/portfolio/{odd}/footprint").json()
    gone = client.delete(f"/api/portfolio/{odd}")
    check(r.status_code == 200 and fp["details"] and gone.status_code == 200
          and odd not in db.all_details(),
          "an entity named with a slash can be edited, measured and deleted")
    # a cash flow entered by hand on a company's "Invested" list can be deleted on
    # its own (the {entity:path} delete route must not swallow /calls/<id>)
    client.post("/api/portfolio/Company 91D/details", json={"kind": "direct_investment"})
    ids = client.post("/api/portfolio/Company 91D/calls", json={
        "lines": "10/17/2025\tSAFE\t1.000\n12/1/2025\tConvertible Note\t1.155", "scale": 1000000}).json()["ids"]
    gone = client.delete(f"/api/portfolio/calls/{ids[1]}")
    left = [c["id"] for c in db.get_capital_calls("Company 91D")]
    check(gone.status_code == 200 and left == [ids[0]]
          and client.delete(f"/api/portfolio/calls/{ids[1]}").status_code == 404
          and any(a["action"] == "capital_call_delete" and a["field"] == "Company 91D"
                  for a in db.get_audit(limit=10)),
          "one hand-entered investment row is deleted (once, audited); the other stays")
    with db.get_engine().begin() as conn:      # a row saved before sources were recorded
        conn.execute(db.capital_calls.update().where(db.capital_calls.c.id == ids[0])
                     .values(source=None, doc_kind=None))
    row = next(c for c in portfolio.report()["companies"] if c["entity"] == "Company 91D")
    check([c["document"] for c in row["invested"]] == ["source not recorded"],
          "a row with no recorded source is not labelled 'entered by hand'")
    client.delete("/api/portfolio/Company 91D")
    bad = client.post("/api/portfolio/Company 9/details", content=b"not json",
                      headers={"Content-Type": "application/json"})
    check(bad.status_code == 500 and "detail" in bad.json(),
          "an unexpected server error still answers in JSON with a detail")
    subs = client.get("/api/submissions").json()["submissions"]
    victim = next(s for s in subs if s["report_type"] == "company_financials" and s["n_facts"])
    before = client.get("/api/dashboard").json()["n_facts"]
    r = client.delete(f"/api/submissions/{victim['id']}")
    after = client.get("/api/dashboard").json()
    still = [s["id"] for s in client.get("/api/submissions").json()["submissions"]]
    check(r.status_code == 200 and r.json()["deleted"]["deleted_facts"] >= 1
          and victim["id"] not in still and after["n_facts"] < before
          and client.delete(f"/api/submissions/{victim['id']}").status_code == 404
          and any(a["action"] == "delete_submission" and a["submission_id"] == victim["id"]
                  for a in db.get_audit(limit=10)),
          "one uploaded file is deleted with its values, once, and the audit trail says so")
    fund = next(s["entity_name"] for s in subs if s["entity_name"].startswith("Fund ")
                and s["n_facts"] and s.get("extractor") != "manual")
    fp = client.get(f"/api/portfolio/{fund}/footprint").json()
    r = client.delete(f"/api/portfolio/{fund}")
    left = [s for s in client.get("/api/submissions").json()["submissions"] if s["entity_name"] == fund]
    inv = client.get("/api/investments").json()
    check(r.status_code == 200 and fp["files"] and not left
          and fund not in {e["entity_name"] for e in inv["entities"]}
          and fund not in db.all_details() and not db.get_capital_calls(fund),
          f"deleting {fund} takes its files, values, details and investments; the roll-forward is rebuilt")


def pdf_reading(samples: Path) -> None:
    """Comparative columns, numbers a PDF splits apart, and figures at the wrong
    scale: the reporting period's column is read, and mismatches are flagged."""
    from app.extractors.pdf_native import extract_pdf
    from app.models import FinancialFact
    from app.validate import scale_checks
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    report = samples / "Fund 84_Q1 2026 LP Report.pdf"
    if not report.exists():
        import subprocess
        subprocess.run([sys.executable, str(samples / "make_samples.py")], check=True)
    items, text = extract_pdf(report)
    check("88,450,000" in text and "2,100,000" in text and "8 8,450,000" not in text,
          "a number the PDF splits apart (a stray space on a digit) is read whole")
    r = process_path(report, allow_duplicate=True)
    facts = {f["metric_key"]: f for f in db.get_facts(r["submission_id"])}
    check(r["report_type"] == "fund_financials"
          and Decimal(facts["total_assets"]["value"]) == 91_300_000
          and Decimal(facts["total_liabilities"]["value"]) == 120_000
          and Decimal(facts["nav"]["value"]) == 91_180_000
          and "March 31, 2026" in facts["total_assets"]["provenance"],
          "this period's column is read, not last year's comparative on its right")
    check({c["name"]: c["ok"] for c in r["checks"]}.get("balance_sheet")
          and all(f["status"] == "needs_review" for f in facts.values()),
          "the figures reconcile, and a column choice still goes to a person to confirm")

    def fact(rt: str, metric: str, value: int) -> FinancialFact:
        return FinancialFact(entity_name="Fund 84", entity_kind="fund", report_type=rt,
                             period="2026-Q1", metric_key=metric, metric_label=metric,
                             value=Decimal(value), status="auto")

    def names(rt: str, entity: str, fs: list) -> set:
        return {c.name for c in scale_checks(rt, entity, fs)}

    big = [fact("investment_roll_forward", "ending_balance", 95_000_000)]
    check(names("investment_roll_forward", "Fund 84", big) == {"above_fund_total"}
          and big[0].status == "needs_review" and "above_fund_total" in big[0].flags,
          "an investor balance larger than the whole fund on file is flagged for review")
    client.post("/api/portfolio/Fund 84/details", json={"fund_size": "100m", "commitment": "5m"})
    off = [fact("investment_roll_forward", "ending_balance", 40_000_000)]
    fine = [fact("investment_roll_forward", "ending_balance", 4_600_000)]
    check(names("investment_roll_forward", "Fund 84", off) == {"share_out_of_line"}
          and not names("investment_roll_forward", "Fund 84", fine) and fine[0].status == "auto",
          "a balance far from the commitment's share of the fund is flagged; one in line is not")
    client.post("/api/manual-values", json={
        "entity": "Fund 85", "report_type": "investment_roll_forward", "period": "2026-Q1",
        "metric": "ending_balance", "value": "4,000,000"})
    small = [fact("fund_financials", "total_assets", 3_200), fact("fund_financials", "nav", 3_300)]
    check(names("fund_financials", "Fund 85", small) == {"assets_below_net_assets",
                                                         "below_investor_balance"},
          "fund totals below the investor's own balance (or assets below net assets) are flagged")


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
    check(db.all_details() == {} and db.get_capital_calls() == [] and db.get_budgets() == {}
          and body.get("cleared_entities", 0) >= 1,
          "details, dated investments and budgets are cleared too — nothing 'default' remains")
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
