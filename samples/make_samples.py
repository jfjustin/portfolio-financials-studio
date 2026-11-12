"""Generate realistic sample reports to exercise every path of the pipeline.

Creates (in samples/):
  - fund_financials_q4.xlsx        (balance sheet + income statement that reconcile)
  - jv_holdings.xlsx               (joint-venture financials)
  - direct_investment.xlsx         (cost / fair value / MOIC that reconcile)
  - jv_equity_income.pdf           (NATIVE pdf; equity income = ownership × JV NI)
  - Company 90A–90D statements, Fund 82 reports (the two quarterly deliverables; made-up)

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


def lake_flow_pdf():
    """Fund 80, Q1 2026 (made-up): a file no other check uploads, for the lake flow."""
    _lp_statement_pdf("Fund 80_Q1 2026 Statement_Sample Holdings LP.pdf",
                      "Limited Partner Fund Statement - Q1, 2026", [
        ("Total committed", "40,000,000", "1,000,000"),
        ("Called in Q1, 2026", "800,000", "20,000"),
        ("Unrealised Capital Gains",),
        ("Capital Loss Q1, 2026", "(400,000)", "(10,000)"),
        ("Capital Accounts at Q1", "40,400,000", "1,010,000"),
    ])


# --------------------------------------------------------------------------
# The two quarterly deliverables (made-up companies and figures)
# --------------------------------------------------------------------------
def company_quickbooks_xlsx():
    """Company 90A, Q1 2026: a QuickBooks export — monthly columns plus Total on the
    P&L and cash flow, month-end columns on the balance sheet, and liability lines
    that merely contain the word "Revenue"."""
    wb = openpyxl.Workbook()
    months = ["Jan 2026", "Feb 2026", "Mar 2026"]
    pl = wb.active
    pl.title = "P&L"
    for row in (["Profit and Loss"], ["Sample Robotics Inc."], ["January 1-March 31, 2026"], [],
                [None, *months, "Total"], ["Income"], ["4000 Revenue"],
                ["4010 Product Revenue", 20000, 25000, 30000, 75000],
                ["Total for 4000 Revenue", 20000, 25000, 30000, 75000],
                ["Total for Income", 20000, 25000, 30000, 75000], ["Expenses"],
                ["6100 Payroll", 300000, 300000, 300000, 900000],
                ["6500 Rent Expense", 50000, 50000, 50000, 150000],
                ["Total for Expenses", 350000, 350000, 350000, 1050000],
                ["Net Operating Income", -330000, -325000, -320000, -975000], ["Other Expenses"],
                ["8300 Interest Expense", 1000, 1000, 1000, 3000],
                ["Net Income", -331000, -326000, -321000, -978000]):
        pl.append(row)
    bs = wb.create_sheet("BS")
    for row in (["Balance Sheet"], ["Sample Robotics Inc."], ["As of Mar 31, 2026"], [],
                [None, *months], ["Assets"], ["Bank Accounts"],
                ["1000 Operating Account", 3000000, 2700000, 2400000],
                ["Total for Bank Accounts", 3000000, 2700000, 2400000],
                ["Total for Current Assets", 3100000, 2800000, 2500000], ["Liabilities"],
                ["2260 Deferred Revenue", 80000, 85000, 90000],
                ["Sample State Department of Revenue Payable", 500, 600, 700],
                ["Total for Liabilities", 400000, 410000, 420000]):
        bs.append(row)
    cf = wb.create_sheet("CF")
    for row in (["Statement of Cash Flows"], ["Sample Robotics Inc."], ["January 1-March 31, 2026"],
                [], ["Full name", *months, "Total"], ["OPERATING ACTIVITIES"],
                ["Net Income", -331000, -326000, -321000, -978000],
                ["Adjustments to reconcile Net Income to Net Cash provided by operations"],
                ["2240 Accrued Expenses", 6000, 6000, 6000, 18000],
                ["Net cash provided by operating activities", -325000, -320000, -315000, -960000],
                ["INVESTING ACTIVITIES"],
                ["16100 Fixed Assets:Computer Equipment", -5000, -10000, -15000, -30000],
                ["1802 Other Assets:Security Deposit", 0, -20000, 0, -20000],
                ["Net cash provided by investing activities", -5000, -30000, -15000, -50000],
                ["FINANCING ACTIVITIES"],
                ["Net cash provided by financing activities", 0, 0, 0, 0],
                ["NET CASH INCREASE FOR PERIOD", -330000, -350000, -330000, -1010000],
                ["Cash at the beginning of the period", 3410000, 3080000, 2730000, 3410000],
                ["Cash at the end of the period", 3080000, 2730000, 2400000, 2400000]):
        cf.append(row)
    out = OUT / "Company 90A_Financials_Q1_2026.xlsx"
    wb.save(out)
    print("wrote", out.name)


def company_audited_xlsx():
    """Company 90B, Q1 2026: audited-style statements whose cash-flow sheet also
    carries a prior year and the prior year's quarter — to its right."""
    wb = openpyxl.Workbook()
    bs = wb.active
    bs.title = "1.1 Balance Sheet"
    for row in (["Sample Devices, Inc."], ["Consolidated Balance Sheets"], [], [],
                [None, None, None, "March,"], [None, None, None, 2026], ["Assets"],
                ["Current assets"], ["Cash and cash equivalents", None, "$", 5000000],
                ["Total current assets", None, None, 5300000],
                ["Property and equipment, net", None, None, 250000],
                ["Total assets", None, None, 5550000]):
        bs.append(row)
    so = wb.create_sheet("1.2 Statement of Operations")
    for row in (["Sample Devices, Inc."], ["Consolidated Statements of Operations"],
                ["Three Months ended March 31, 2026"], [],
                [None, None, "Three Months ended March 31,"], [None, None, 2026],
                [], ["Revenue", "$", 400000], ["Cost of revenue", None, 150000],
                ["Gross margin", None, 250000], ["Operating expenses:"],
                ["Research and development", None, 900000],
                ["General and administrative", None, 300000],
                ["Total operating expenses", None, 1200000], ["Net loss", "$", -950000]):
        so.append(row)
    cf = wb.create_sheet("1.3 Cash flow statement")
    for row in (["Sample Devices, Inc."], ["Consolidated Statements of Cash Flows"],
                ["Three Months ended March 31, 2026"], [],
                [None, None, None, "Year Ended December 31,", None, "Three Months Ended March 31,",
                 None, "Three Months Ended March 31,"],
                [None, None, None, "2025", None, "2026", None, "2025"],
                ["Operating Activities"],
                ["Net loss", None, "$", -4000000, "$", -950000, "$", -800000],
                ["Net cash used in operating activities", None, None, -3500000, None, -1000000,
                 None, -700000],
                ["Investing Activities"],
                ["Purchases of property and equipment", None, None, -100000, None, -60000,
                 None, -20000],
                ["Net cash used in investing activities", None, None, -100000, None, -60000,
                 None, -20000],
                ["Cash, cash equivalents and restricted cash, end of period", None, None, 6000000,
                 None, 5000000, None, 7000000]):
        cf.append(row)
    out = OUT / "Company 90B_Consolidated_Financial_Statements_Mar26.xlsx"
    wb.save(out)
    print("wrote", out.name)


def company_cash_flow_pdf():
    """Company 90C, Q1 2026: a cash-flow-only PDF printed QuickBooks-style — a
    single space before each figure, numbers inside some labels."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "B", 13)
    pdf.cell(0, 8, "Statement of Cash Flows", ln=True)
    pdf.set_font("helvetica", "", 10)
    for line in ("January-March, 2026", "Full name Total", "OPERATING ACTIVITIES",
                 "Net Income -500,000.00",
                 "Adjustments to reconcile Net Income to Net Cash provided by operations:",
                 "Clearing Account (4410) -250.00",
                 "Total for Adjustments to reconcile Net Income to Net Cash provided by operations: -$250.00",
                 "Net cash provided by operating activities -$500,250.00", "INVESTING ACTIVITIES",
                 "Computer Hardware -412.30",
                 "Fixed Asset - Leasehold Improvements 12-20 -21,500.00",
                 "Fixed Asset - Lab Equipment 2026 -10,000.00",
                 "Net cash provided by investing activities -$31,912.30", "FINANCING ACTIVITIES",
                 "Net cash provided by financing activities $0.00",
                 "NET CASH INCREASE FOR PERIOD -$532,162.30",
                 "Cash at beginning of period $3,000,000.00",
                 "CASH AT END OF PERIOD $2,467,837.70"):
        pdf.cell(0, 6, line, ln=True)
    out = OUT / "Company 90C_Q1_2026_Quarterly_CF.pdf"
    pdf.output(str(out))
    print("wrote", out.name)


def company_scanned_pdf():
    """Company 90G, Q1 2026: a "scanned" statement — drawn shapes, no text layer —
    that nothing can read without a vision model or OCR."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    for i in range(12):
        pdf.rect(20, 30 + i * 12, 120 + (i % 3) * 20, 6, style="F")
    out = OUT / "Company 90G_Q1_2026_Financial_Statements.pdf"
    pdf.output(str(out))
    print("wrote", out.name)


def direct_investment_financials_csv():
    """Company 90D: a finished "Direct Investment Financials" sheet, in $000, with
    a budget in the Q1 Budget column."""
    rows = ["Company 90D(in $000),Q1'26,,Q2'26,,Q3'26,,Q4'26,,2026,,",
            ",Budget,Actual,Budget,Actual,Budget,Actual,Budget,Actual,Budget,Actual,",
            "Revenue,$150 ,$120 ,,,,,,,,,",
            'Operating Cash Flow,,"($900)",,,,,,,,,',
            "CapEx,,($15),,,,,,,,,",
            'Ending Cash,,"$4,200 ",,,,,,,,,',
            'Operating Expense,,"$1,050 ",,,,,,,,,',
            "Gross Monthly Burn,,$350 ,,,,,,,,,",
            "Gross Runway,,12 Months,,,,,,,,,",
            "Net Monthly Burn,,$305 ,,,,,,,,,",
            "Net Runway,,14 Months,,,,,,,,,"]
    out = OUT / "Direct Investment Financials Q1 2026 - Company 90D.csv"
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("wrote", out.name)


def portfolio_fund_pdfs():
    """Fund 82, Q1 2026: our capital statement with since-inception lines, and the
    fund's financial statements with its schedule of portfolio investments."""
    _pcap_pdf("Fund 82_Q1 2026 PCAP_Sample Holdings LP.pdf", "March 31, 2026", [
        ("Beginning capital account balance as of January 1, 2026", "$ 3,600,000"),
        ("Capital contributions", "500,000"),
        ("Capital distributions", "(200,000)"),
        ("Net change in unrealized appreciation", "300,000"),
        ("Ending capital account balance as of March 31, 2026", "$ 4,200,000"),
        ("Total capital commitment", "5,000,000"),
        ("Capital contributions since inception", "4,000,000"),
        ("Distributions since inception", "(1,000,000)"),
        ("Remaining commitment", "1,000,000"),
    ])
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "B", 13)
    pdf.cell(0, 8, "Example Growth Fund I, L.P.", ln=True, align="C")
    pdf.set_font("helvetica", "", 10)
    for line in ("Statement of Assets and Liabilities", "March 31, 2026",
                 "Investments, at fair value 55,000,000", "Cash 3,000,000",
                 "Total assets 58,000,000", "Total liabilities 500,000",
                 "Net assets 57,500,000", "", "Schedule of Portfolio Investments",
                 "March 31, 2026", "Percent of Partners' Capital Cost Fair Value Unrealized",
                 "Total Example One, Inc. 10.4 5,000,000 6,000,000 1,000,000",
                 "Total Example Two, Inc. 85.2 35,000,000 49,000,000 14,000,000",
                 "Total Non-Marketable Investments 95.6 40,000,000 55,000,000 15,000,000",
                 "Total Portfolio Investments 95.6 $ 40,000,000 $ 55,000,000 $ 15,000,000"):
        pdf.cell(0, 6, line, ln=True)
    out = OUT / "Fund 82_Q1 2026 Financial Statements.pdf"
    pdf.output(str(out))
    print("wrote", out.name)


def comparative_columns_pdf():
    """Fund 84 LP report (made-up): this quarter beside last year-end under stacked
    column headers, note references in brackets, and — as some redacted or
    re-saved PDFs carry — a stray space lying on the first digit of the
    comparative figures, which plain text extraction reads as "8 8,450,000"."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 7, "Example Opportunity Fund IV, L.P.", ln=True)
    pdf.cell(0, 7, "Combined Statement of Financial Position", ln=True)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, "As at", ln=True)
    pdf.cell(0, 6, "[Expressed in United States dollars]", ln=True)
    y = pdf.get_y() + 6

    def right(x_end: float, text: str, split: bool = False) -> None:
        x = x_end - pdf.get_string_width(text)
        if not split:
            pdf.text(x, y, text)
            return
        pdf.text(x, y, text[0])
        pdf.text(x, y, " ")                          # the stray space, on top of the digit
        pdf.text(x + pdf.get_string_width(text[0]), y, text[1:])

    for a, b in (("March 31,", "December 31,"), ("2026", "2025"), ("$", "$")):
        right(150, a)
        right(190, b)
        y += 6
    for label, now, before in (
            ("ASSETS", None, None),
            ("Cash", "1,250,000", "2,100,000"),
            ("Investments [notes 5 and 9]", "90,050,000", "86,350,000"),
            ("Total assets", "91,300,000", "88,450,000"),
            ("LIABILITIES", None, None),
            ("Accounts payable and accrued liabilities [note 8]", "120,000", "95,000"),
            ("Total liabilities", "120,000", "95,000"),
            ("Partners' Capital [note 6]", "91,180,000", "88,355,000")):
        pdf.text(15, y, label)
        if now:
            right(150, now)
            right(190, before, split=label != "Total liabilities")
        y += 6
    out = OUT / "Fund 84_Q1 2026 LP Report.pdf"
    pdf.output(str(out))
    print("wrote", out.name)


def company_prior_quarter_xlsx():
    """Company 90A, Q4 2025 (made-up): the quarter before the Q1 2026 export, same
    QuickBooks layout — so a run of quarters (Q4'25 to Q1'26) has two to add up."""
    wb = openpyxl.Workbook()
    months = ["Oct 2025", "Nov 2025", "Dec 2025"]
    pl = wb.active
    pl.title = "P&L"
    for row in (["Profit and Loss"], ["Sample Robotics Inc."], ["October 1-December 31, 2025"], [],
                [None, *months, "Total"], ["Income"], ["4000 Revenue"],
                ["4010 Product Revenue", 15000, 18000, 22000, 55000],
                ["Total for 4000 Revenue", 15000, 18000, 22000, 55000],
                ["Total for Income", 15000, 18000, 22000, 55000], ["Expenses"],
                ["6100 Payroll", 280000, 280000, 280000, 840000],
                ["6500 Rent Expense", 50000, 50000, 50000, 150000],
                ["Total for Expenses", 330000, 330000, 330000, 990000],
                ["Net Income", -315000, -312000, -308000, -935000]):
        pl.append(row)
    bs = wb.create_sheet("BS")
    for row in (["Balance Sheet"], ["Sample Robotics Inc."], ["As of Dec 31, 2025"], [],
                [None, *months], ["Assets"], ["Bank Accounts"],
                ["1000 Operating Account", 4300000, 3860000, 3410000],
                ["Total for Bank Accounts", 4300000, 3860000, 3410000]):
        bs.append(row)
    cf = wb.create_sheet("CF")
    for row in (["Statement of Cash Flows"], ["Sample Robotics Inc."], ["October 1-December 31, 2025"],
                [], ["Full name", *months, "Total"], ["OPERATING ACTIVITIES"],
                ["Net Income", -315000, -312000, -308000, -935000],
                ["Net cash provided by operating activities", -305000, -300000, -295000, -900000],
                ["INVESTING ACTIVITIES"],
                ["16100 Fixed Assets:Computer Equipment", -10000, -10000, -5000, -25000],
                ["Net cash provided by investing activities", -10000, -10000, -5000, -25000],
                ["NET CASH INCREASE FOR PERIOD", -315000, -310000, -300000, -925000],
                ["Cash at the end of the period", 4020000, 3710000, 3410000, 3410000]):
        cf.append(row)
    out = OUT / "Company 90A_Financials_Q4_2025.xlsx"
    wb.save(out)
    print("wrote", out.name)


def _quickbooks_quarter(filename: str, months: list[str], span: str, as_of: str,
                        revenue: list[int], opex: list[int], ocf: list[int], capex: list[int],
                        cash: list[int]) -> None:
    """A QuickBooks-style export for one quarter: a column per month plus Total."""
    wb = openpyxl.Workbook()
    pl = wb.active
    pl.title = "P&L"
    for row in (["Profit and Loss"], ["Sample Robotics Inc."], [span], [], [None, *months, "Total"],
                ["Income"], ["4010 Product Revenue", *revenue, sum(revenue)],
                ["Total for Income", *revenue, sum(revenue)], ["Expenses"],
                ["6100 Payroll", *[o - 50000 for o in opex], sum(opex) - 150000],
                ["6500 Rent Expense", 50000, 50000, 50000, 150000],
                ["Total for Expenses", *opex, sum(opex)],
                ["Net Income", *[r - o for r, o in zip(revenue, opex)], sum(revenue) - sum(opex)]):
        pl.append(row)
    bs = wb.create_sheet("BS")
    for row in (["Balance Sheet"], ["Sample Robotics Inc."], [as_of], [], [None, *months], ["Assets"],
                ["Bank Accounts"], ["1000 Operating Account", *cash], ["Total for Bank Accounts", *cash]):
        bs.append(row)
    cf = wb.create_sheet("CF")
    for row in (["Statement of Cash Flows"], ["Sample Robotics Inc."], [span], [], ["Full name", *months, "Total"],
                ["OPERATING ACTIVITIES"],
                ["Net cash provided by operating activities", *ocf, sum(ocf)], ["INVESTING ACTIVITIES"],
                ["16100 Fixed Assets:Computer Equipment", *capex, sum(capex)],
                ["Net cash provided by investing activities", *capex, sum(capex)],
                ["Cash at the end of the period", *cash, cash[-1]]):
        cf.append(row)
    wb.save(OUT / filename)
    print("wrote", filename)


def company_year_of_quarters():
    """Company 90A, Q2 and Q3 2025 (made-up): with Q4'25 and Q1'26, twelve months of
    QuickBooks exports — "last 12 months" of revenue in one view."""
    _quickbooks_quarter("Company 90A_Financials_Q2_2025.xlsx", ["Apr 2025", "May 2025", "Jun 2025"],
                        "April 1-June 30, 2025", "As of Jun 30, 2025", [8000, 9000, 11000],
                        [300000, 300000, 310000], [-290000, -295000, -300000], [-5000, 0, -5000],
                        [6000000, 5705000, 5400000])
    _quickbooks_quarter("Company 90A_Financials_Q3_2025.xlsx", ["Jul 2025", "Aug 2025", "Sep 2025"],
                        "July 1-September 30, 2025", "As of Sep 30, 2025", [11000, 12000, 14000],
                        [310000, 320000, 320000], [-300000, -305000, -310000], [-5000, -5000, -10000],
                        [5095000, 4785000, 4465000])


def master_workbooks():
    """The team's two master workbooks for the sample portfolio (made-up figures):
    an "Investment Portfolio" workbook — one sheet per fund / company in the team's
    layout, the accounting block beside, dated investments, a planning tab — and
    an accounting "Equity Investments Summary" — the summary table plus backup tabs.
    Uploading either fills every fund's and company's details at once."""
    import datetime as dt
    wb = openpyxl.Workbook()
    aop = wb.active
    aop.title = "Investments-AOP"
    aop.append(["Follow-on 1 Company 90A US/ Robotics H1 2026 1 25"])
    aop.append(["Planned", "H1 2026", 1.0])

    def fund(title, name, region, vintage, size, commit, future, balance, drawn, dist, stats, calls, acct):
        ws = wb.create_sheet(title)
        for r, label, value in ((2, "Investment Name", name), (3, "Investment Type", "LP"),
                                (4, "Current / Most Recent Round", "-"), (5, "Vintage", vintage),
                                (6, "Fund Size", size), (7, "Commitment", commit), (9, "Future Draw", future),
                                (10, "Region / Sector Focus", region), (11, "Investment Balance", balance),
                                (13, "Drawn to Date", drawn)):
            ws.cell(r, 1, label)
            ws.cell(r, 2, value)
        ws.cell(13, 5, "Distribution to Date")
        ws.cell(13, 6, -dist)
        for c, h in zip("BCDEF", ("Date", "# of investments", "Cost Basis", "Value", "Gain / (Loss) %")):
            ws[f"{c}14"] = h
        ws["A15"] = "Latest Stats"
        for c, v in zip("BCDE", stats):
            ws[f"{c}15"] = v
        for c, h in zip("BCDEF", ("Date", "Current Round", "Amount Invested", "Total Raise", "Pre Money Valuation")):
            ws[f"{c}16"] = h
        for i, (d, desc, amt) in enumerate(calls, start=17):
            ws.cell(i, 1, "Invested")
            ws.cell(i, 2, d)
            ws.cell(i, 3, desc)
            ws.cell(i, 4, amt)
        ws["H2"] = "Equity Investments Summary"
        for r, (label, value) in enumerate(acct, start=3):
            ws.cell(r, 8, label)
            ws.cell(r, 9, value)

    fund("Fund 82", "Example Growth Fund I", "US/Software", 2019, 150.0, 5.0, 1.0, 4.2, 4.0, 1.0,
         (dt.datetime(2026, 3, 31), 14, 40.0, 55.0),
         [(dt.datetime(2019, 4, 14), "Drawdown Notice #1", 0.5), (dt.datetime(2020, 6, 1), "Drawdown Notice #3", 1.0),
          (dt.datetime(2022, 3, 1), "Drawdown Notice #6", 1.5), (dt.datetime(2026, 2, 10), "Drawdown Notice #9", 1.0)],
         (("Entity Type", "Limited Partnership"), ("Method of Accounting", "Equity Method"),
          ("GL Account", "190100"), ("Company Code", "C100"), ("% Ownership", 0.033),
          ("Initial Investment Date", dt.datetime(2019, 4, 14)), ("Terms", "10-year term, two 1-year extensions")))
    fund("Fund 99", "Example Ventures II", "EU/Deep tech", 2021, 220.0, 2.0, 0.8, 1.35, 1.2, 0.1,
         (dt.datetime(2026, 3, 31), 9, 60.0, 72.0),
         [(dt.datetime(2021, 9, 1), "Capital Call #1", 0.4), (dt.datetime(2023, 5, 1), "Capital Call #2", 0.4),
          (dt.datetime(2025, 11, 1), "Capital Call #3", 0.4)],
         (("Entity Type", "Limited Partnership"), ("Method of Accounting", "Equity Method"),
          ("GL Account", "190100"), ("Company Code", "C100"), ("% Ownership", 0.009),
          ("Initial Investment Date", dt.datetime(2021, 9, 1))))

    def company(title, name, rnd, security, theme, invested, projected, value, status, rounds, exit_=None):
        ws = wb.create_sheet(title)
        rows = [(2, "Investment Name", name), (3, "Investment Type", "Direct"), (4, "Current / Most Recent Round", rnd),
                (5, "Most Recent Investment Date", rounds[-1][0]), (6, "Total Investment to Date", invested),
                (7, "Projected Future Investments", projected), (9, "Investments", security),
                (10, "Theme", theme), (11, "Total Value", value), (12, "Operating Status", status)]
        if exit_:
            rows.append((13, "Date of Exit", exit_[0]))
            ws.cell(13, 5, "Total Received Proceeds")
            ws.cell(13, 6, exit_[1])
            ws.cell(14, 5, "Total Proceeds on Escrow")
            ws.cell(14, 6, exit_[2])
        for r, label, v in rows:
            ws.cell(r, 1, label)
            ws.cell(r, 2, v)
        for c, h in zip("BCDEF", ("Date", "Current Round", "Amount Invested", "Total Raise", "Pre Money Valuation")):
            ws[f"{c}16"] = h
        for i, (d, desc, amt, raise_, pre) in enumerate(rounds, start=17):
            for c, v in zip("ABCDEF", ("Invested", d, desc, amt, raise_, pre)):
                ws[f"{c}{i}"] = v

    company("Company 90A", "Sample Robotics Inc.", "Series A", "Series A Preferred", "Robotics", 3.0, 1.0, 4.6, "Active",
            [(dt.datetime(2024, 6, 1), "Seed", 1.0, 4.0, 12.0), (dt.datetime(2026, 3, 1), "Series A", 2.0, 20.0, 60.0)])
    company("Company 90B", "Sample Devices Corp.", "Series B", "Series B Preferred", "Devices", 2.4, None, 7.5, "Inactive",
            [(dt.datetime(2021, 5, 1), "Series A", 1.4, 10.0, 30.0), (dt.datetime(2024, 9, 15), "Series B", 1.0, 25.0, 90.0)],
            exit_=(dt.datetime(2026, 6, 15), 6.9, 0.6))
    company("Company 90C", "Sample Fabrication Ltd.", "Seed", "Seed Preferred", "Advanced manufacturing", 1.5, 0.5, 0.4,
            "Active", [(dt.datetime(2025, 11, 2), "Seed", 1.5, 5.0, 15.0)])
    company("Company 90D", "Sample Bio Tools Inc.", "Series A", "Series A Preferred", "Life sciences tools", 2.0, 1.0, 3.8,
            "Active", [(dt.datetime(2026, 1, 20), "Series A", 2.0, 18.0, 40.0)])
    sv = wb.create_sheet("Summary-Portfolio Value")
    sv.append(["Worksheet Reference", None, "Company", "Theme", "Operating Status", "Current/Impaired/Exit",
               "Impairment ($M)", "Book Value ($M)", "Group"])
    for row in (("Company 90A", "Active", "Current", None, 3.0, "Core"),
                ("Company 90B", "Inactive", "Exit", None, 0.0, "Core"),
                ("Company 90C", "Active", "Impaired", 1.1, 0.4, "Core"),
                ("Company 90D", "Active", "Current", None, 2.0, "Life Sciences")):
        sv.append([row[0], None, row[0], None, *row[1:]])
    sv.append([None, None, "Total Direct"])
    out = OUT / "Investment Portfolio Jun'26 (sample).xlsx"
    wb.save(out)
    print("wrote", out.name)

    # the accounting team's Equity Investments Summary (made-up figures, whole units)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Investments Summary"
    ws.append([None, "Equity Investments Summary Workbook", "Summarized presentation of investment balances"])
    ws.append([None, dt.datetime(2026, 6, 28)])
    ws.append([])
    head = [None, "Investment", "Investment Balance", "Gross Investment / Funding", "Equity Income/(Loss)",
            "Return of Capital/Other Cash Proceeds", "MTM & Conversion Gain/(Loss)", "Acquisition Impact",
            "Cumulative Impairment", "Total Commitment", "Remaining Commitment", "Ownership %",
            "Initial Investment Date"]
    ws.append(head)
    ws.append([None, "Equity Investments"])
    ws.append(["Company 90A (2024)", "Company 90A", 3_000_000, 3_000_000, 0, 0, 0, 0, 0, 0, 0, 0.041, dt.datetime(2024, 6, 1)])
    ws.append(["Company 90C (2025)", "Company 90C", 400_000, 1_500_000, 0, 0, 0, 0, -1_100_000, 0, 0, 0.052, dt.datetime(2025, 11, 2)])
    ws.append(["Company 90D (2026)", "Company 90D", 2_000_000, 2_000_000, 0, 0, 0, 0, 0, 0, 0, 0.033, dt.datetime(2026, 1, 20)])
    ws.append([None, None, 5_400_000, 6_500_000])
    ws.append([None, "Funds"])
    ws.append(["Fund 82 (2019)", "Fund 82", 4_200_000, 4_000_000, 1_200_000, -1_000_000, 0, 0, 0, 5_000_000, 1_000_000, 0.033, dt.datetime(2019, 4, 14)])
    ws.append(["Fund 99 (2021)", "Fund 99", 1_350_000, 1_200_000, 250_000, -100_000, 0, 0, 0, 2_000_000, 800_000, 0.009, dt.datetime(2021, 9, 1)])
    ws.append([None, None, 5_550_000, 5_200_000])
    ws.append([None, "Total Equity Investments", 10_950_000])
    ws.append([None, "CLOSED INVESTMENTS"])
    ws.append(head)
    ws.append([None, "Equity Investments"])
    ws.append(["Company 90B (2021)", "Company 90B", 0, 2_400_000, 0, -6_900_000, 4_500_000, 0, 0, 0, 0, None, None])
    for title, rows in (
        ("Company 90A", (["Company 90A"], [], ["Entity Type", "Corporation"],
                         ["Method of Accounting", "Measurement Alternative"], ["GL Account", 190645],
                         ["Company Code", 1220], ["% Ownership - 12/31/2025", 0.041, "Cap table"],
                         ["Investment", "Investment Value", "Cash Outlay", "MTM of Shares", "Investment Date"],
                         ["Company 90A Seed Preferred Shares", 1_000_000, 1_000_000, None, dt.datetime(2024, 6, 1)],
                         ["Company 90A Series A Preferred Shares", 2_000_000, 2_000_000, None, dt.datetime(2026, 3, 1)],
                         [None, 3_000_000, 3_000_000])),
        ("Fund 82", (["Fund 82"], [], ["Entity Type", "Limited Partnership"], ["Method of Accounting", "Equity Method"],
                     ["GL Account", 190100], ["Company Code", 1220], ["% Ownership", 0.033],
                     ["Initial Investment Date", dt.datetime(2019, 4, 14)], ["Terms", "- 10-year term"],
                     ["Total Commitment", 5_000_000], ["Remaining Commitment", 1_000_000],
                     ["Funding", "Amount", "Investment Date"],
                     ["Initial Investment", 500_000, dt.datetime(2019, 4, 14)],
                     ["Capital Call", 1_000_000, dt.datetime(2020, 6, 1)],
                     ["Capital Call", 1_500_000, dt.datetime(2022, 3, 1)],
                     ["Capital Call", 1_000_000, dt.datetime(2026, 2, 10)], ["Total Funding", 4_000_000],
                     ["Investment Balance", 4_200_000]))):
        tab = wb.create_sheet(title)
        for r in rows:
            tab.append(r)
    sap = wb.create_sheet("SAP")
    sap.append(["Update quarterly"])
    sap.append(["Account", "P12 FY25", "P3 FY26"])
    out = OUT / "Equity Investments Summary Workbook - Jun26 (sample).xlsx"
    wb.save(out)
    print("wrote", out.name)


if __name__ == "__main__":
    fund_financials()
    jv_holdings()
    direct_investment()
    jv_equity_income_pdf()
    pcap_pdfs()
    lp_statement_two_column_pdf()
    format_memory_pdfs()
    fund_level_report_pdf()
    lake_flow_pdf()
    company_quickbooks_xlsx()
    company_audited_xlsx()
    company_cash_flow_pdf()
    company_scanned_pdf()
    direct_investment_financials_csv()
    portfolio_fund_pdfs()
    comparative_columns_pdf()
    company_prior_quarter_xlsx()
    company_year_of_quarters()
    master_workbooks()
    print("\nSample files ready in", OUT)
