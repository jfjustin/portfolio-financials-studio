# Architecture — financial document analytics

**The lake is the system of record. Fabric is the analytics backbone. Azure AI
does the reading.** Portfolio Financials Studio runs intake, extraction and the
human review step, and publishes into the lake. Dashboards and analysis build
on the lake's Gold layer.

```
01 Input ─▶ 02 Land ─▶ 03 Extract ─▶ 04 Store ─▶ 05 Model ─▶ 06 Present ─▶ 07 Consume
 upload      originals   parsers +    Bronze      rules,      Power BI      portfolio
 (PDF/XLSX/  + registry  Azure OpenAI Silver      reconcile,  (Direct Lake  monitoring,
  CSV)                                Gold        review      over Gold)    SQL/notebooks
```

## Stage by stage

| Stage | Where it lives | What's there |
|---|---|---|
| **01 Input** | Upload in the app (one file per request) | PDF, Excel, CSV: quarterly packages, statements, schedules |
| **02 Land** | `Files/landing/<entity>/<period>/<document>/<file>` and `Tables/land_document_registry` | The original, **as received and never overwritten**. Document id = content hash. Registry columns: company, period, source file, content hash, extraction revision (a re-read is a new revision) |
| **03 Extract** | The app: native parsers for Excel/CSV/text PDFs + **Azure OpenAI** (GPT-6 Astra) for complex layouts and scans | No page cap; billed per token. Document Intelligence can be added as a second reader later if needed |
| **04 Bronze** | `Files/bronze/<document>/extraction_r<n>.json` and `Tables/bronze_raw_records` | Each extraction exactly as produced (incl. the model's reading); every line read with its page/sheet locator |
| **04 Silver** | `Tables/silver_financial_metric` | Every standardized value: metric, period, currency, provenance, flags, **review status** |
| **04 Gold** | `Tables/gold_fact_financial_metric`, `gold_dim_entity`, `gold_dim_period`, `gold_dim_metric`, `gold_fact_investment_quarter`, `gold_publication` | **Published values**: reviewer-approved, plus auto-accepted unless `PUBLISH_AUTO_ACCEPTED=false`. Never values waiting for review. Dimensional. Every (entity, period) carries a **reporting version** that goes up whenever its published content changes |
| **05 Model** | The app: business rules and reconciliation (balance and roll-forward checks, carry-forward, YTD → quarter, pro-rata, currency) | Runs before anything is published |
| **Validate before publishing** | The app's review panel | Exceptions (anything flagged) go to reviewers, with the source line and page, and only reach Gold once approved. Values that pass every check are auto-accepted; set `PUBLISH_AUTO_ACCEPTED=false` to require approval for those too, as in the reference diagram |
| **06 Present** | Power BI semantic model over Gold (Direct Lake on OneLake; import or DirectQuery on ADLS) | Optional; build reports only as needed |
| **07 Consume** | Power BI, Fabric notebooks / SQL endpoint, or the app's own dashboards | Accounting review, trends, investment analysis |

The app's database is the **working copy**: it holds values while they are
reviewed. Silver and Gold are rewritten from it a moment after each change, and
Delta history keeps every previous version. Land and Bronze are append-only.

## The lake today: ADLS Gen2, OneLake-compatible

| Setting | Today | With Fabric |
|---|---|---|
| `LAKE_BACKEND` | `adls` | `onelake` |
| `LAKE_ACCOUNT_URL` | `https://<account>.dfs.core.windows.net` | `https://onelake.dfs.fabric.microsoft.com` |
| `LAKE_FILESYSTEM` | `lakehouse` | the Fabric workspace (name or id) |
| `LAKE_ROOT` | *(empty)* | `<lakehouse>.Lakehouse` |

Authentication is Microsoft Entra ID throughout. The app uses its managed
identity, which needs *Storage Blob Data Contributor* on the account (ADLS) or
contributor access to the workspace (OneLake). No keys are stored anywhere.

**You don't have to move anything to adopt Fabric.** A OneLake *shortcut* to
the ADLS container makes the Gold tables appear in a Fabric lakehouse without
copying them. Alternatively, switch the settings above so the app writes to
OneLake directly.

## Using the lake without the app

- **Browse or drop files:** Azure Storage Explorer (Windows, macOS, Linux) for
  ADLS and OneLake, or OneLake file explorer (Windows) once on Fabric.
- **Power BI Desktop** (free) can read Gold today. In Power Query, use
  `AzureStorage.DataLake("https://<account>.dfs.core.windows.net/lakehouse/Tables/gold_fact_financial_metric")`
  then `DeltaLake.Table`. Repeat for the dimension tables, and relate them on
  `entity_name`, `period_label` and `metric_key`. Sharing reports needs a Power
  BI licence.
- **Fabric** (once licensed): build a Direct Lake semantic model over the
  shortcut Gold tables. Notebooks and the SQL endpoint read the same tables.

## What stays in the app

The review step. Fabric and Power BI have no built-in screen for confirming,
correcting or rejecting extracted values against their source. They also don't
cover period and currency fixes, teaching statement wording, or approving into
Gold. The app is that reviewer workbench.

## Operations

- **Sync:** automatic a moment after every change. *Data lake → Sync now*
  forces one; `POST /api/lake/sync`.
- **Backfill:** `POST /api/lake/backfill` lands files uploaded before the lake
  was switched on.
- **Status:** `GET /api/lake/status`. Lake failures never block the app; they
  show on the Data lake card and are retried on the next sync.
- **Clear data** empties the working copy. On the next sync, Silver and Gold
  are emptied too. Land, Bronze and the Delta history of every layer remain, as
  a system of record should.
