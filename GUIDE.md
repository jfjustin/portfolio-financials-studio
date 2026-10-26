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
Click any row in the **Submissions** table to open the review panel.

**Check the reporting period first.** The *Reporting period* bar at the top shows
the quarter the file was dated to, as both a calendar quarter (`2026-Q2`) and a
fiscal quarter (`FY2026 Q3`), plus the dates it covers. If it's wrong, change
either picker; the other one follows. Then click **Save period**. That:
- re-dates every value in the file;
- records the change in the audit trail;
- for roll-forwards, re-links the quarter to the one before it (beginning balance
  carried forward).

Values are grouped into **sections** so you review like-with-like instead of one
long list:

| Section | What's in it |
|---|---|
| **Assets & Balances** | NAV, cost basis, fair value, beginning/ending balance |
| **Income & Profit / (Loss)** | net income, realized/unrealized gain, equity income |
| **Distributions & Capital Flows** | capital called, contributions, distributions, dividends |
| **Performance Ratios** | ownership %, MOIC, IRR |

For each extracted value you see:
- the **metric** it mapped to and the **raw label/value** it came from,
- a **status pill** — `auto` (green, trusted) or `needs_review` (amber),
- **flags** (e.g. `multiple_candidates`, `also_on_other_sheets`, a reconciliation
  mismatch),
- the **reconciliation checks** for the whole submission (balance sheet, MOIC,
  roll-forward, etc.) with pass/fail.

You can **edit a value**, **re-map a metric**, or **confirm / reject** each row.
Every change writes an **audit-trail** entry (old → new, when) shown at the bottom.
The header also shows the submission's **period and fiscal-year badge** (e.g.
`2026-Q1` and `FY2026 Q2`) — see §4b.

### ③ Confirm
- **Confirm all & lock in** promotes the submission's `auto` values to `confirmed`
  and marks it reviewed.
- For `needs_review` rows, accept them individually (confirm) or fix the value
  first. **Only confirmed/auto rows enter the ledger and charts.**

### ④ Read the results
Scroll the dashboard top-to-bottom:
- **KPI tiles** — files ingested, trusted data points, entities, values to review.
- **Lam Capital Overall Performance** — TVPI, DPI, IRR, and current NAV across
  all funds (see §4c).
- **Direct Investments** — one row per company: cost, FMV, book value,
  unrealized gain/(loss), MOIC, ownership.
- **Fund Investments** — one row per fund: cumulative funding/distributions,
  current balance, quarter-over-quarter change, total % gain/(loss).
- **Portfolio value over time** — the headline trend (see §4).
- **Investment portfolio** — one row per entity (balance, equity income, ownership…).
- **Quarterly roll-forward** — Beginning → … → Ending per entity/period; click an
  entity row to filter the table to it (click again to clear).
- **Grand ledger** — every trusted fact, filterable, grouped by section (see §5).

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

## 4b. Calendar vs fiscal year

Every period shown anywhere in the app carries **both** labels:

- **Calendar**: `2026-Q1` (Jan–Mar 2026)
- **Fiscal**: `FY2026 Q2` (shown smaller, italic, underneath)

The fiscal year defaults to ending in September (FY2026 = Oct 2025–Sep 2026):

| Calendar quarter | Covers | Fiscal label |
|---|---|---|
| 2025-Q4 | Oct 1–Dec 31, 2025 | FY2026 Q1 |
| 2026-Q1 | Jan 1–Mar 31, 2026 | FY2026 Q2 |
| 2026-Q2 | Apr 1–Jun 30, 2026 | FY2026 Q3 |
| 2026-Q3 | Jul 1–Sep 30, 2026 | FY2026 Q4 |

An administrator can change this via `FISCAL_YEAR_END_MONTH` (`12` = pure
calendar year) — see README.

## 4b-2. Roll-forward quarters follow on from each other

Each capital-account / roll-forward file is recorded against the **calendar
quarter it reports on**, inferred from what the document covers rather than the
first date printed on it:

- an explicit "2026 Q2" in the filename wins, then a results date ("03-31-2026
  results", "For the Period Ended June 30, 2026");
- a year-to-date statement ending June 30 is **2026-Q2 (Apr 1–Jun 30)**, never
  the half-year or fiscal-year date;
- a journal voucher's booking month (`MONTH: 2026-06-28`) and posting dates are
  kept as metadata only.

Statements are often **year-to-date** and repeat stale figures. A Q2 statement
may still print "Beginning balance as of January 1" (Q1's beginning) and
Jan–Jun cumulative activity. Before anything is saved, every roll-forward
upload is checked against the entity's earlier quarters:

| Step | What happens | Badge |
|---|---|---|
| Carry forward | Beginning = the previous quarter's ending, whenever the document says otherwise | ↺ |
| De-cumulate | YTD activity minus the year's earlier quarters (Q2 MTM = Jan–Jun MTM − Q1 MTM) | YTD→Q |
| Reconcile | Beginning + activity must equal ending; any residual is put in *Other* | plug |
| Missing quarter | The prior quarter isn't on file yet, so the value is recorded as printed and a warning is shown | ! |

- Hover a badge to see what the document actually printed. The original figure
  is also kept on the fact (`reported_value`) and in the audit trail.
- Every adjusted value goes to **review**.
- Upload order doesn't matter. If Q1 arrives after Q2, Q2 is re-derived
  automatically, but values you've already confirmed are never changed.
- Since-inception lines ("Capital contributions from Initial Closing Date
  through …") never feed a quarter's Funding or Distributions. They're recorded
  as *Contributions / Distributions (since inception)*, and the capital
  commitment fills the Commit column.

## 4c. Lam Capital Overall Performance (TVPI / DPI / IRR)

Above the trend chart, three ratios summarize the whole fund portfolio:

- **TVPI** (Total Value to Paid-In) = (distributions + current NAV) ÷ total
  capital called. Above 1.0x means the portfolio is worth more than was paid in.
- **DPI** (Distributions to Paid-In) = cash actually returned ÷ total capital
  called. The "money back in hand" ratio.
- **IRR** — a money-weighted annualized return computed from each fund's
  period-by-period cash flows (calls out, distributions + current value in).

A ratio shows **"—"** when there's genuinely no capital called yet for any
confirmed fund — that's correct, not a bug. See the README roadmap for this
feature's known approximations (fund "since" ≠ true vintage; IRR uses
period-end dates as a proxy, not exact call/distribution dates).

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

## 6b. Statements in different formats

Nothing is tied to a particular fund. Every fund's statement goes through the
same two layers.

**1. Parser (deterministic, always on).** It reads statements laid out as
label + figure:

- **Numbers:** US `1,234,567.89` and European `1.234.567,89` styles, decided
  per document.
- **Dates:** "Q1, 2026", "31 March 2026", "31-Mar-26" and "Mar 31 2026", in the
  filename or the text.
- **Several figure columns** (e.g. *Total Partnership | our investor*, or
  *3 months | year to date | inception*): it keeps the right-most and marks
  the value `multi_column(n)` for review.

**2. Model step (when a model is configured).** It runs when the parser's
roll-forward is incomplete, has multi-column picks, or doesn't add up; when a
document talks about capital accounts or names our investor; and on every
scanned PDF. The model is told:

- **Who our investor is** (`LP_NAMES`), so it reads that investor's column,
  never the fund total.
- **To use the current quarter's column**, never year-to-date or inception. On
  cumulative statements, it uses only the lines labelled for the quarter
  ("Called in Q1, 2026").
- **To copy the printed lines** that make up each field. The app parses and
  sums them; the model does no arithmetic.

Each value shows which column and period it came from (e.g. *[Lam Research |
Q1, 2026]*). It is flagged `llm_suggested`, gets `parser_agrees` when the
parser read the same number, and always goes to review.

| What you upload | What happens |
|---|---|
| **The investor's own capital-account statement** (one or several columns) | Recorded as that investor's roll-forward for the quarter. If no opening balance is printed and no earlier quarter is on file, it's derived as ending − quarter activity (badge **Σ**). |
| **Scanned statement** | Read from the page image by the vision model (Azure OpenAI / OpenAI), or by local Tesseract OCR if installed. Every value goes to review. |
| **Fund-level report** (LP/investor report, fund financial statements, a table of many investors with redacted names) | Fund-level figures, plus our quarter computed **pro-rata** once our commitment is known. See §6d. |

Without a model, everything still works for parser-friendly layouts. Anything
the parser can't settle is flagged for review rather than guessed.

## 6d. Fund-level reports (no column for our investor)

Some funds only send the fund's quarterly report: statements for the whole fund,
and maybe a capital-accounts table where every investor's name is redacted. Our
quarter can still be derived, because funds allocate to investors in proportion
to commitment:

**our quarter = the fund's quarter × our share** (our commitment ÷ total commitments)

1. **Upload the report.**
   - Statements printed side by side on one page are read block by block, so
     the fund's figures come out right even without a model.
   - The model, if configured, reads them too and cross-checks them
     (*model agrees* / *model reads …*).
   - Without our share the figures stay **fund-level**, and a note asks for it.
2. **Tell the app our stake:** open the file's review panel, then *Format memory
   → Fund parameters*. Enter **our commitment to this fund**, or the name of our
   row in the investor table, and click **Save & re-read**. If a commitment value
   is already confirmed for that fund (e.g. from its own statement in an earlier
   quarter), it's used automatically.
3. **The re-read records our quarter.** Opening balance, capital called,
   distributions, income, expenses and unrealised change are each the fund's
   figure × our share, flagged **pro_rata**. The ending balance is our row's
   capital account when the table has one, and it's checked against the
   pro-rata figure: a gap over 0.5% is flagged **pro_rata_mismatch**. The
   reviewer note states the share and how it was worked out. Everything goes to
   review.
4. **If our own statement for that quarter arrives later,** its figures replace
   the pro-rata ones (unreviewed pro-rata values are retired automatically, with
   an audit entry). If our statement is already on file, the fund report stays
   fund-level and only cross-checks it.

**Save & re-read** works for any file: it reads the stored copy again with
everything the app knows now. Reviewed values stay; unreviewed ones are replaced.

## 6c. Teaching the app new formats (format memory)

Models don't remember anything between uploads, so the app keeps the memory and
hands it to both the parser and the model each time. There are three layers,
and all of them are data, not code:

| Layer | What it holds | Where it comes from |
|---|---|---|
| **Pre-loaded terms** | Statement wording → field, e.g. "Called in Q1, 2026" → Funding, "[Unrealised Capital Gains] Capital Loss Q1, 2026" → MTM, "Revenue reserve" → Other. Lines that look like values but aren't are marked **not a value**. | Ships with the app (`app/data/statement_terms.json`), seeded from every format seen so far. |
| **Taught terms** | Your own wording, for **one fund** or **all funds**. | The **Statement terms** card on the dashboard, or **Teach** on a line in the review panel. |
| **Learned per fund** | The lines behind every value you **confirm**: the investor column and quarter column the model used, the currency, and last quarter's figures. | Automatic: confirming a submission teaches the app that fund's layout. |

How it's used:

- **Matching** ignores the quarter, year, date and gain/loss wording. A term
  taught for "Called in Q1, 2026" also covers "Called in Q3, 2027", and
  "Capital Loss" also covers "Capital Gain". The heading above a line keeps
  look-alike lines in different blocks apart.
- **The parser** checks known wording before its generic synonyms:
  1. terms taught for this fund;
  2. what this fund's confirmed values taught it;
  3. terms taught for all funds;
  4. presets.

  Each value shows where its wording was recognised, e.g. "known wording
  (learned)". When several known lines feed one activity field, they're added
  together.
- **The model** is shown:
  - the known wording;
  - for this fund: the investor column and quarter column it confirmed before,
    the confirmed lines, and last quarter's ending balance (which this quarter's
    opening should match);
  - your **fund parameters**: how our investor appears in this fund's statements
    (e.g. a row name in a fund-wide table) and any notes.

**Your part:**

1. **Confirm** submissions as usual. Each confirmed quarter teaches the app that
   fund's layout.
2. **When the app misses or mis-maps a line**, open the file's *found but not
   mapped* list in the review panel. Pick the field (or *not a value*) and
   **Teach** it, for this fund or for all funds. The value is recorded straight
   away and the quarter is recalculated.
3. **For a fund with an unusual statement**, open *Format memory → Fund
   parameters* in its review panel. Write how our investor appears there and
   anything the model should know (e.g. "statements are year-to-date", "use the
   QTD Change column").

Taught and learned wording is used from the next upload onwards; nothing is
retrained. Teaching and forgetting terms are recorded in the audit trail, and
anything a model reads still goes to review.

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

## 8b. Managing the database

### What it is

Everything the dashboard shows lives in one database:

| Where you're running | Storage |
|---|---|
| Locally (`./run.sh`) | a single file, `data/pipeline.db` (SQLite) |
| Azure App Service | SQLite at `/home/data/pipeline.db` — the platform's **persistent** mount (survives restarts/redeploys; see README's note on this) |
| Production multi-user | Azure SQL / PostgreSQL, via `DATABASE_URL` — same schema, different engine |

Three kinds of rows live in it:

- **`submissions`** — one row per uploaded file (filename, detected type, period, status).
- **`facts`** — one row per extracted value, with its `status`
  (`auto` / `needs_review` / `confirmed` / `rejected`) and every flag.
- **`audit`** — **append-only**. Every value and every change (old → new, who,
  when) is recorded here and never edited or deleted, even when you wipe
  everything else below.

**What the dashboard actually displays** — the grand ledger, the Performance/
Direct/Fund Investments tables, the time-series chart, and the Fabric export —
are all computed **on each request** from `facts` where
`status IN ('auto', 'confirmed')`. Nothing else is shown. The
`investment_entities` / `investment_quarters` tables are a **projection**,
rebuilt from those trusted facts every time you load the page — they are not
an independent source of truth, so you never need to "sync" them separately.

### Clearing it from the app (recommended)

Click **Clear data** (top-right of the header), type `CLEAR`, and confirm. It
works the same locally and on Azure:

- **Deletes** every submission, extracted value, and the investment views.
- **Keeps** the audit trail (append-only). A `clear_data` entry records who
  cleared what and when.
- **Backs up first** (SQLite): a snapshot is saved to
  `data/backups/pipeline_<UTC time>.db` (on Azure, `/home/data/backups/`). The
  newest 5 are kept.
- Files you uploaded before can be uploaded again afterwards; the
  duplicate-file check starts fresh.
- Scripted: `curl -X POST <site>/api/admin/clear -H 'Content-Type: application/json'
  -d '{"confirm":"CLEAR"}'`. When sign-in is on, this needs a signed-in session.

**Undoing a clear:** stop the app, copy the backup over `pipeline.db`, then start
it again. On Azure, use the Kudu file API from the next section to upload it to
`data/pipeline.db`, then restart.

Nothing else clears data. Page refreshes, app restarts and deploys all keep the
database: on Azure it lives on the persistent `/home/data` disk, which deploys
don't touch. The test suite (`python tests/test_e2e.py`) always runs against
its own temporary database and never touches yours.

The manual methods below remain for when the app itself won't start.

### Wiping it — local

```bash
# stop the server first
rm -f data/pipeline.db      # also clear source-file copies if you want:
rm -f data/uploads/* data/processed/*
./run.sh                    # recreates an empty schema automatically
```

This is instant and irreversible. If you want a record first, hit
**Export trusted CSV** (header) or **Export ledger CSV** (Grand ledger card)
before deleting.

### Wiping it — Azure App Service (SQLite backend)

Prefer the **Clear data** button above. If the app is down, use Kudu's file API:

```bash
RG=rg-pfs; APP=pfs-22053     # your resource group / app name

# 1. fetch the SCM (Kudu) publish credentials
CREDS_XML=$(az webapp deployment list-publishing-profiles -g "$RG" -n "$APP" --xml)
USER=$(echo "$CREDS_XML" | grep -o 'userName="[^"]*"' | head -1 | cut -d'"' -f2)
PASS=$(echo "$CREDS_XML" | grep -o 'userPWD="[^"]*"' | head -1 | cut -d'"' -f2)

# 2. delete the DB file — note the If-Match header: Kudu's VFS API requires it
#    for deleting a file that already exists, or you'll get a 409 Conflict
curl -s -u "${USER}:${PASS}" -H "If-Match: *" \
  -X DELETE "https://${APP}.scm.azurewebsites.net/api/vfs/data/pipeline.db"

# 3. restart so the app reopens a fresh (empty) database file
az webapp restart -g "$RG" -n "$APP"
```

Two things worth knowing from doing this in practice:

- **Give it 20–60 seconds after the restart**, and verify with a plain
  `curl .../api/health` (check `"n_submissions"` reads `0`) before trusting the
  UI — the old worker process can keep serving stale data for a little while
  after the restart command returns, especially on the B1 tier.
- **Never run this at the same time as a CI/CD deploy.** A manual restart and
  an in-flight GitHub Actions deploy can collide (Kudu's deployment lock) and
  leave the site briefly 503. Check `gh run list` is clear first.

### Wiping it — Azure SQL / PostgreSQL backend

There's no file to delete. Connect with any SQL client and truncate the
app's tables (`submissions`, `facts`, `audit`, `investment_entities`,
`investment_quarters`, …) — or drop and let `db.init_db()` recreate them on
the app's next startup.

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
| **Dashboard/Investment portfolio empty after a redeploy on Azure** | App Service unpacks the app into an ephemeral path on every restart/redeploy, which used to wipe the local SQLite DB each time. Fixed: the app now auto-detects App Service and stores data in the **persistent** `/home/data` mount. `git pull` the latest code and redeploy — data will then survive restarts. For true multi-instance production, use `DATABASE_URL` (Azure SQL/Postgres) instead of SQLite. |
| **Page broken in Chrome but fine in Safari** (badge stuck on "…", no data, buttons dead) | Chrome was running its cached copy of an older `app.js` against the newer page. Fixed: asset URLs now carry a version (`app.js?v=…`) that changes on every deploy, and pages are always revalidated, so a normal reload picks up new code. If a browser still shows it, do one hard reload (Cmd+Shift+R / Ctrl+Shift+R). |
| TVPI/DPI/QoQ show "—" | correct when that fund has no confirmed capital called yet (can't divide by zero) — not a bug. |

---

## 10. FAQ

**Does it need a database?** It ships with **SQLite** (a local file, zero setup).
For multi-user / Azure, point `DATABASE_URL` at Azure SQL or PostgreSQL — same code.
See §8b for what's in it and how to wipe it.

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

**Why is a metric in "Income" and not "Distributions" (or vice versa)?** The
section comes from `category=` on that metric's definition in
`app/report_types.py`. If your accounting treats something differently, change
it there — every view (review modal, ledger, Fabric export) picks it up
automatically.

**Why does a period show two labels, like `2026-Q1` and `FY2026 Q2`?** The first
is the calendar period as extracted; the second is the fiscal-year label computed
from `FISCAL_YEAR_END_MONTH` (default `9`: FY2026 = Oct 2025–Sep 2026). Both are always shown together — see §4b.
