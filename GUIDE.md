# Portfolio Financials Studio — User Guidebook

A practical, screen-by-screen guide to turning a pile of company reports into one
trusted **grand ledger** and a **value-over-time** view.

- New to the project? This guide is for **using** it.
- Setting it up or deploying? See [README.md](README.md).

---

## 1. The idea in one minute

You receive financial reports from many companies/funds, in many formats (PDF,
Excel, CSV). This app:

1. **Extracts** the numbers deterministically (exact parsing, not guessing).
2. **Classifies** each file to an entity (Fund 1, Company A…) and a report type.
3. **Reconciles** the numbers (balance sheet balances, roll-forward ties out, …).
4. Asks a **human to confirm** anything low-confidence or unreconciled.
5. Consolidates all confirmed figures into a **grand ledger** and plots the
   **portfolio value over time**.

**The golden rule:** only data a person has confirmed (or that was auto-accepted
with high confidence *and* passed all checks) counts as "trusted" and appears in
the ledger, charts, and exports. Nothing is silently trusted.

---

## 2. Getting in

| How it's running | What you do |
|---|---|
| Local (`./run.sh`) | open **http://127.0.0.1:8713** |
| Azure App Service | open **https://<your-app>.azurewebsites.net** |

If a **sign-in screen** appears ("Private workspace"):
- **Password mode** — enter the shared workspace password → **Sign in**.
- **Microsoft mode** — click **Sign in with Microsoft** (Entra ID SSO).

No sign-in screen = open mode (local/single-user). Your session lasts 12 hours;
**Sign out** is top-right.

---

## 3. The core workflow (do this each reporting period)

```
  ①  Upload the reports     →  ②  Review each submission  →  ③  Confirm
          │                                                       │
          └──────────────  ④  Read the ledger & trend  ◀──────────┘
                           ⑤  Export CSV
```

### ① Upload
Drag PDF/XLSX/XLSM/XLS files onto the **Upload** box (or click to browse). You can
drop many at once — each becomes a *submission*.

**Name files so the entity is obvious.** The classifier reads the filename:
- `Fund 12 …`, `Fund 11A …` → a **fund**
- `Company A …`, `Company M …` → a **direct investment**
- `Note 3B …` → a **direct investment** (note)
- Document type is inferred too (PCAP / capital account, balance sheet, income
  statement, capital call, JV equity income, …).

After upload you'll see a result line per file: detected **report type**,
**entity**, **period**, and counts — *N auto, N to review, N unmapped*, plus any
reconciliation warnings.

### ② Review
Click any row in the **Submissions** table to open the review panel. For each
extracted value you see:
- the **metric** it mapped to and the **raw label/value** it came from,
- a **status pill** — `auto` (green, trusted) or `needs_review` (amber),
- **flags** (e.g. `multiple_candidates`, `also_on_other_sheets`, a reconciliation
  mismatch),
- the **reconciliation checks** for the whole submission (balance sheet, MOIC,
  roll-forward, etc.) with pass/fail.

You can **edit a value**, **re-map a metric**, or **confirm / reject** each row.
Every change writes an **audit-trail** entry (old → new, when) shown at the bottom.

### ③ Confirm
- **Confirm all & lock in** promotes the submission's `auto` values to `confirmed`
  and marks it reviewed.
- For `needs_review` rows, accept them individually (confirm) or fix the value
  first. **Only confirmed/auto rows enter the ledger and charts.**

### ④ Read the results
Scroll the dashboard top-to-bottom:
- **KPI tiles** — files ingested, trusted data points, entities, values to review.
- **Portfolio value over time** — the headline trend (see §4).
- **Investment portfolio** — one row per entity (balance, equity income, ownership…).
- **Quarterly roll-forward** — Beginning → … → Ending per entity/period; click an
  entity row to filter the table to it (click again to clear).
- **Grand ledger** — every trusted fact, filterable (see §5).

### ⑤ Export
- **Export trusted CSV** (header) — the flat trusted-facts table.
- **Export ledger CSV** (in the Grand ledger card) — the consolidated ledger
  (period, entity, report, metric, value, currency, source).
- **For BI tools (Fabric / Power BI)** — use the versioned export tables instead
  of the ad-hoc CSVs; see §5b below.

### ⑤b Feeding a BI tool (Fabric, Power BI, Excel)

For reporting, don't hand over the ad-hoc CSVs — use the **stable export
contract**, whose column names and types are frozen so your dashboards don't
break when the app is updated:

| What | Where |
|---|---|
| Fact table (the grand ledger) | `/api/fabric/fact_financial_metric.parquet` (or `.csv`) |
| Entity dimension | `/api/fabric/dim_entity.parquet` (or `.csv`) |
| The contract itself (columns, types, grain) | `/api/fabric/schema` |

Give your BI/platform team the **[Fabric / Power BI Integration
Spec](docs/FABRIC_INTEGRATION.md)** — it documents every column, the four
landing options, refresh semantics, starter DAX, and SQL views. They won't need
to read application code.

Two things worth knowing:
- **Prefer Parquet.** It keeps money as exact decimal; CSV can turn it into a float.
- **The fact table is "tall."** One row per *metric*, so always filter by
  `metric_key` before aggregating — never sum the whole value column, since
  currency amounts, percentages, and ratios share it. The spec's DAX examples
  show the correct pattern.

**No Fabric access?** You don't need it. Power BI can connect directly to the
app's database and deliver the same reports — option D in the spec.

---

## 4. Reading the "Portfolio value over time" chart

- The **bold line with the end-label** is the **Total** investment balance across
  all entities, one point per period.
- **Thin lines** are individual entities.
- Hover any point for the exact figure.

**To get a real trend, load the same entity across consecutive periods.** One
fund's capital account for 2025-Q4, 2026-Q1, 2026-Q2… produces a genuine
trajectory. A single period just shows one point — that's expected.

How a period is determined: the app normalizes dates/quarters from the document
text or filename (e.g. `2026-Q1`, `2026-03-31`, `2026-FY`). If it can't tell, the
submission is flagged so you can set it.

---

## 5. The grand ledger

This is the consolidation of **everything** — all companies, all formats, all
periods — into one normalized table:

`period · company/entity · report type · metric · value · currency · source`

- Type in the **filter box** to narrow by company, metric, or period (e.g.
  `Fund 1`, `ending`, `2026`).
- **Export ledger CSV** gives you the whole thing for Excel / Power BI / Fabric.

Because it's built only from trusted facts, the ledger is audit-defensible: every
row traces back (via `source`) to the file and cell/line it came from.

---

## 6. Report types & what gets extracted

| Report type | Key metrics |
|---|---|
| **Fund Financials** | total assets, liabilities, NAV, income, expenses, contributions, distributions |
| **Joint Venture** | ownership %, JV assets/liabilities/equity, revenue, net income, carrying value |
| **JV Equity Income / (Loss)** | ownership %, JV net income, our equity income (auto-recomputed) |
| **Direct Investment** | cost, fair value, unrealized/realized, ownership, MOIC, IRR |
| **Investment Roll-Forward** | Beginning → Funding → Equity inc/(loss) → Return of capital → Dividends → MTM → Other → Change → Ending |

Add metrics/synonyms in `app/report_types.py` — the whole pipeline adapts.

---

## 7. Understanding the trust signals

| Signal | Meaning | Appears in ledger/charts? |
|---|---|---|
| `auto` | high-confidence map **and** passed all checks | ✅ yes |
| `confirmed` | a person accepted/edited it | ✅ yes |
| `needs_review` | low confidence, ambiguous, or failed a check | ❌ not until confirmed |
| `rejected` | a person rejected it | ❌ never |

**Common flags you'll see and what to do:**
- `multiple_candidates(n)` — the same metric appeared in several cells; check you
  picked the right one.
- `also_on_other_sheets(n)` — the label also exists on another sheet (e.g. a
  fund-total tab); verify the value is from the right statement.
- `roll_forward_change_review` / `roll_forward_activity` — the capital-account
  didn't fully tie out (often because several lines collapse into "Other"); review
  the activity lines.
- `percent_scale_uncertain` — a percent might be 45 vs 0.45; confirm the scale.

---

## 8. Choosing the model (optional)

The app is **deterministic by default** and needs no model. A model is used
**only as a fallback** for non-standard layouts, and its suggestions are **always**
`needs_review`.

| `LLM_PROVIDER` | When |
|---|---|
| `none` | default; demos and fully-deterministic runs |
| `azure` | the customer's **Azure OpenAI** (data stays in their Azure tenant) |
| `openai` | public OpenAI cloud |
| `ollama` | a fully-local model |

The header badge shows the active provider and whether it's reachable. See
README → *Model providers* for the environment variables.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| File rejected on upload | filename doesn't identify an entity — rename to include `Fund N` / `Company X`. |
| Values look wrong / huge | multi-sheet workbook pulled from the wrong tab — they'll be flagged `also_on_other_sheets`; confirm the right sheet in review. |
| Scanned PDF, nothing extracted | image-only PDF (no text layer); it's flagged for OCR — provide a native PDF, or add OCR (roadmap). |
| Period shows `UNKNOWN` | date not detected — set/confirm it in review. |
| Ledger/charts empty | nothing confirmed yet — review a submission and **Confirm**. |
| Trend chart shows one point | only one period loaded — add more periods for the same entity. |
| 500 on Azure right after deploy | first boot still installing deps — wait ~45s and refresh; else `az webapp log tail`. |

---

## 10. FAQ

**Does it need a database?** It ships with **SQLite** (a local file, zero setup).
For multi-user / Azure, point `DATABASE_URL` at Azure SQL or PostgreSQL — same code.

**Where are my uploaded files kept?** A copy is stored (local `data/uploads/` or
Azure Blob) so every number traces back to its source.

**Can numbers be wrong without me knowing?** The design prevents silent trust:
auto-accept requires high confidence *and* passing reconciliation; everything else
waits for a human. The audit trail records every value and change.

**How do I get data into Power BI / Fabric?** Use the versioned export tables
(`/api/fabric/fact_financial_metric.parquet` + `dim_entity.parquet`) and hand your
BI team the [integration spec](docs/FABRIC_INTEGRATION.md). If Fabric isn't
available, Power BI can read the app's Azure SQL database directly — same reports,
no Fabric capacity needed.

**What's the difference between Azure and Fabric here?** Azure *hosts the
application* (App Service / Container Apps, the database, document storage).
Fabric is a separate SaaS *analytics* platform that would *consume* the exported
tables for reporting. Fabric is optional — the app is fully functional without it.
