# Portfolio Financials Studio

Extract, validate, and review confidential **fund / JV / direct-investment**
financials from **PDF and Excel** reports — replacing manual data entry into
spreadsheets — with a **pluggable model backend** and a strict human-in-the-loop
review step.

Runs **deterministic-only by default**: no model, no credentials, nothing leaves
the machine. When you need help with non-standard layouts, plug in a model
provider — including a **customer's own Microsoft model (Azure OpenAI)** running
inside their Azure tenant, so data never goes to a public vendor.

```
 upload ─▶ extract ─▶ classify+map ─▶ validate ─▶ human review ─▶ GRAND LEDGER ─▶ trend over time
 (any co,  (PDF/Excel) (entity+metric) (reconcile) (confirm/edit)  (consolidated   (+ CSV export)
  any fmt)                                                          all companies)
```

The end goal: take every company's report — **different companies, different
formats** — normalize them into one **grand ledger**, and **visualize value over
time**. This project combines two lineages: the rigor of a deterministic,
audit-trailed extraction pipeline, and the domain model of an
investment-intelligence app (entity classification, capital-account
roll-forwards, workbook field mapping).

## Architecture

**The lake is the system of record. Fabric is the analytics backbone. Azure AI
does the reading.** The app lands every original, keeps every extraction (Bronze)
and every standardized value (Silver). It publishes **approved and
auto-accepted values to Gold** (never values waiting for review), the
dimensional tables Power BI and Fabric build on. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Settings: `LAKE_BACKEND`
(`none` | `local` | `adls` | `onelake`), `LAKE_ACCOUNT_URL`, `LAKE_FILESYSTEM`,
`LAKE_ROOT`, `PUBLISH_AUTO_ACCEPTED`.

## See it in action

Two recordings of the complete workflow — upload → extraction → sectioned
review → edit a value → human confirm → portfolio sections → export — on the
same tool, two model configurations:

| Deterministic (no model) | Azure OpenAI (customer's own deployment) |
|---|---|
| ![Deterministic workflow](docs/demo/deterministic_demo.gif) | ![Azure OpenAI workflow](docs/demo/azure_demo.gif) |
| Zero credentials, nothing leaves the machine. | A required field is genuinely missing — the model suggests three values (flagged, capped at 50% confidence), a human overwrites one, and TVPI/DPI/NAV go from "—" to real numbers once confirmed. |

**📖 New here? Read the [User Guidebook (GUIDE.md)](GUIDE.md)** for a
screen-by-screen walkthrough.
**🔌 Integrating BI?** See the
**[Fabric / Power BI Integration Spec](docs/FABRIC_INTEGRATION.md)**.

## Reporting integration (Microsoft Fabric / Power BI)

The app is the **OLTP system**; Fabric/Power BI is the **reporting layer**. Output
is published as two versioned tables with a frozen schema (`schema_version 1.0`):

| Table | Grain | Key |
|---|---|---|
| `fact_financial_metric` | one row per entity × period × metric | `fact_key` |
| `dim_entity` | one row per entity | `entity_name` |

```bash
curl -O http://127.0.0.1:8713/api/fabric/fact_financial_metric.parquet   # recommended
curl -O http://127.0.0.1:8713/api/fabric/dim_entity.parquet
curl    http://127.0.0.1:8713/api/fabric/schema                          # contract JSON
# CSV variants exist at the same paths with .csv
```

- **Parquet preserves money as `decimal(38,6)`** — CSV can coerce it to float.
- **Only `auto` + `confirmed` rows are exported**, so a dashboard can never show
  an unreviewed figure.
- Four landing options (file + OneLake shortcut, Azure SQL mirroring, Data Factory
  pipeline, or **Power BI direct to Azure SQL with no Fabric at all**), plus DAX
  starters and ready-made SQL views, are documented in the
  [integration spec](docs/FABRIC_INTEGRATION.md).

## Dashboard & the grand ledger

- **Lam Capital Overall Performance** — TVPI, DPI, and a money-weighted IRR
  aggregated across all fund investments, computed from confirmed funding,
  distributions, and NAV (`app/performance.py`).
- **Direct Investments** — one row per company: investment cost, estimated FMV,
  book value, unrealized gain/(loss), MOIC, ownership — mirroring Lam Capital's
  own "Portfolio Value" report format.
- **Direct Investment Financials** and **Investment Portfolio** — Lam Capital's
  two quarterly deliverables, each with a **Download xlsx** in Lam's own layout
  (see [Lam's quarterly deliverables](#lams-quarterly-deliverables)).
- **Portfolio value over time** — a line chart of total investment balance per
  period (plus a thin line per entity); each period shows both its calendar
  label and fiscal-year label (e.g. "2026-Q1" / "FY2026 Q2").
- **Investment portfolio** — the consolidated entities table (`investment_entities`).
- **Quarterly roll-forward** — Beginning → Funding → … → Ending per entity and
  calendar quarter (`investment_quarters`), with entity drill-down.
  - Each quarter's beginning is carried forward from the previous quarter's ending.
  - Year-to-date statements are de-cumulated, and every quarter reconciles.
  - Each adjustment is badged and sent to review (GUIDE §4b-2).
- **Grand ledger** — every trusted fact from every company/period in one
  normalized, filterable table, grouped by **section** (Assets & Balances /
  Income & Profit / Distributions & Capital Flows / Performance Ratios);
  **Export ledger CSV**.
- **KPI tiles**, value-by-type / value-by-entity bars, submissions list, and a
  per-submission **review modal sectioned the same way** so a reviewer checks
  balances, P&L, and capital flows as distinct groups rather than one long list.

Only `auto` + human-`confirmed` values appear in these views — unreviewed or
unreconciled figures stay out until a person accepts them.

## Lam's quarterly deliverables

Both are built from **approved values only** and say where every figure came from.

**Direct Investment Financials Qn YYYY** (`app/direct_investments.py`). Per portfolio
company, Budget | Actual for Q1'26 … Q4'26 and the year, in $000:

| Row | Actual comes from |
|---|---|
| Revenue | P&L "Total for Income" / "Total revenue" / "Revenue" |
| Operating Cash Flow | CF "Net cash provided by (used in) operating activities" |
| CapEx | CF fixed-asset purchases under investing activities (not deposits or securities) |
| Ending Cash | BS "Total for Bank Accounts" / "Cash and cash equivalents" (checked against the CF's closing cash) |
| Operating Expense | P&L "Total for Expenses" / "Total operating expenses" |
| Gross Monthly Burn | Operating Expense ÷ months in the period |
| Gross Runway | Ending Cash ÷ gross monthly burn, whole months |
| Net Monthly Burn | −(Operating Cash Flow + CapEx) ÷ months in the period |
| Net Runway | Ending Cash ÷ net monthly burn, whole months (n/a when cash-flow positive) |

A file named "Company X" / "Note X" (or reading as a company's own P&L, balance sheet
or cash flow) is filed as **Direct Investment Financials**. `app/extractors/company.py`
matches each figure on its whole wording inside the right statement — "Deferred
Revenue" is never revenue — and takes the column covering the quarter (the "Total"
of Jan | Feb | Mar, the "Three Months Ended" 2026 column rather than a prior year to
its right, never a budget column). Scanned statements are read by the vision model
(or Tesseract), and figures stated "in $000" are recorded in whole dollars. Budgets
are typed into the dashboard (`POST /api/budgets`).
`GET /api/exports/direct-investment-financials.xlsx?year=2026&quarter=1` gives one
sheet per company ("Company Y(in $000)", Lam's rows and formats) and a **Sources** sheet
naming the file and line behind every actual.

**Investment Portfolio Mon'YY** (`app/portfolio.py`). One sheet per fund in Lam's
layout — Investment Name (B2), Type (B3), Round (B4), Vintage (B5), Fund Size (B6),
Commitment (B7), Future Draw (B9), Region / Sector (B10), Investment Balance (B11),
Drawn to Date (B13), Distribution to Date (F13), the **Latest Stats** row (date, #
of investments, cost, value, gain %) and the dated **Invested** history — plus the
accounting team's Equity Investments Summary beside it; one sheet per direct
investment; and a **Summary-Cash Flows** sheet whose cells reference those sheets
exactly as Lam's workbook does (`='Fund 1'!$B$13`, …) with the cash-flow totals
(cash inflow = distributions + exit proceeds; actual outflow = drawn + invested;
committed = future draw). Investment Balance is the roll-forward's ending balance;
drawn, distributed and remaining commitment come from the statements' since-inception
lines (else the quarters on file, marked as an estimate); Latest Stats from the fund's
schedule of investments (model step, or its "Total Portfolio Investments" line);
master data (type, vintage, fund size, region / sector, GL account, company code…)
and dated calls are entered once per fund (`POST /api/portfolio/{entity}/details`,
`…/calls`). `GET /api/exports/investment-portfolio.xlsx?as_of=2026-Q2` →
"Investment Portfolio Jun'26.xlsx".

**Gaps are flagged, never guessed.** Both reports list, per company / fund, what
needs a person: figures in no approved file (*missing*), figures read but not yet
approved (*to review*, with the file), approved figures that disagree (*mismatch*),
and estimates. A person types a missing figure in from the dashboard
(`POST /api/manual-values`, or `POST /api/submissions/{id}/facts` while reviewing a
file); it's kept in its own "Entered manually" submission, confirmed, flagged
`manual_entry` and audited, and an empty value withdraws it.

### Metric sections (asset / income / distribution / ratio)

Every metric across all five report types carries a `category` so the review
screen, grand ledger, and Fabric export all group figures the same way:

| Section | Examples |
|---|---|
| **Assets & Balances** | NAV, cost basis, fair value, beginning/ending balance |
| **Income & Profit / (Loss)** | net income, realized/unrealized gain, equity income |
| **Distributions & Capital Flows** | capital called, contributions, distributions, dividends |
| **Performance Ratios** | ownership %, MOIC, IRR |

Edit the taxonomy in `app/report_types.py` (`MetricCategory`, `CATEGORY_LABELS`) —
every metric spec carries a `category=...` kwarg.

### Fiscal year

Every period shown in the app carries both its **calendar** label (e.g.
`2026-Q1`) and a **fiscal-year** label (e.g. `FY2026 Q2`), computed from
`FISCAL_YEAR_END_MONTH` (default `9`: FY2026 = Oct 2025–Sep 2026, so Jan–Mar
2026 is FY2026 Q2 and Apr–Jun 2026 is FY2026 Q3). Set it to `12` for a pure
calendar year. See `app/db.py:fiscal_info()`.

## Why the numbers are trustworthy

No automated extractor — model or otherwise — can *guarantee* correct figures on
its own. Trust is built in four layers:

1. **Deterministic parsing first.** Standard sheets and native PDFs are parsed by
   exact rules (openpyxl / pdfplumber), never guessed. In multi-sheet workbooks
   the **primary statement sheet** is detected so values aren't pulled off a
   look-alike summary tab.
2. **Validation / reconciliation.** Arithmetic checks run automatically:
   Assets = Liabilities + Net Assets; Income − Expenses = Net Income;
   Equity income = Ownership % × JV Net Income; Fair Value − Cost = Unrealized;
   MOIC = FV / Cost; **capital-account roll-forward** (Beginning + Activity =
   Ending); ownership in 0–100; currency consistency. Anything that fails is
   downgraded to *needs review*.
3. **Human-in-the-loop.** Every low-confidence, flagged, or **model-suggested**
   value is surfaced for a person to confirm or correct. Only `auto`
   (high-confidence + passed checks) and `confirmed` values feed the dashboard
   and export.
4. **Full audit trail.** Every value and every change (old → new, when, note) is
   recorded append-only in SQLite. Money is stored as exact decimals, never floats.

The LLM is used **only as a fallback** for non-standard layouts, and its output is
**always** routed to human review — the guarantee never rests on the model.

## Model providers (pluggable)

Select with the `LLM_PROVIDER` environment variable:

| `LLM_PROVIDER` | Model runs | Data boundary | Use when |
|---|---|---|---|
| `none` *(default)* | — | nothing leaves the machine | demos, fully deterministic runs |
| `azure` | **customer's Azure tenant** | inside their Azure subscription | enterprise / confidential data |
| `openai` | public OpenAI cloud | leaves to OpenAI | quick trials |
| `ollama` | local machine | nothing leaves the machine | fully offline with a model |

With a model configured, investor statements in layouts the parser doesn't
fully cover are read by a dedicated capital-account step. It covers
multi-column (fund total vs. investor, quarter vs. YTD/inception) and
since-inception statements, European number formats, and scanned pages (vision
for `azure`/`openai`, else local Tesseract). It reads the investor named in
`LP_NAMES` and the current-quarter column. Every value goes to review; see
GUIDE §6b.

**Fund-level reports** (GUIDE §6d) compute our quarter pro-rata from the fund's
figures once our commitment to that fund is known. Side-by-side statements are
read block by block, and our row in an investor table is used as a check. Any
stored file can be re-read after teaching the app something new.

**Format memory** (GUIDE §6c): the parser and the model both use known statement
wording. It's pre-loaded (`app/data/statement_terms.json`), taught by reviewers
(for one fund or all), and learned from each fund's confirmed values. Each new
statement is read the way that fund was confirmed before, with no retraining.

### Customer Microsoft model (Azure OpenAI)

Connects to an Azure OpenAI **deployment** in the customer's own subscription, so
prompts/data stay within their Azure boundary and are not shared with OpenAI.

```bash
export LLM_PROVIDER=azure
export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
export AZURE_OPENAI_DEPLOYMENT=your-deployment-name        # deployment, not base model
export AZURE_OPENAI_API_VERSION=2024-10-21
export AZURE_OPENAI_API_KEY=...          # OR keyless: AZURE_USE_ENTRA_ID=true
pip install "openai>=1.40" "azure-identity>=1.17"
./run.sh
```

**Choosing the model.** The model is whatever deployment `AZURE_OPENAI_DEPLOYMENT`
names, so switching is one app setting. The capital-account step needs a model
that does chat completions with JSON output and reads images (for scans), and
`LLM_REASONING_EFFORT=high` makes GPT-5/6-class models read more carefully. Each
file's model reading must finish within App Service's ~230 s request limit
(uploads go one file per request; calls time out at 180 s). Benchmarked on four
hard statements (two-column, scanned, fund-level, reserves workbook):

| Deployment | Result | Time per file |
|---|---|---|
| GPT-6 Astra (flagship) | all correct | 19–52 s |
| GPT-6.1 Sol (near-Astra, ~1/5 the price) | all correct | 21–39 s |
| gpt-5-mini | one misread | 25–48 s |

Keyless (recommended for enterprise): set `AZURE_USE_ENTRA_ID=true` and
authenticate with `azure-identity` (`DefaultAzureCredential`: managed identity,
`az login`, or environment credentials) — no API key stored anywhere.

### Deterministic-only (demo)

```bash
./run.sh            # LLM_PROVIDER defaults to none
```

## Authentication (private workspace)

Set `AUTH_MODE`:

| `AUTH_MODE` | Gate | Use when |
|---|---|---|
| `none` *(default)* | open | local / single user |
| `password` | one shared workspace password (`APP_PASSWORD`) | quick private deployments, demos |
| `entra` | **Microsoft Entra ID SSO** (OIDC auth-code flow via MSAL) | enterprise |

```bash
# shared-password workspace
AUTH_MODE=password APP_PASSWORD='choose-one' SESSION_SECRET=$(openssl rand -hex 32) ./run.sh

# Microsoft Entra ID SSO
AUTH_MODE=entra ENTRA_TENANT_ID=... ENTRA_CLIENT_ID=... ENTRA_CLIENT_SECRET=... ./run.sh
# register the redirect URI <your-host>/auth/callback in the Entra app registration
```

Sessions are signed HttpOnly cookies. Static assets, the login page, and the
health check are the only unauthenticated routes.

## Deploy to Azure (Microsoft-environment target)

For a Microsoft shop (Entra ID + Fabric + Azure), the coherent combo is the
**Azure OpenAI model** + the app on **Azure Container Apps**, both inside the
tenant, reached over the corporate VPN:

- Container image: `Dockerfile` (multi-stage; injects `$PORT`).
- One-shot reference deploy: [`deploy/azure-container-apps.sh`](deploy/azure-container-apps.sh)
  — builds the image, creates a Container App with a **system-assigned managed
  identity**, and grants it `Cognitive Services OpenAI User` + `Storage Blob
  Data Contributor` (keyless; no secrets stored).
- Storage: `DATABASE_URL` → **Azure SQL / PostgreSQL**; `STORAGE_BACKEND=azure`
  → **Blob** for source docs. Both support Entra ID / managed identity.
- Network: deploy with **internal ingress** + a **Private Endpoint** on Azure
  OpenAI, so model traffic stays on the Azure backbone and the app is only
  reachable on the VPN.
- **Microsoft Fabric / Power BI**: publish the versioned export tables
  (`/api/fabric/*.parquet`) into OneLake / a Lakehouse, or point Power BI straight
  at the database. See **[docs/FABRIC_INTEGRATION.md](docs/FABRIC_INTEGRATION.md)**.

```bash
docker build -t portfolio-financials-studio .
docker run -p 8000:8000 -e LLM_PROVIDER=none portfolio-financials-studio   # local container
# full Azure deploy:
bash deploy/azure-container-apps.sh        # edit the names/SKUs at the top first
```

### No Docker / restricted subscription? Deploy from source on App Service

If your subscription disables ACR Tasks (common on trial/sponsored accounts) or
you have no local Docker, deploy straight from source — no image build:

```bash
# in Azure Cloud Shell (has az + git); region must be allowed by your subscription
az group create -n rg-pfs -l swedencentral
APP=pfs-$RANDOM; echo "https://$APP.azurewebsites.net"
az webapp up --name "$APP" --resource-group rg-pfs --runtime "PYTHON:3.12" --sku B1
az webapp config set -g rg-pfs -n "$APP" \
  --startup-file "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
az webapp config appsettings set -g rg-pfs -n "$APP" --settings WEBSITES_PORT=8000 LLM_PROVIDER=none
```
Set env vars (`LLM_PROVIDER=azure …`, `AUTH_MODE=password …`) with
`az webapp config appsettings set`.

> **Data persistence.** App Service for Python/Linux unpacks the deployed app
> into an **ephemeral path** on every container start/restart/redeploy — a
> default SQLite DB placed relative to the app code is silently wiped each
> time. The app auto-detects App Service (via `WEBSITE_INSTANCE_ID`) and
> defaults storage to `/home/data`, the platform's **persistent** mount, so
> this works with zero configuration on a current checkout. If you're
> redeploying code that predates this fix, `git pull` first. For true
> multi-instance production, use `DATABASE_URL` (Azure SQL/Postgres) instead
> of SQLite — see [docs/FABRIC_INTEGRATION.md](docs/FABRIC_INTEGRATION.md) for
> the same database feeding your BI layer.

See **[GUIDE.md](GUIDE.md)** for the full step-by-step with Azure OpenAI + auth.

## Report types supported

- **Fund Financials** — assets, liabilities, NAV, income, expenses, contributions, distributions…
- **Joint Venture** — ownership %, JV assets/liabilities/equity, revenue, net income, carrying value
- **JV Equity Income / (Loss)** — equity-method share of JV earnings (auto-recomputed & checked)
- **Direct Investment** — cost, fair value, unrealized/realized, ownership, MOIC, IRR
- **Direct Investment Financials** — a portfolio company's own P&L / balance sheet /
  cash flow: revenue, operating expense, operating cash flow, CapEx, ending cash
- **Investment Roll-Forward** — capital-account statements (Beginning → Funding →
  Equity Income/(Loss) → Return of Capital → … → Ending), reconciled end-to-end

Add or edit metrics/synonyms in `app/report_types.py` — the rest of the pipeline adapts.

## Setup

```bash
cd portfolio-financials-studio
pip install -r requirements.txt          # core deps; LLM SDKs are optional
python samples/make_samples.py           # optional: generate demo files
./run.sh                                  # http://127.0.0.1:8713
```

Open **http://127.0.0.1:8713**, drag PDFs/Excel onto the upload area, then click a
submission to review, confirm, and lock in the values.

## What's in the box

```
app/
  report_types.py     canonical report types, metrics, synonym maps
  models.py           data models + robust number/currency parsing + label→metric mapping
  extractors/
    base.py           period normalization, entity guessing, document classification
    excel.py          deterministic .xlsx/.xlsm/.xls extraction
    pdf_native.py     deterministic native-PDF extraction + scanned-file detection
    registry.py       routing + assembly + primary-sheet selection + auto/needs-review policy
  llm/                pluggable model layer (never auto-accepted)
    base.py           provider interface + shared prompt/JSON handling + NoOp (deterministic)
    azure_openai.py   customer Microsoft model (Azure OpenAI)
    openai_cloud.py   public OpenAI cloud
    ollama.py         local, on-machine model
  validate.py         reconciliation / sanity checks
  performance.py      Direct/Fund Investment summaries + portfolio TVPI/DPI/IRR
  fabric_export.py    versioned Fabric/Power BI export (fact + dim tables)
  pipeline.py         orchestrator (ingest → extract → validate → store)
  db.py               SQLAlchemy Core storage + append-only audit (SQLite / Azure SQL / Postgres)
                      + fiscal_info() (calendar <-> fiscal-year period labels)
  storage.py          source-document storage (local folder or Azure Blob)
  auth.py             AuthMiddleware + password gate / Entra ID SSO (optional)
  config.py           settings; auto-detects Azure App Service's persistent /home mount
  main.py             FastAPI app + JSON API + auth routes
templates/            index.html (dashboard) + login.html (private-workspace gate)
static/               dashboard UI (upload, review, offline SVG charts)
docs/                 FABRIC_INTEGRATION.md — BI integration spec
Dockerfile, deploy/   container image + Azure Container Apps deploy script
samples/              demo report generator
```

## Data & privacy

- All data in `data/` (SQLite DB + copies of uploaded source files). Back this
  folder up; it contains confidential financials.
- Server binds to `127.0.0.1` only. No telemetry, no external fonts/CDNs/scripts.
- In `azure`/`openai` modes, only the extracted text slice needed for a fallback
  is sent to the configured endpoint, and only for non-standard layouts.

## Risks — data handling against Lam's Responsible AI protocol

The project's protocol says Lam and portfolio-company financials stay in the designated
shared project folder (never personal or public cloud accounts) and are used only with
Lam-approved AI tools. Four points currently conflict with it:

1. **A personal (student) Azure subscription.** The live app, its database, the data lake
   and Azure OpenAI run in a personal subscription, so uploaded statements and extracted
   figures are stored and processed outside Lam's tenant. *Mitigation:* deploy to a
   Lam-owned subscription (the app deploys unchanged — see Deploy to Azure), or run
   locally in deterministic mode until that's approved.
2. **A public GitHub repository.** Code, tests and samples carry no Lam data (samples are
   made-up companies), but the demo GIFs and one earlier commit show real figures.
   *Mitigation:* make the repository private, remove the GIFs and rewrite that history.
3. **Local copies on a personal machine.** Real statements, the app's `data/` folder and
   exported workbooks sit outside the shared project folder. *Mitigation:* keep them only
   in the designated folder; use **Clear data** and delete local copies at project end, as
   the protocol's data-retention rule requires.
4. **AI tools not yet approved by Lam.** The model step can send document pages to Azure
   OpenAI (vision for scans), and development used an AI coding assistant on real files.
   *Mitigation:* get Lam's written approval for the model deployment (Azure OpenAI doesn't
   train on inputs), or keep `LLM_PROVIDER=none`. Every model reading is flagged and
   reviewed by a person before it is published.

## Known limitations / roadmap

- **One value per metric per statement.** When several source lines collapse into
  one canonical metric (e.g. multiple expense lines → "Other"), only one is kept
  and the roll-forward reconciliation surfaces the gap for review. Line-level
  aggregation is a planned enhancement.
- **Scanned PDFs need a vision model or Tesseract.** With neither configured,
  scanned pages are listed for the reviewer instead of read.
- **Per-metric time series** — the trend view currently charts total balance;
  charting an arbitrary metric (NAV, equity income) over time is a planned toggle.
- **Vintage, fund size, region / sector and accounting details are entered,
  not extracted.** They're master data on each fund's details (Investment
  Portfolio card); fund size and the schedule of investments' totals and count
  are read from fund reports by the model step and reviewed.
- **Schedules of investments are read as totals.** Latest Stats takes the
  grand total (cost, fair value) and the number of portfolio companies; the
  individual holdings aren't stored line by line.
- **Direct-investment holdings data is entered.** Theme, round, total invested,
  projected investment, status and exit figures for the Investment Portfolio
  come from each company's details — company statements don't carry them.
- **IRR is a reasonable estimate, not GP-grade.** `app/performance.py:xirr()`
  computes a money-weighted return from each period's net cash flows using the
  period's calendar end-date as a proxy timestamp — it will differ from a GP's
  own cash-flow-accurate IRR (exact call/distribution dates, carry waterfall).
