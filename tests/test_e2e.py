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
