"""Fabric-ready export — a stable, typed contract for downstream analytics.

Purpose
-------
Downstream consumers (Microsoft Fabric Lakehouse/Warehouse, Power BI, Databricks,
plain Excel) need a schema that does **not** drift as the application evolves.
Everything in this module is therefore versioned and append-only in spirit:

  * Column names and order are frozen per `SCHEMA_VERSION`.
  * Types are explicit and documented (see `FACT_COLUMNS`).
  * Only **trusted** rows are exported (status `auto` or `confirmed`), so the
    export can never contain an unreviewed figure.

Two tables are published:

  `fact_financial_metric`  one row per (entity x period x metric) — the grand ledger
  `dim_entity`             one row per entity, with its latest balance

Grain, keys and refresh guidance live in `docs/FABRIC_INTEGRATION.md`.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from . import db

SCHEMA_VERSION = "1.0"

# (name, type, description) — FROZEN for this schema version. Append only.
FACT_COLUMNS: list[tuple[str, str, str]] = [
    ("fact_key",       "string",  "Stable surrogate key: entity|period|metric"),
    ("entity_name",    "string",  "Company / fund / JV name (as classified)"),
    ("entity_kind",    "string",  "fund | joint_venture | direct_investment | unknown"),
    ("report_type",    "string",  "Source report type key"),
    ("report_label",   "string",  "Human-readable report type"),
    ("period_label",   "string",  "Normalized period: YYYY-Qn | YYYY-MM-DD | YYYY-FY"),
    ("period_order",   "int64",   "Sortable period key, YYYYQ (e.g. 202601)"),
    ("period_year",    "int64",   "Calendar year of the period"),
    ("period_quarter", "int64",   "Calendar quarter 1-4 (0 if not determinable)"),
    ("metric_key",     "string",  "Canonical metric key (machine name)"),
    ("metric_label",   "string",  "Canonical metric label (display name)"),
    ("metric_unit",    "string",  "currency | percent | ratio | count | text"),
    ("value_numeric",  "decimal", "The value. Exact decimal; null when not found"),
    ("currency_code",  "string",  "ISO currency code for currency-unit metrics"),
    ("source_file",    "string",  "Originating document file name"),
    ("source_locator", "string",  "Sheet!cell or page/table locator within the document"),
    ("trust_status",   "string",  "auto | confirmed (unreviewed rows are never exported)"),
    ("confidence",     "double",  "Mapping confidence 0-1 at extraction time"),
    ("exported_at",    "string",  "UTC ISO-8601 timestamp of this export run"),
    ("schema_version", "string",  "Export schema version"),
]

DIM_COLUMNS: list[tuple[str, str, str]] = [
    ("entity_name",        "string",  "Primary key — company / fund / JV name"),
    ("entity_kind",        "string",  "fund | joint_venture | direct_investment | unknown"),
    ("latest_period",      "string",  "Most recent period with trusted data"),
    ("investment_balance", "decimal", "Latest trusted balance (ending balance / NAV / FV)"),
    ("total_commitment",   "decimal", "Commitment or cost basis where available"),
    ("ownership_pct",      "double",  "Ownership percentage where available"),
    ("exported_at",        "string",  "UTC ISO-8601 timestamp of this export run"),
    ("schema_version",     "string",  "Export schema version"),
]

FACT_TABLE = "fact_financial_metric"
DIM_TABLE = "dim_entity"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _year_quarter(period: str) -> tuple[int, int]:
    """Derive (year, quarter) from a normalized period label."""
    order = db._period_order(period or "")
    if not order:
        return 0, 0
    return order // 100, order % 100


def build_fact_rows() -> list[dict[str, Any]]:
    """The grand ledger in the frozen Fabric schema (trusted rows only)."""
    from .report_types import REPORT_TYPES
    ts = _now()
    sub_files = {s["id"]: s["filename"] for s in db.list_submissions()}
    rows: list[dict[str, Any]] = []
    for f in db.trusted_facts():
        period = f.get("period") or ""
        year, quarter = _year_quarter(period)
        rt = REPORT_TYPES.get(f.get("report_type") or "")
        prov = f.get("provenance") or ""
        # provenance looks like "excel:Sheet1!B7 (2026)" or "pdf_native:p3 table1 r5c2"
        locator = prov.split(":", 1)[1] if ":" in prov else prov
        rows.append({
            "fact_key": f'{f.get("entity_name")}|{period}|{f.get("metric_key")}',
            "entity_name": f.get("entity_name"),
            "entity_kind": f.get("entity_kind"),
            "report_type": f.get("report_type"),
            "report_label": rt.label if rt else f.get("report_type"),
            "period_label": period,
            "period_order": db._period_order(period),
            "period_year": year,
            "period_quarter": quarter,
            "metric_key": f.get("metric_key"),
            "metric_label": f.get("metric_label"),
            "metric_unit": f.get("unit"),
            "value_numeric": _dec(f.get("value")),
            "currency_code": f.get("currency"),
            "source_file": sub_files.get(f.get("submission_id")),
            "source_locator": locator,
            "trust_status": f.get("status"),
            "confidence": f.get("confidence"),
            "exported_at": ts,
            "schema_version": SCHEMA_VERSION,
        })
    rows.sort(key=lambda r: (r["period_order"], r["entity_name"] or "",
                             r["metric_key"] or ""))
    return rows


def build_dim_rows() -> list[dict[str, Any]]:
    """One row per entity with its latest trusted balance."""
    ts = _now()
    inv = db.get_investments()
    out = []
    for e in inv["entities"]:
        out.append({
            "entity_name": e.get("entity_name"),
            "entity_kind": e.get("entity_type"),
            "latest_period": e.get("latest_period"),
            "investment_balance": _dec(e.get("investment_balance")),
            "total_commitment": _dec(e.get("total_commitment")),
            "ownership_pct": e.get("ownership_value"),
            "exported_at": ts,
            "schema_version": SCHEMA_VERSION,
        })
    out.sort(key=lambda r: (r["entity_kind"] or "", r["entity_name"] or ""))
    return out


# --------------------------------------------------------------------------
# Serializers
# --------------------------------------------------------------------------
def _csv(rows: list[dict[str, Any]], columns: list[tuple[str, str, str]]) -> str:
    import csv
    buf = io.StringIO()
    names = [c[0] for c in columns]
    w = csv.DictWriter(buf, fieldnames=names, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in names})
    return buf.getvalue()


def to_csv(table: str) -> str:
    if table == DIM_TABLE:
        return _csv(build_dim_rows(), DIM_COLUMNS)
    return _csv(build_fact_rows(), FACT_COLUMNS)


def to_parquet(table: str) -> bytes:
    """Parquet bytes for the requested table. Requires pyarrow."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if table == DIM_TABLE:
        rows, columns = build_dim_rows(), DIM_COLUMNS
    else:
        rows, columns = build_fact_rows(), FACT_COLUMNS

    fields, arrays = [], []
    for name, typ, _desc in columns:
        vals = [r.get(name) for r in rows]
        if typ == "int64":
            pa_type = pa.int64()
            vals = [None if v is None else int(v) for v in vals]
        elif typ == "double":
            pa_type = pa.float64()
            vals = [None if v is None else float(v) for v in vals]
        elif typ == "decimal":
            # decimal128(38,6) keeps cents exact and survives Fabric/Spark typing
            pa_type = pa.decimal128(38, 6)
            vals = [None if v is None else Decimal(v).quantize(Decimal("0.000001"))
                    for v in vals]
        else:
            pa_type = pa.string()
            vals = [None if v is None else str(v) for v in vals]
        fields.append(pa.field(name, pa_type))
        arrays.append(pa.array(vals, type=pa_type))

    table_obj = pa.Table.from_arrays(arrays, schema=pa.schema(fields))
    sink = io.BytesIO()
    pq.write_table(table_obj, sink, compression="snappy")
    return sink.getvalue()


def schema_manifest() -> dict[str, Any]:
    """Machine-readable contract for downstream consumers."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "tables": [
            {
                "name": FACT_TABLE,
                "grain": "one row per entity x period x metric",
                "primary_key": ["fact_key"],
                "sort_key": ["period_order", "entity_name", "metric_key"],
                "row_count": len(build_fact_rows()),
                "columns": [{"name": n, "type": t, "description": d}
                            for n, t, d in FACT_COLUMNS],
            },
            {
                "name": DIM_TABLE,
                "grain": "one row per entity",
                "primary_key": ["entity_name"],
                "row_count": len(build_dim_rows()),
                "columns": [{"name": n, "type": t, "description": d}
                            for n, t, d in DIM_COLUMNS],
            },
        ],
        "notes": [
            "Only trust_status in ('auto','confirmed') is exported; unreviewed "
            "values are never published.",
            "Money is exact decimal (decimal128(38,6) in Parquet) — never float.",
            "Full-refresh semantics: replace the table on each load, or upsert on "
            "the primary key.",
        ],
    }
