"""Record the Azure OpenAI variant using the already-populated live site
(verified good data, including real llm_suggested facts on Fund 11) --
sidesteps re-uploading (which would hit duplicate-file detection anyway)."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1]
OUT_DIR = Path(sys.argv[2])
OUT_DIR.mkdir(parents=True, exist_ok=True)

_n = [0]
def shot(page, label):
    _n[0] += 1
    fn = OUT_DIR / f"{_n[0]:02d}_{label}.png"
    page.screenshot(path=str(fn))
    print("  shot:", fn.name)

def scroll_to(page, selector, nth=0):
    page.evaluate(f"document.querySelectorAll('{selector}')[{nth}]?.scrollIntoView({{block:'start'}})")
    page.wait_for_timeout(250)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.goto(BASE_URL + "/", wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(500)
    shot(page, "landing_populated")

    scroll_to(page, "#subs-body")
    page.wait_for_timeout(300)
    shot(page, "submissions_table")

    # Open Fund 11 specifically — the file with a real LLM-suggested fact
    rows = page.locator("#subs-body tr")
    target_row = None
    for i in range(rows.count()):
        if "Fund 11" in rows.nth(i).inner_text():
            target_row = rows.nth(i)
            break
    (target_row or rows.nth(0)).click()
    page.wait_for_selector("#modal.show", timeout=20000)
    page.wait_for_timeout(500)
    shot(page, "review_sectioned")

    llm_badge = page.locator("#facts-body .flag", has_text="llm_suggested")
    if llm_badge.count() > 0:
        llm_badge.first.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        shot(page, "review_llm_suggestion")

    # Modification: edit the LLM-suggested value itself (human has final say)
    llm_row = page.locator("#facts-body tr", has=page.locator(".flag", has_text="llm_suggested")).first
    val_input = llm_row.locator("input.val")
    if val_input.count() > 0:
        val_input.scroll_into_view_if_needed()
        val_input.click()
        val_input.fill("")
        val_input.type("145000", delay=35)
        shot(page, "modification_editing")
        val_input.blur()
        page.wait_for_timeout(900)
        shot(page, "modification_saved")

        # confirm that specific (now human-edited) row
        confirm_btn = llm_row.locator("[data-confirm]")
        if confirm_btn.count() > 0:
            confirm_btn.click()
            page.wait_for_timeout(900)
            shot(page, "human_confirm_llm_value")

    page.locator("#m-confirm-all").click()
    page.wait_for_timeout(900)
    shot(page, "human_confirm_all")
    page.locator("#m-close").click()
    page.wait_for_timeout(500)

    # Confirm every remaining needs_review fact across every submission via
    # the real API (not fragile UI clicks on a re-rendering row list) -- also
    # confirms needs_review facts, which "Confirm all & lock in" alone does not.
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

    scroll_to(page, ".report-card", 0)
    page.wait_for_timeout(300)
    shot(page, "performance_strip")

    scroll_to(page, ".report-card", 1)
    page.wait_for_timeout(300)
    shot(page, "direct_investments")

    scroll_to(page, ".report-card", 2)
    page.wait_for_timeout(300)
    shot(page, "operating_metrics")

    scroll_to(page, ".report-card", 3)
    page.wait_for_timeout(300)
    shot(page, "fund_investments")

    page.evaluate("document.getElementById('chart-time').scrollIntoView({block:'center'})")
    page.wait_for_timeout(300)
    shot(page, "value_over_time")

    page.evaluate("document.getElementById('inv-entities').scrollIntoView({block:'center'})")
    page.wait_for_timeout(300)
    shot(page, "investment_portfolio")

    page.evaluate("document.getElementById('ledger-filter').scrollIntoView({block:'center'})")
    page.wait_for_timeout(200)
    page.fill("#ledger-filter", "Fund")
    page.wait_for_timeout(400)
    shot(page, "grand_ledger_filtered")
    page.fill("#ledger-filter", "")
    page.wait_for_timeout(300)
    shot(page, "export_links")

    page.evaluate("window.scrollTo(0,0)")
    page.wait_for_timeout(300)
    shot(page, "final_overview")

    browser.close()
    print("CONSOLE ERRORS:" if errors else "NO CONSOLE ERRORS")
    for e in errors:
        print("  ", e)
