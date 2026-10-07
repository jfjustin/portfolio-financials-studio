# Microsoft Fabric / Power BI Integration Specification

**Audience:** the customer's data platform, Fabric, and BI leads.
**Purpose:** everything needed to land Portfolio Financials Studio output in
Microsoft Fabric (or Power BI directly) without reading the application code.

| | |
|---|---|
| Schema version | **1.0** (frozen — see [Versioning](#versioning)) |
| Contract endpoint | `GET /api/fabric/schema` (machine-readable JSON) |
| Tables published | `fact_financial_metric`, `dim_entity` |
| Formats | CSV and **Parquet** (typed; recommended) |
| Trust rule | **Only `auto` + `confirmed` rows are exported.** Unreviewed figures are never published. |

---

## 1. What this system produces

Portfolio Financials Studio ingests fund / JV / direct-investment reports in
mixed formats (PDF, Excel), extracts them deterministically, reconciles them,
and requires human confirmation of anything uncertain. The output is a
**consolidated ledger** of trusted financial facts plus an entity dimension.

It is an **OLTP application** — Fabric is the **analytics/reporting layer** on
top. The application never writes directly into Fabric; it publishes tables that
Fabric ingests.

```
  Reports (any company, any format)
        │
        ▼
  Portfolio Financials Studio  ──writes──▶  Azure SQL / PostgreSQL   (app state + audit trail)
        │                                          │
        │ /api/fabric/*.parquet                     │ (option B/C/D below)
        ▼                                          ▼
  Fabric Lakehouse / OneLake  ──────────▶  Power BI semantic model ──▶ Reports
```

---

## 2. Tables

### 2.1 `fact_financial_metric` — the grand ledger

**Grain:** one row per **entity × period × metric**.
**Primary key:** `fact_key` (`entity_name|period_label|metric_key`).
**Sort key:** `period_order, entity_name, metric_key`.

| Column | Type | Description |
|---|---|---|
| `fact_key` | string | Stable surrogate key: `entity|period|metric` |
| `entity_name` | string | Company / fund / JV name — **FK → `dim_entity.entity_name`** |
| `entity_kind` | string | `fund` \| `joint_venture` \| `direct_investment` \| `unknown` |
| `report_type` | string | Source report type key |
| `report_label` | string | Human-readable report type |
| `period_label` | string | Normalized period: `YYYY-Qn`, `YYYY-MM-DD`, or `YYYY-FY` |
| `period_order` | int64 | Sortable period key, `YYYYQ` form (e.g. `202601`) — **use for time axis ordering** |
| `period_year` | int64 | Calendar year |
| `period_quarter` | int64 | Calendar quarter 1–4 (`0` if not determinable) |
| `metric_key` | string | Canonical metric key (machine name) |
| `metric_label` | string | Canonical metric label (display name) |
| `metric_unit` | string | `currency` \| `percent` \| `ratio` \| `count` \| `text` |
| `value_numeric` | decimal(38,6) | The value. **Exact decimal, never float.** Null when not found |
| `currency_code` | string | ISO code; meaningful when `metric_unit = currency` |
| `source_file` | string | Originating document file name |
| `source_locator` | string | `Sheet!cell` or `p3 table1 r5c2` — audit traceability |
| `trust_status` | string | `auto` \| `confirmed` |
| `confidence` | double | Mapping confidence 0–1 at extraction time |
| `exported_at` | string | UTC ISO-8601 timestamp of the export run |
| `schema_version` | string | Export schema version |

> **Important for measures:** this table is a *tall / EAV-shaped* fact. Always filter
> by `metric_key` (and usually `metric_unit = 'currency'`) before aggregating —
> never `SUM(value_numeric)` across all metrics, since percentages, ratios, and
> currency amounts share the column. See §5 for ready-made DAX.

### 2.2 `dim_entity` — entity dimension

**Grain:** one row per entity. **Primary key:** `entity_name`.

| Column | Type | Description |
|---|---|---|
| `entity_name` | string | Primary key |
| `entity_kind` | string | `fund` \| `joint_venture` \| `direct_investment` \| `unknown` |
| `latest_period` | string | Most recent period with trusted data |
| `investment_balance` | decimal(38,6) | Latest trusted balance (ending balance / NAV / fair value) |
| `total_commitment` | decimal(38,6) | Commitment or cost basis where available |
| `ownership_pct` | double | Ownership percentage where available |
| `exported_at` | string | UTC ISO-8601 timestamp |
| `schema_version` | string | Export schema version |

### 2.3 Recommended date dimension

The fact table carries `period_order`, `period_year`, `period_quarter` so you can
join your **existing enterprise date dimension**. If you need a local one, build
it from `DISTINCT period_label, period_order, period_year, period_quarter`.
Periods are **not** necessarily month-ends — some sources report quarter labels
(`2026-Q1`) and others exact dates (`2026-03-31`).

---

## 3. Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/fabric/schema` | JSON contract: tables, columns, types, grain, row counts |
| `GET /api/fabric/fact_financial_metric.parquet` | Fact table, Parquet + snappy **(recommended)** |
| `GET /api/fabric/fact_financial_metric.csv` | Fact table, CSV |
| `GET /api/fabric/dim_entity.parquet` | Entity dimension, Parquet |
| `GET /api/fabric/dim_entity.csv` | Entity dimension, CSV |
| `GET /api/ledger.csv` | Human-friendly ledger (ad-hoc Excel use, not the contract) |

**Prefer Parquet.** It preserves `decimal(38,6)` for money and `int64` for period
keys. CSV re-types on read and can silently coerce money to float.

Authentication: when the app runs with `AUTH_MODE=entra` or `password`, these
endpoints sit behind the same session gate as the UI. For unattended pulls, use
one of the server-side options (B/C) below rather than scraping the HTTP endpoint.

---

## 4. Landing options — pick one

Listed in order of increasing integration depth. **Option A works today with no
additional Azure resources.**

### Option A — Scheduled file export → OneLake shortcut *(simplest)*
1. A scheduled job (Fabric pipeline, Azure Function, or cron) calls
   `/api/fabric/*.parquet` and writes the files to ADLS Gen2 / Blob.
2. Create a **OneLake shortcut** in a Fabric Lakehouse to that container.
3. Load to Lakehouse tables (full refresh) and build the semantic model.

*Pros:* no DB access needed, trivially auditable. *Cons:* batch latency.

### Option B — Mirroring from Azure SQL *(lowest ongoing effort)*
Point **Fabric mirroring** at the application's Azure SQL database and expose the
two published views (see §6) in the Lakehouse. Near-real-time with no pipeline to
maintain. *Requires:* app DB on Azure SQL and mirroring enabled on the capacity.

### Option C — Fabric Data Factory pipeline / Dataflow Gen2
Pipeline connects to the app's Azure SQL / PostgreSQL on a schedule and loads
the two views into a Lakehouse or Warehouse. Most control over transforms,
incremental windows, and failure handling.

### Option D — Power BI directly on Azure SQL *(no Fabric required)*
If the requirement is simply "reports for the finance team," Power BI can
Import or DirectQuery the two views straight from Azure SQL. **This delivers the
reporting outcome without any Fabric capacity.** Recommended when Fabric
provisioning is not yet available.

| | A: File + shortcut | B: Mirroring | C: Pipeline | D: Power BI direct |
|---|---|---|---|---|
| Needs Fabric capacity | Yes | Yes | Yes | **No** |
| Needs DB network access | No | Yes | Yes | Yes |
| Latency | Batch | Near-real-time | Batch | On refresh |
| Ongoing maintenance | Low | Lowest | Medium | Low |

---

## 5. Refresh, loading semantics, and DAX

**Load pattern:** **full refresh** (replace the table) is the default and safest —
the dataset is small (thousands of rows per period). If you prefer incremental,
upsert on the primary key (`fact_key` / `entity_name`); rows are immutable except
when a reviewer corrects a value, in which case the key stays stable and the
value changes — so an upsert is correct and idempotent.

**Suggested cadence:** daily during quarter-end close, weekly otherwise.
Each row carries `exported_at` so you can prove data vintage to auditors.

**Starter DAX** (note the mandatory `metric_key` filter):

```dax
-- Total portfolio balance over time (the headline trend)
Portfolio Balance :=
CALCULATE(
    SUM( fact_financial_metric[value_numeric] ),
    fact_financial_metric[metric_key] IN { "ending_balance", "nav", "fair_value", "carrying_value" },
    fact_financial_metric[metric_unit] = "currency"
)

-- Equity income / (loss)
Equity Income :=
CALCULATE(
    SUM( fact_financial_metric[value_numeric] ),
    fact_financial_metric[metric_key] IN { "equity_income_loss", "equity_income" }
)

-- Ownership % (an average, never a sum)
Ownership % :=
CALCULATE(
    AVERAGE( fact_financial_metric[value_numeric] ),
    fact_financial_metric[metric_key] = "ownership_pct"
)
```

Order any time axis by **`period_order`**, not `period_label` (string sorting
would place `2026-03-31` before `2026-Q1` incorrectly).

---

## 6. Database views (for Options B, C, D)

If reading the app database directly, create these views so downstream consumers
never depend on internal table shapes.

> **Dialect note:** the string concatenation below uses `||` (PostgreSQL / SQLite).
> On **Azure SQL** use `CONCAT(f.entity_name, '|', f.period, '|', f.metric_key)`
> instead, and `TRY_CAST` in place of `CAST` if any legacy rows may be non-numeric.

```sql
CREATE VIEW v_fact_financial_metric AS
SELECT
    f.entity_name || '|' || f.period || '|' || f.metric_key AS fact_key,
    f.entity_name, f.entity_kind, f.report_type,
    f.period        AS period_label,
    f.metric_key, f.metric_label,
    f.unit          AS metric_unit,
    CAST(f.value AS DECIMAL(38,6)) AS value_numeric,
    f.currency      AS currency_code,
    s.filename      AS source_file,
    f.provenance    AS source_locator,
    f.status        AS trust_status,
    f.confidence
FROM facts f
JOIN submissions s ON s.id = f.submission_id
WHERE f.status IN ('auto','confirmed') AND f.value IS NOT NULL;

CREATE VIEW v_dim_entity AS
SELECT entity_name, entity_type AS entity_kind, latest_period,
       investment_balance, total_commitment, ownership_value AS ownership_pct
FROM investment_entities;
```

`period_order` / `period_year` / `period_quarter` are derived in the application
layer; compute them in the view or the Fabric load step if you need them here
(quarter = `(month-1)/3 + 1`).

---

## 7. Governance notes

- **Only reviewed data leaves the system.** Rows are exported solely with
  `trust_status IN ('auto','confirmed')`; `needs_review` and `rejected` are
  excluded by construction, so a Fabric report cannot display an unconfirmed figure.
- **Audit traceability.** Every fact carries `source_file` + `source_locator`,
  so any number in a dashboard can be traced to the document and cell it came
  from. The application additionally retains an append-only audit trail of every
  value and change (old → new, who, when) — available on request but **not**
  part of this export.
- **Money precision.** Stored and exported as exact decimal. Do not convert to
  float anywhere in the pipeline.
- **Data classification.** The export contains confidential financial data. Apply
  the same sensitivity labels, workspace access controls, and row-level security
  you use for other treasury reporting.

---

## Versioning

`schema_version` is **1.0**. Within a major version we will only **append**
columns — never rename, remove, or re-type existing ones. Breaking changes ship
as `2.0` with both versions available during a transition window. Pin your
pipelines to the version reported by `GET /api/fabric/schema`.

---

## Open items for the platform team

1. Which **landing option** (A–D)? → determines what we schedule vs what you pull.
2. Which **Fabric workspace and capacity**, and who owns the semantic model?
3. Is **Azure SQL** confirmed as the app's OLTP store (vs PostgreSQL)?
4. **Network path** from Fabric/Power BI to the database — private endpoint, or
   firewall allow-list?
5. **Refresh cadence** and whether quarter-end needs an on-demand trigger.
6. **Retention** for exported snapshots (audit may want point-in-time copies).
