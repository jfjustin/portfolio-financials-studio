"""Generate realistic sample reports to exercise every path of the pipeline.

Creates (in samples/):
  - fund_financials_q4.xlsx        (balance sheet + income statement that reconcile)
  - jv_holdings.xlsx               (joint-venture financials)
  - direct_investment.xlsx         (cost / fair value / MOIC that reconcile)
  - jv_equity_income.pdf           (NATIVE pdf; equity income = ownership × JV NI)

Run:  python samples/make_samples.py
"""
from __future__ import annotations

from pathlib import Path

import openpyxl

OUT = Path(__file__).resolve().parent


def _kv_sheet(path: Path, title: str, header: list[str], rows: list[tuple]):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for line in header:
        ws.append([line])
    ws.append([])
    ws.append(["Line item", "Amount"])
    for label, value in rows:
        ws.append([label, value])
    wb.save(path)
    print("wrote", path.name)


def fund_financials():
    _kv_sheet(
        OUT / "fund_financials_q4.xlsx",
        "fund",
        ["Evergreen Capital Fund III, L.P.",
         "Statement of Assets, Liabilities and Net Assets",
         "As of December 31, 2024 (USD)"],
        [("Total Assets", 50_000_000),
         ("Total Liabilities", 20_000_000),
         ("Net Asset Value", 30_000_000),
         ("Total Investment Income", 4_000_000),
         ("Total Expenses", 1_500_000),
         ("Net Income", 2_500_000),
         ("Capital Called", 45_000_000),
         ("Distributions", 3_000_000),
         ("Management Fees", 600_000)],
    )


def jv_holdings():
    _kv_sheet(
        OUT / "jv_holdings.xlsx",
        "jv",
        ["Harbor Logistics Joint Venture",
         "JV Financial Summary — Year ended 31 December 2024 (USD)",
         "Our equity interest / ownership"],
        [("Ownership %", 40),
         ("Total Assets", 80_000_000),
         ("Total Liabilities", 30_000_000),
         ("Total Equity", 50_000_000),
         ("Revenue", 65_000_000),
         ("Net Income", 12_500_000),
         ("Carrying Value of Investment", 20_000_000)],
    )


def direct_investment():
    _kv_sheet(
        OUT / "direct_investment.xlsx",
        "direct",
        ["Direct Investment — Nimbus Technologies Inc.",
         "Portfolio holding valuation as of Q4 2024 (USD)"],
        [("Cost", 10_000_000),
         ("Fair Value", 18_000_000),
         ("Unrealized Gain", 8_000_000),
         ("Ownership %", 15),
         ("MOIC", 1.8),
         ("IRR", 22)],
    )


def jv_equity_income_pdf():
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "Meridian Renewables Joint Venture", ln=True)
    pdf.set_font("helvetica", "", 11)
    pdf.cell(0, 7, "Equity Method Investment - Equity in Earnings", ln=True)
    pdf.cell(0, 7, "For the year ended December 31, 2024 (USD)", ln=True)
    pdf.ln(4)

    def leader(label, value):
        dots = "." * max(3, 60 - len(label))
        pdf.cell(0, 7, f"{label} {dots} {value}", ln=True)

    pdf.set_font("helvetica", "", 11)
    leader("Our ownership interest", "40%")
    leader("Net income for the year (100%)", "12,500,000")
    leader("Equity in earnings of joint venture", "5,000,000")
    leader("Dividends received", "1,200,000")
    leader("Investment carrying value", "20,000,000")
    out = OUT / "jv_equity_income.pdf"
    pdf.output(str(out))
    print("wrote", out.name)


def _pcap_pdf(filename: str, period_end: str, lines: list[tuple[str, str]]):
    """A partner capital-account statement laid out like a real PCAP: label on
    the left, figure right-aligned, no dot leaders (single space when parsed)."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "B", 13)
    pdf.cell(0, 8, "Statement of Individual Partner's Capital Account", ln=True, align="C")
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, "(Unaudited)", ln=True, align="C")
    pdf.cell(0, 6, "Name of Limited Partner: Sample Holdings LP", ln=True)
    pdf.cell(0, 6, "For the Period Ended", ln=True, align="R")
    pdf.cell(0, 6, period_end, ln=True, align="R")
    for label, value in lines:
        pdf.cell(150, 6, label)
        pdf.cell(40, 6, value, ln=True, align="R")
    pdf.output(str(OUT / filename))
    print("wrote", filename)


def pcap_pdfs():
    """Fund 99, 2026: a Q1 statement and a YEAR-TO-DATE Q2 statement whose
    beginning balance repeats Q1's (as of January 1) — made-up numbers.
    Q2 quarter-only: beginning 452,000 → ending 471,600."""
    _pcap_pdf("Fund 99_2026 Q1 PCAP_Sample Holdings LP.pdf", "March 31, 2026", [
        ("Beginning capital account balance as of January 1, 2026", "$ 500,000"),
        ("Capital distributions", "(20,000)"),
        ("Investment income:", ""),
        ("Interest income", "1,000"),
        ("Operating expenses:", ""),
        ("Management fee", "(2,500)"),
        ("Legal, accounting and professional fees", "(500)"),
        ("", "(3,000)"),
        ("Net change in unrealized appreciation/(depreciation) on investments", "(30,000)"),
        ("Unrealized carried interest", "4,000"),
        ("Ending capital account balance as of March 31, 2026", "$ 452,000"),
        ("Ending capital account balance as % of total partners' capital", "0.50%"),
        ("Total capital commitment", "$ 600,000"),
        ("Capital contributions from Initial Closing Date through March 31, 2026", "550,000"),
    ])
    _pcap_pdf("Fund 99_2026 Q2 PCAP_Sample Holdings LP.pdf", "June 30, 2026", [
        ("Beginning capital account balance as of January 1, 2026", "$ 500,000"),
        ("Capital distributions", "(20,000)"),
        ("Investment income:", ""),
        ("Interest income", "1,800"),
        ("Operating expenses:", ""),
        ("Management fee", "(5,000)"),
        ("Legal, accounting and professional fees", "(900)"),
        ("", "(5,900)"),
        ("Net change in unrealized appreciation/(depreciation) on investments", "(10,000)"),
        ("Provision for foreign withholding tax", "(300)"),
        ("Unrealized carried interest", "6,000"),
        ("Ending capital account balance as of June 30, 2026", "$ 471,600"),
        ("Ending capital account balance as % of total partners' capital", "0.49%"),
        ("Total capital commitment", "$ 600,000"),
        ("Capital contributions from Initial Closing Date through June 30, 2026", "550,000"),
        ("Recallable distributions from Initial Closing Date through", ""),
        ("", "(50,000)"),
    ])


def lp_statement_two_column_pdf():
    """A since-inception LP statement in euros with a 'Total Partnership' column
    next to the investor's own column and quarter-labelled activity lines (no
    opening balance printed) — made-up numbers. Investor quarter: called 40,000,
    unrealised -30,000 + FX 10,000, expenses -5,000; capital account 2,015,000."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 7, "Quarterly report Q1, 2026", ln=True)
    pdf.cell(0, 7, "Limited Partner Fund Statement as at Q1", ln=True)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, "(all figures in EUR)", ln=True)
    pdf.cell(110, 6, "")
    pdf.cell(40, 6, "Total Partnership", align="R")
    pdf.cell(40, 6, "Sample Holdings LP", ln=True, align="R")
    rows = [
        ("Total committed", "100,000,000", "2,000,000"),
        ("Total Capital Contributions called", "102,000,000", "2,040,000"),
        ("Called in previous quarters", "100,000,000", "2,000,000"),
        ("Called in Q1, 2026", "2,000,000", "40,000"),
        ("Distributions to date", "-", "-"),
        ("Capital Loss Q1, 2026", "(1,500,000)", "(30,000)"),
        ("Capital FX Gain Q1, 2026", "500,000", "10,000"),
        ("GP Share and Other Expenses Q1, 2026", "(250,000)", "(5,000)"),
        ("Capital Accounts at Q1", "100,750,000", "2,015,000"),
    ]
    for label, total, ours in rows:
        pdf.cell(110, 6, label)
        pdf.cell(40, 6, total, align="R")
        pdf.cell(40, 6, ours, ln=True, align="R")
    pdf.output(str(OUT / "Fund 77_Q1 2026 Statement_Sample Holdings LP.pdf"))
    print("wrote Fund 77_Q1 2026 Statement_Sample Holdings LP.pdf")


def _lp_statement_pdf(filename: str, title: str, rows: list[tuple]):
    """Two-column LP statement: (label, partnership total, our investor) rows,
    or (heading,) rows that head the lines below them."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 7, title, ln=True)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, "(all figures in EUR)", ln=True)
    pdf.cell(110, 6, "")
    pdf.cell(40, 6, "Total Partnership", align="R")
    pdf.cell(40, 6, "Sample Holdings LP", ln=True, align="R")
    for row in rows:
        if len(row) == 1:
            pdf.set_font("helvetica", "B", 10)
            pdf.cell(0, 6, row[0], ln=True)
            pdf.set_font("helvetica", "", 10)
            continue
        label, total, ours = row
        pdf.cell(110, 6, label)
        pdf.cell(40, 6, total, align="R")
        pdf.cell(40, 6, ours, ln=True, align="R")
    pdf.output(str(OUT / filename))
    print("wrote", filename)


def format_memory_pdfs():
    """Fund 78, two consecutive quarters in the same layout (made-up numbers).
    Q2 rewords lines the way real statements do: new quarter numbers, and
    'Capital Loss' becomes 'Capital Gain'. 'Fund expenses allocated' and
    'Equalisation interest' are wordings nothing is pre-loaded for."""
    _lp_statement_pdf("Fund 78_Q1 2026 Statement_Sample Holdings LP.pdf",
                      "Limited Partner Fund Statement - Q1, 2026", [
        ("Total committed", "50,000,000", "1,000,000"),
        ("Called in Q1, 2026", "1,000,000", "20,000"),
        ("Income",),
        ("Equalisation interest Q1, 2026", "25,000", "500"),
        ("Unrealised Capital Gains",),
        ("Balance as at Q4, 2025", "5,000,000", "100,000"),
        ("Capital Loss Q1, 2026", "(500,000)", "(10,000)"),
        ("GP Share and Other Expenses (Net of income)",),
        ("Balance as at Q4, 2025", "(2,500,000)", "(50,000)"),
        ("GP Share and Other Expenses Q1, 2026", "(100,000)", "(2,000)"),
        ("Fund expenses allocated Q1, 2026", "(50,000)", "(1,000)"),
        ("Capital Accounts at Q1", "50,375,000", "1,007,500"),
    ])
    _lp_statement_pdf("Fund 78_Q2 2026 Statement_Sample Holdings LP.pdf",
                      "Limited Partner Fund Statement - Q2, 2026", [
        ("Total committed", "50,000,000", "1,000,000"),
        ("Called in Q2, 2026", "1,500,000", "30,000"),
        ("Income",),
        ("Equalisation interest Q2, 2026", "35,000", "700"),
        ("Unrealised Capital Gains",),
        ("Balance as at Q1, 2026", "4,500,000", "90,000"),
        ("Capital Gain Q2, 2026", "750,000", "15,000"),
        ("GP Share and Other Expenses (Net of income)",),
        ("Balance as at Q1, 2026", "(2,650,000)", "(53,000)"),
        ("GP Share and Other Expenses Q2, 2026", "(125,000)", "(2,500)"),
        ("Fund expenses allocated Q2, 2026", "(25,000)", "(500)"),
        ("Capital Accounts at Q2", "52,510,000", "1,050,200"),
    ])


def fund_level_report_pdf():
    """Fund 79 quarterly report (made-up, European number format): three
    financial statements side by side on one page, then a capital-accounts
    table of investors with redacted names — no column for any one investor."""
    from fpdf import FPDF
    pdf = FPDF(orientation="L")
    pdf.add_page()
    pdf.set_font("helvetica", "B", 11)
    pdf.cell(0, 8, "FINANCIAL STATEMENTS - Quarterly report 31 March 2026", ln=True)
    blocks = [
        [("ASSETS", None), ("Investments at value", "80.000.000"), ("Cash", "5.000.000"),
         ("Total assets", "85.000.000"), ("LIABILITIES", None),
         ("Accrued liabilities", "50.000"), ("Total liabilities", "50.000"),
         ("Net assets", "84.950.000")],
        [("INVESTMENT INCOME", None), ("Interest", "40.000"),
         ("Total investment income", "40.000"), ("EXPENSES", None),
         ("Management fee", "500.000"), ("Professional fee", "10.000"),
         ("Total expenses", "510.000")],
        [("CHANGES IN NET ASSETS", None), ("Net investment income", "-470.000"),
         ("Changes in net unrealized", "2.420.000"),
         ("Change in net assets from operations", "1.950.000"),
         ("DISTRIBUTIONS TO SHAREHOLDERS", "0"), ("CAPITAL TRANSACTION", "10.000.000"),
         ("NET ASSETS", None), ("Beginning of the period", "73.000.000"),
         ("End of the period", "84.950.000")],
    ]
    top = pdf.get_y() + 4
    for b, rows in enumerate(blocks):
        x = 10 + b * 95
        y = top
        for label, value in rows:
            pdf.set_xy(x, y)
            pdf.set_font("helvetica", "B" if value is None else "", 9)
            pdf.cell(60, 6, label)
            if value is not None:
                pdf.cell(25, 6, value, align="R")
            y += 6
    pdf.add_page()
    pdf.set_font("helvetica", "B", 11)
    pdf.cell(0, 8, "CAPITAL ACCOUNTS", ln=True)
    pdf.set_font("helvetica", "", 9)
    pdf.cell(0, 6, "Shareholder | shares | % | Committed (EUR) | Paid in capital (EUR) | "
                   "Capital account at fair value (EUR)", ln=True)
    for name, shares, pct, committed, paid, account in [
            ("Shareholder A", "100.000", "10,00%", "10.000.000", "8.000.000", "8.495.000"),
            ("Shareholder B", "20.000", "2,00%", "2.000.000", "1.600.000", "1.699.000"),
            ("Shareholder C", "20.000", "2,00%", "2.000.000", "1.600.000", "1.699.000"),
            ("Shareholder D", "860.000", "86,00%", "86.000.000", "68.800.000", "73.057.000"),
            ("TOTAL", "1.000.000", "100,00%", "100.000.000", "80.000.000", "84.950.000")]:
        pdf.cell(0, 6, f"{name} {shares} {pct} {committed} {paid} {account}", ln=True)
    pdf.cell(0, 6, "Total commitments 100.000.000", ln=True)
    pdf.output(str(OUT / "Fund 79_Q1 2026_Quarterly report.pdf"))
    print("wrote Fund 79_Q1 2026_Quarterly report.pdf")
    # and the investor's own statement for the same quarter (for supersede)
    _lp_statement_pdf("Fund 79_Q1 2026 Statement_Sample Holdings LP.pdf",
                      "Limited Partner Fund Statement - Q1, 2026", [
        ("Total committed", "100,000,000", "2,000,000"),
        ("Called in Q1, 2026", "10,000,000", "200,000"),
        ("Capital Accounts at Q1", "84,950,000", "1,699,000"),
    ])


if __name__ == "__main__":
    fund_financials()
    jv_holdings()
    direct_investment()
    jv_equity_income_pdf()
    pcap_pdfs()
    lp_statement_two_column_pdf()
    format_memory_pdfs()
    fund_level_report_pdf()
    print("\nSample files ready in", OUT)
