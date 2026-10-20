"""Drive the full Portfolio Financials Studio workflow in a real browser and
capture a sequence of PNG frames: upload -> processing -> review (sectioned) ->
modification (edit a value) -> human confirm -> portfolio sections -> ledger ->
exports (CSV + Fabric).

Usage: python3 record_workflow.py <variant> <base_url> <out_dir>
  variant: "deterministic" | "azure"  (controls captions / which fund is
           highlighted for the LLM-suggestion moment)

NOTE: the FILES list below points at real sample report files used in the
original recording session, kept OUTSIDE this repo on purpose (they're real,
if partially redacted, fund/company filings -- not something to commit to a
public repo). Point FILES at your own local reports before running this, or
at the synthetic fixtures in samples/ (see tools/demo/README.md).
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

VARIANT = sys.argv[1]
BASE_URL = sys.argv[2]
OUT_DIR = Path(sys.argv[3])
OUT_DIR.mkdir(parents=True, exist_ok=True)

FILES = [
    "/Users/amgclarkson/Downloads/output/JV Equity Income Loss Fund 1 - 03-31-2026 results - Jun26.xlsx",
    "/Users/amgclarkson/Lam_Automated_Fin-Tool/resources/JV Equity Income Loss Fund 3 - 03-31-2026 results - Jun26.xlsx",
    "/Users/amgclarkson/Downloads/output/JV Equity Income Loss Fund 11 - 03-31-2026 results - Jun26.xlsx",
    "/Users/amgclarkson/Lam_Automated_Fin-Tool/resources/SCU Example - Direct Investment Financials Q1 2026 - Copy(Company Y).csv",
    "/Users/amgclarkson/Downloads/SCU Example - Direct Investment Financials Q1 2026 - Copy(Company J).csv",
    "/Users/amgclarkson/Lam_Automated_Fin-Tool/resources/Company W_Consolidated_Financial_Statements_Mar26.xlsx",
    "/Users/amgclarkson/Lam_Automated_Fin-Tool/resources/Company A_04_Financials_Q1_2026.xlsx",
]

_n = [0]
def shot(page, label):
    _n[0] += 1
    fn = OUT_DIR / f"{_n[0]:02d}_{label}.png"
    page.screenshot(path=str(fn))
    print("  shot:", fn.name)
    return fn

def scroll_to(page, selector, nth=0):
    page.evaluate(f"document.querySelectorAll('{selector}')[{nth}]?.scrollIntoView({{block:'start'}})")
    page.wait_for_timeout(250)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    console_errors = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    print(f"=== variant={VARIANT} base={BASE_URL} ===")

    # 1) Empty dashboard
    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(500)
    shot(page, "landing_empty")

    # 2) Upload dropzone, about to receive files
    scroll_to(page, "#drop")
    page.hover("#drop")
    page.wait_for_timeout(200)
    shot(page, "upload_dropzone")

    # 3) Perform the upload (through the real <input type=file> -> real JS handler).
    # Block on the actual /api/upload network response rather than polling the
    # status text -- on the live site this request can run multiple real Azure
    # OpenAI calls sequentially (one per file with a missing required field),
    # which can comfortably exceed a minute.
    with page.expect_response(
        lambda r: "/api/upload" in r.url and r.request.method == "POST",
        timeout=240000,
    ):
        page.set_input_files("#file-input", FILES)
    page.wait_for_timeout(1000)
    shot(page, "upload_results")

    # 4) Submissions table populated
    scroll_to(page, "#subs-body")
    page.wait_for_timeout(300)
    shot(page, "submissions_table")

    # Open the richest review case: in deterministic mode this is Company A
    # (all flagged, multi-column ambiguity). In azure mode we specifically
    # open Fund 11 — the file missing a required field, to show the LLM
    # fallback suggestion in context.
    target_text = "Fund 11" if VARIANT == "azure" else "Company A"
    rows = page.locator("#subs-body tr")
    target_row = None
    for i in range(rows.count()):
        if target_text in rows.nth(i).inner_text():
            target_row = rows.nth(i)
            break
    if target_row is None:
        target_row = rows.nth(0)
    target_row.click()
    page.wait_for_selector("#modal.show", timeout=20000)
    page.wait_for_timeout(500)

    # 5) Review modal — sectioned facts
    shot(page, "review_sectioned")

    if VARIANT == "azure":
        # highlight the llm_suggested row if present (scroll it into view)
        llm_badge = page.locator("#facts-body .flag", has_text="llm_suggested")
        if llm_badge.count() > 0:
            llm_badge.first.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            shot(page, "review_llm_suggestion")

    # 6) MODIFICATION — edit a value in place
    val_inputs = page.locator("#facts-body input.val")
    if val_inputs.count() > 0:
        target_input = val_inputs.first
        target_input.scroll_into_view_if_needed()
        target_input.click()
        target_input.fill("")
        target_input.type("925000", delay=35)
        shot(page, "modification_editing")
        target_input.blur()
        page.wait_for_timeout(900)  # let the save + toast land
        shot(page, "modification_saved")

    # 7) Human judgement — confirm one flagged item individually
    confirm_btns = page.locator("#facts-body [data-confirm]")
    if confirm_btns.count() > 0:
        confirm_btns.first.scroll_into_view_if_needed()
        confirm_btns.first.click()
        page.wait_for_timeout(900)
        shot(page, "human_confirm_one")

    # 8) Confirm all & lock in
    page.locator("#m-confirm-all").click()
    page.wait_for_timeout(900)
    shot(page, "human_confirm_all")

    # close modal
    page.locator("#m-close").click()
    page.wait_for_timeout(600)

    # Confirm every remaining needs_review fact across every submission (not
    # screenshotted -- this is the "rest of the reviewer's afternoon" compressed
    # into one call) so the dashboard sections below are fully populated. Done
    # via the real API, not UI clicks on a stale/re-rendering row list, and
    # critically ALSO confirms needs_review facts -- the "Confirm all & lock
    # in" button only promotes already-auto facts, so a pure UI-click loop
    # would under-populate anything that needed a human look.
    confirmed_count = page.evaluate("""
        async () => {
            const subs = (await (await fetch('/api/submissions')).json()).submissions;
            for (const s of subs) {
                const detail = await (await fetch(`/api/submissions/${s.id}`)).json();
                for (const f of detail.facts) {
                    if (f.status === 'needs_review') {
                        await fetch(`/api/facts/${f.id}`, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({status: 'confirmed'}),
                        });
                    }
                }
                await fetch(`/api/submissions/${s.id}/confirm`, {method: 'POST'});
            }
            return subs.length;
        }
    """)
    print(f"  bulk-confirmed across {confirmed_count} submissions")
    page.wait_for_timeout(400)

    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(600)

    # 9) Performance strip (TVPI/DPI/IRR/NAV)
    scroll_to(page, ".report-card", 0)
    page.wait_for_timeout(300)
    shot(page, "performance_strip")

    # 10) Direct Investments table
    scroll_to(page, ".report-card", 1)
    page.wait_for_timeout(300)
    shot(page, "direct_investments")

    # 11) Direct Investments - Operating Metrics
    scroll_to(page, ".report-card", 2)
    page.wait_for_timeout(300)
    shot(page, "operating_metrics")

    # 12) Fund Investments
    scroll_to(page, ".report-card", 3)
    page.wait_for_timeout(300)
    shot(page, "fund_investments")

    # 13) Portfolio value over time
    page.evaluate("document.getElementById('chart-time').scrollIntoView({block:'center'})")
    page.wait_for_timeout(300)
    shot(page, "value_over_time")

    # 14) Investment portfolio (entities) + roll-forward
    page.evaluate("document.getElementById('inv-entities').scrollIntoView({block:'center'})")
    page.wait_for_timeout(300)
    shot(page, "investment_portfolio")

    # 15) Grand ledger, filtered
    page.evaluate("document.getElementById('ledger-filter').scrollIntoView({block:'center'})")
    page.wait_for_timeout(200)
    page.fill("#ledger-filter", "Fund")
    page.wait_for_timeout(400)
    shot(page, "grand_ledger_filtered")
    page.fill("#ledger-filter", "")
    page.wait_for_timeout(300)

    # 16) Export — CSV and Fabric links visible together
    page.wait_for_timeout(200)
    shot(page, "export_links")

    # 17) Final full view (top)
    page.evaluate("window.scrollTo(0,0)")
    page.wait_for_timeout(300)
    shot(page, "final_overview")

    browser.close()
    print("CONSOLE ERRORS:" if console_errors else "NO CONSOLE ERRORS")
    for e in console_errors:
        print("  ", e)
    print(f"Frames written to {OUT_DIR}")
