"""The lake: system of record for the document flow (docs/ARCHITECTURE.md).

    01 Input    uploads — PDF / Excel / CSV
    02 Land     Files/landing/<entity>/<period>/<document>/<file>  originals, as received
                Tables/land_document_registry  company, period, source, document id,
                                               content hash, extraction revision
    03 Extract  the app's parsers + Azure OpenAI; the raw output lands in Bronze
    04 Store    Files/bronze/<document>/extraction_r<n>.json  each extraction, as produced
                Tables/bronze_raw_records       every line read, with page/sheet evidence
                Tables/silver_financial_metric  standardized values, every review state
                Tables/gold_*                   published values — dimensional, versioned
    05 Model    business rules and reconciliation (validate, carry-forward) in the app
    06-07       Power BI / Fabric notebooks / SQL over Gold

Backends: local Delta files (development, tests), Azure Data Lake Storage Gen2
(OneLake-compatible), Microsoft Fabric OneLake. Lake writes never block or break
the app: a failure is recorded in the lake status and retried on the next sync.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .config import settings

log = logging.getLogger("pfs.lake")

# --------------------------------------------------------------------------
# Table definitions (column, type) — the contract Fabric / Power BI build on
# --------------------------------------------------------------------------
_REGISTRY = [("document_id", "string"), ("revision", "int64"), ("submission_id", "int64"),
             ("entity_name", "string"), ("period_label", "string"), ("report_type", "string"),
             ("source_file", "string"), ("content_hash", "string"), ("landed_path", "string"),
             ("extractor", "string"), ("n_values", "int64"), ("n_unmapped", "int64"),
             ("landed_at", "timestamp")]
_RAW = [("document_id", "string"), ("revision", "int64"), ("submission_id", "int64"),
        ("line_no", "int64"), ("section", "string"), ("label", "string"),
        ("value_raw", "string"), ("locator", "string"), ("mapped_metric", "string"),
        ("extracted_at", "timestamp")]
_SILVER = [("fact_id", "int64"), ("submission_id", "int64"), ("document_id", "string"),
           ("revision", "int64"), ("entity_name", "string"), ("entity_kind", "string"),
           ("report_type", "string"), ("period_label", "string"), ("period_start", "string"),
           ("period_end", "string"), ("fiscal_label", "string"), ("metric_key", "string"),
           ("metric_label", "string"), ("metric_category", "string"), ("metric_unit", "string"),
           ("value", "decimal"), ("currency_code", "string"), ("reported_value", "decimal"),
           ("raw_label", "string"), ("raw_value", "string"), ("source_locator", "string"),
           ("confidence", "double"), ("review_status", "string"), ("flags", "string"),
           ("updated_at", "timestamp")]
_GOLD_FACT = [("fact_key", "string"), ("entity_name", "string"), ("entity_kind", "string"),
              ("report_type", "string"), ("period_label", "string"), ("period_order", "int64"),
              ("fiscal_label", "string"), ("metric_key", "string"), ("metric_label", "string"),
              ("metric_category", "string"), ("metric_unit", "string"),
              ("value_numeric", "decimal"), ("currency_code", "string"),
              ("document_id", "string"), ("source_file", "string"), ("source_locator", "string"),
              ("review_status", "string"), ("reporting_version", "int64"),
              ("published_at", "timestamp")]
_GOLD_ENTITY = [("entity_name", "string"), ("entity_kind", "string"),
                ("latest_period", "string"), ("investment_balance", "decimal"),
                ("total_commitment", "decimal"), ("currency_code", "string"),
                ("published_at", "timestamp")]
_GOLD_PERIOD = [("period_label", "string"), ("period_order", "int64"),
                ("period_year", "int64"), ("period_quarter", "int64"),
                ("fiscal_year", "int64"), ("fiscal_quarter", "int64"), ("fiscal_label", "string")]
_GOLD_METRIC = [("report_type", "string"), ("metric_key", "string"), ("metric_label", "string"),
                ("metric_category", "string"), ("metric_unit", "string")]
_RF_KEYS = ("beginning_balance", "funding", "equity_income_loss", "return_of_capital",
            "dividends", "investment_mtm", "other", "change", "ending_balance")
_GOLD_QUARTER = ([("entity_name", "string"), ("period_label", "string"),
                  ("period_order", "int64"), ("fiscal_label", "string")]
                 + [(k, "decimal") for k in _RF_KEYS]
                 + [("adjustments", "string"), ("reporting_version", "int64"),
                    ("published_at", "timestamp")])
_GOLD_PUBLICATION = [("entity_name", "string"), ("period_label", "string"),
                     ("version", "int64"), ("content_hash", "string"),
                     ("n_values", "int64"), ("published_at", "timestamp")]

TABLES: dict[str, list[tuple[str, str]]] = {
    "land_document_registry": _REGISTRY,
    "bronze_raw_records": _RAW,
    "silver_financial_metric": _SILVER,
    "gold_fact_financial_metric": _GOLD_FACT,
    "gold_dim_entity": _GOLD_ENTITY,
    "gold_dim_period": _GOLD_PERIOD,
    "gold_dim_metric": _GOLD_METRIC,
    "gold_fact_investment_quarter": _GOLD_QUARTER,
    "gold_publication": _GOLD_PUBLICATION,
}
STAGE_OF = {"land": "02 Land", "bronze": "04 Store · Bronze",
            "silver": "04 Store · Silver", "gold": "04 Store · Gold"}


def enabled() -> bool:
    return settings.lake_backend in ("local", "adls", "onelake")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(v: Any) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def document_id(file_hash: str) -> str:
    """Stable id for a document: its content hash (same bytes = same document)."""
    return f"doc-{(file_hash or '')[:16]}"


def _safe(part: Optional[str]) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", part or "unknown").strip("_") or "unknown"


# --------------------------------------------------------------------------
# Storage access
# --------------------------------------------------------------------------
_credential = None
_token: tuple[str, float] = ("", 0.0)


def _azure_credential():
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential
        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _credential


def _bearer_token() -> str:
    global _token
    if time.time() > _token[1] - 300:
        t = _azure_credential().get_token("https://storage.azure.com/.default")
        _token = (t.token, float(t.expires_on))
    return _token[0]


def _local_root() -> Path:
    return settings.data_dir / "lake"


def _remote_root() -> str:
    host = urlparse(settings.lake_account_url).netloc
    root = settings.lake_root.strip("/")
    return f"abfss://{settings.lake_filesystem}@{host}" + (f"/{root}" if root else "")


def location() -> str:
    """Where the lake is, for display."""
    if settings.lake_backend == "local":
        return str(_local_root())
    if settings.lake_backend in ("adls", "onelake"):
        return _remote_root()
    return ""


def table_uri(name: str) -> str:
    if settings.lake_backend == "local":
        return str(_local_root() / "Tables" / name)
    return f"{_remote_root()}/Tables/{name}"


def _storage_options() -> dict[str, str]:
    if settings.lake_backend == "local":
        return {}
    opts = {"bearer_token": _bearer_token()}
    if settings.lake_backend == "onelake":
        opts["use_fabric_endpoint"] = "true"
    return opts


def _put_file(rel_path: str, data: bytes, *, overwrite: bool = True) -> str:
    """Write a file under the lake root; returns its lake path."""
    if settings.lake_backend == "local":
        p = _local_root() / rel_path
        if p.exists() and not overwrite:
            return str(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p)
    from azure.storage.filedatalake import DataLakeServiceClient
    root = settings.lake_root.strip("/")
    path = f"{root}/{rel_path}" if root else rel_path
    fs = DataLakeServiceClient(settings.lake_account_url,
                               credential=_azure_credential()).get_file_system_client(
        settings.lake_filesystem)
    client = fs.get_file_client(path)
    if not overwrite and client.exists():
        return f"{_remote_root()}/{rel_path}"
    client.upload_data(data, overwrite=True)
    return f"{_remote_root()}/{rel_path}"


def _arrow_table(columns: list[tuple[str, str]], rows: list[dict[str, Any]]):
    import pyarrow as pa
    types = {"string": pa.string(), "int64": pa.int64(), "double": pa.float64(),
             "decimal": pa.decimal128(38, 6), "timestamp": pa.timestamp("us", tz="UTC")}
    schema = pa.schema([(c, types[t]) for c, t in columns])
    data = {c: [r.get(c) for r in rows] for c, _t in columns}
    return pa.Table.from_pydict(data, schema=schema)


def _write(name: str, rows: list[dict[str, Any]], mode: str) -> None:
    from deltalake import write_deltalake
    write_deltalake(table_uri(name), _arrow_table(TABLES[name], rows), mode=mode,
                    schema_mode="overwrite" if mode == "overwrite" else "merge",
                    storage_options=_storage_options())


def read_table(name: str) -> list[dict[str, Any]]:
    """Rows of a lake table (for checks and tests); [] if it doesn't exist yet."""
    from deltalake import DeltaTable
    try:
        return DeltaTable(table_uri(name), storage_options=_storage_options()).to_pyarrow_table().to_pylist()
    except Exception:
        return []


# --------------------------------------------------------------------------
# Status (shown in the app)
# --------------------------------------------------------------------------
_lock = threading.Lock()
_status: dict[str, Any] = {"last_sync": None, "last_error": None, "tables": {},
                           "last_landed": None}


def status() -> dict[str, Any]:
    return {"enabled": enabled(), "backend": settings.lake_backend, "location": location(),
            "publish_auto_accepted": settings.publish_auto_accepted, **_status}


def _note(table: str, rows: int) -> None:
    _status["tables"][table] = {"rows": rows, "at": _now().isoformat(timespec="seconds")}


# --------------------------------------------------------------------------
# 02 Land + 04 Bronze — once per extraction
# --------------------------------------------------------------------------
def record_extraction(sub_id: int, result, source: Optional[Path], revision: int) -> None:
    """Land the original (once — originals are immutable) and register the
    document; store the extraction as produced (Bronze). Never raises."""
    if not enabled():
        return
    try:
        with _lock:
            _record_extraction(sub_id, result, source, revision)
        _status["last_landed"] = _now().isoformat(timespec="seconds")
    except Exception as e:  # the app keeps working; the next sync retries Silver/Gold
        log.warning("lake: landing submission %s failed: %s", sub_id, e)
        _status["last_error"] = f"landing #{sub_id}: {e}"


def _record_extraction(sub_id: int, result, source: Optional[Path], revision: int) -> None:
    doc = document_id(result.file_hash)
    now = _now()
    landed = ""
    if source is not None and Path(source).exists():
        landed = _put_file(f"Files/landing/{_safe(result.entity_name)}/{_safe(result.period)}/"
                           f"{doc}/{_safe(result.filename)}", Path(source).read_bytes(),
                           overwrite=False)
    mapped = {}
    for f in result.facts:
        for part in (f.raw_label or "").split(" + "):
            mapped.setdefault(part.strip(), f.metric_key)
    raw = [{"document_id": doc, "revision": revision, "submission_id": sub_id, "line_no": i,
            "section": it.section or None, "label": it.label, "value_raw": it.value_raw,
            "locator": it.provenance, "mapped_metric": mapped.get(it.label.strip()),
            "extracted_at": now}
           for i, it in enumerate(result.raw_items, start=1)]
    bronze = {
        "document_id": doc, "revision": revision, "submission_id": sub_id,
        "source_file": result.filename, "content_hash": result.file_hash,
        "extracted_at": now.isoformat(timespec="seconds"), "extractor": result.extractor,
        "report_type": result.detected_report_type, "entity_name": result.entity_name,
        "period": result.period, "currency": result.currency,
        "number_format": result.number_format, "warnings": result.warnings,
        "raw_items": [it.model_dump() for it in result.raw_items],
        "unmapped": [it.model_dump() for it in result.unmapped],
        "facts": [json.loads(f.model_dump_json()) for f in result.facts],
        "model_reading": result.model_reading,
    }
    _put_file(f"Files/bronze/{doc}/extraction_r{revision}.json",
              json.dumps(bronze, indent=1, default=str).encode())
    _write("land_document_registry", [{
        "document_id": doc, "revision": revision, "submission_id": sub_id,
        "entity_name": result.entity_name, "period_label": result.period,
        "report_type": result.detected_report_type, "source_file": result.filename,
        "content_hash": result.file_hash, "landed_path": landed or None,
        "extractor": result.extractor, "n_values": len(result.facts),
        "n_unmapped": len(result.unmapped), "landed_at": now}], "append")
    if raw:
        _write("bronze_raw_records", raw, "append")


def backfill() -> dict[str, int]:
    """Land and register files the app holds that the lake doesn't have yet
    (uploaded before the lake was switched on). Their Bronze record is built from
    what the app kept (the values and unrecognised lines), marked as a backfill."""
    from . import db, storage
    from .models import ExtractionResult, FinancialFact, RawItem
    if not enabled():
        return {"landed": 0, "missing_source": 0}
    have = {r["submission_id"] for r in read_table("land_document_registry")}
    landed = missing = 0
    for s in db.list_submissions():
        if s["id"] in have:
            continue
        src = storage.load_source(s.get("source_key"), s["filename"], s["file_hash"])
        if src is None:
            missing += 1
        facts_ = db.get_facts(s["id"])
        result = ExtractionResult(
            filename=s["filename"], file_hash=s["file_hash"],
            detected_report_type=s["report_type"],
            report_type_confidence=s.get("report_type_confidence") or 0,
            entity_name=s.get("entity_name") or "UNKNOWN", period=s.get("period") or "UNKNOWN",
            currency=s.get("currency") or "USD", extractor=f"{s.get('extractor')} (backfill)",
            number_format=s.get("number_format") or "1,234.56",
            facts=[FinancialFact(**{k: f[k] for k in (
                "entity_name", "entity_kind", "report_type", "period", "metric_key",
                "metric_label", "unit", "currency", "raw_label", "raw_value", "provenance",
                "confidence", "status")}, value=f["value"], flags=f["flags"]) for f in facts_],
            unmapped=[RawItem(**u) for u in json.loads(s.get("unmapped") or "[]")],
            warnings=["landed by backfill: extracted before the lake was switched on"]
                     + json.loads(s.get("warnings") or "[]"))
        result.raw_items = list(result.unmapped)
        record_extraction(s["id"], result, src, revision=s.get("revision") or 1)
        landed += 1
    return {"landed": landed, "missing_source": missing}


# --------------------------------------------------------------------------
# 04 Silver + Gold — snapshots of the current state, after every change
# --------------------------------------------------------------------------
def sync() -> dict[str, Any]:
    """Write Silver (every value, every review state) and Gold (approved values,
    dimensional, with reporting versions). Returns the lake status."""
    if not enabled():
        return status()
    try:
        with _lock:
            _sync()
        _status["last_sync"] = _now().isoformat(timespec="seconds")
        _status["last_error"] = None
    except Exception as e:
        log.warning("lake: sync failed: %s", e)
        _status["last_error"] = f"sync: {e}"
    return status()


def _sync() -> None:
    from . import db
    from .report_types import REPORT_TYPES
    now = _now()
    subs = {s["id"]: s for s in db.list_submissions()}

    def doc_of(sub_id: int) -> Optional[str]:
        s = subs.get(sub_id)
        return document_id(s["file_hash"]) if s else None

    def category(rt: str, key: str) -> str:
        return db._category_for(rt or "", key or "")[0]

    # Silver: every standardized value, whatever its review state
    silver = []
    for f in db.get_facts():
        silver.append({
            "fact_id": f["id"], "submission_id": f["submission_id"],
            "document_id": doc_of(f["submission_id"]),
            "revision": (subs.get(f["submission_id"]) or {}).get("revision") or 1,
            "entity_name": f["entity_name"], "entity_kind": f["entity_kind"],
            "report_type": f["report_type"], "period_label": f["period"],
            "period_start": f.get("period_start"), "period_end": f.get("period_end"),
            "fiscal_label": db.fiscal_info(f["period"] or "")["fiscal_label"],
            "metric_key": f["metric_key"], "metric_label": f["metric_label"],
            "metric_category": category(f["report_type"], f["metric_key"]),
            "metric_unit": f["unit"], "value": _dec(f["value"]),
            "currency_code": f["currency"], "reported_value": _dec(f.get("reported_value")),
            "raw_label": f["raw_label"], "raw_value": f["raw_value"],
            "source_locator": f["provenance"], "confidence": f["confidence"],
            "review_status": f["status"], "flags": json.dumps(f["flags"]),
            "updated_at": _parse_ts(f.get("updated_at")) or now})
    _write("silver_financial_metric", silver, "overwrite")
    _note("silver_financial_metric", len(silver))

    # Gold: published values (reviewer-approved, plus auto-accepted unless
    # PUBLISH_AUTO_ACCEPTED=false). A slice (entity, period) gets a new reporting
    # version whenever its published content changes.
    approved = db.trusted_facts()
    slices: dict[tuple[str, str], list[dict]] = {}
    for f in approved:
        slices.setdefault((f["entity_name"], f["period"]), []).append(f)
    hashes = {}
    for key, rows in slices.items():
        content = sorted(f"{r['metric_key']}={r['value']} {r['currency']}" for r in rows)
        hashes[key] = (hashlib.sha256("\n".join(content).encode()).hexdigest(), len(rows))
    versions = db.record_publications(hashes)

    gold = []
    for f in approved:
        key = (f["entity_name"], f["period"])
        period = f["period"] or ""
        prov = f["provenance"] or ""
        gold.append({
            "fact_key": f"{f['entity_name']}|{period}|{f['metric_key']}",
            "entity_name": f["entity_name"], "entity_kind": f["entity_kind"],
            "report_type": f["report_type"], "period_label": period,
            "period_order": db._period_order(period),
            "fiscal_label": db.fiscal_info(period)["fiscal_label"],
            "metric_key": f["metric_key"], "metric_label": f["metric_label"],
            "metric_category": category(f["report_type"], f["metric_key"]),
            "metric_unit": f["unit"], "value_numeric": _dec(f["value"]),
            "currency_code": f["currency"], "document_id": doc_of(f["submission_id"]),
            "source_file": (subs.get(f["submission_id"]) or {}).get("filename"),
            "source_locator": prov.split(":", 1)[1] if ":" in prov else prov,
            "review_status": f["status"], "reporting_version": versions.get(key, 1),
            "published_at": now})
    _write("gold_fact_financial_metric", gold, "overwrite")
    _note("gold_fact_financial_metric", len(gold))

    inv = db.get_investments()
    currency_of = {}
    for f in approved:
        if f["unit"] == "currency" and f["currency"]:
            currency_of.setdefault(f["entity_name"], f["currency"])
    entities = [{"entity_name": e["entity_name"], "entity_kind": e.get("entity_type"),
                 "latest_period": e.get("latest_period"),
                 "investment_balance": _dec(e.get("investment_balance")),
                 "total_commitment": _dec(e.get("total_commitment")),
                 "currency_code": currency_of.get(e["entity_name"]), "published_at": now}
                for e in inv["entities"]]
    _write("gold_dim_entity", entities, "overwrite")
    _note("gold_dim_entity", len(entities))

    periods = []
    for p in sorted({f["period"] for f in approved if f["period"]}, key=db._period_order):
        fy = db.fiscal_info(p)
        order = db._period_order(p)
        periods.append({"period_label": p, "period_order": order, "period_year": order // 100,
                        "period_quarter": order % 100, "fiscal_year": fy["fiscal_year"] or 0,
                        "fiscal_quarter": fy["fiscal_quarter"] or 0,
                        "fiscal_label": fy["fiscal_label"]})
    _write("gold_dim_period", periods, "overwrite")
    _note("gold_dim_period", len(periods))

    metrics = [{"report_type": rt.key, "metric_key": m.key, "metric_label": m.label,
                "metric_category": m.category.value, "metric_unit": m.unit.value}
               for rt in REPORT_TYPES.values() for m in rt.metrics]
    _write("gold_dim_metric", metrics, "overwrite")
    _note("gold_dim_metric", len(metrics))

    quarters = []
    for q in inv["quarters"]:
        key = (q["entity_name"], q["period_label"])
        row = {"entity_name": q["entity_name"], "period_label": q["period_label"],
               "period_order": q["period_order"], "fiscal_label": q.get("fiscal_label"),
               "adjustments": json.dumps(q.get("notes") or {}) if q.get("notes") else None,
               "reporting_version": versions.get(key, 1), "published_at": now}
        for k in _RF_KEYS:
            row[k] = _dec(q.get(k))
        quarters.append(row)
    _write("gold_fact_investment_quarter", quarters, "overwrite")
    _note("gold_fact_investment_quarter", len(quarters))

    pubs = [{"entity_name": p["entity_name"], "period_label": p["period"],
             "version": p["version"], "content_hash": p["content_hash"],
             "n_values": p["n_values"], "published_at": _parse_ts(p["published_at"]) or now}
            for p in db.list_publications()]
    _write("gold_publication", pubs, "overwrite")
    _note("gold_publication", len(pubs))

    # Land and Bronze grow by appends; count them for the status card
    from deltalake import DeltaTable
    for name in ("land_document_registry", "bronze_raw_records"):
        try:
            n = DeltaTable(table_uri(name), storage_options=_storage_options()) \
                .to_pyarrow_dataset().count_rows()
        except Exception:
            n = 0     # nothing landed yet
        _note(name, n)


def _parse_ts(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Background sync: after a change, Silver/Gold follow a moment later
# --------------------------------------------------------------------------
_wake = threading.Event()
_worker: Optional[threading.Thread] = None
DEBOUNCE_S = 2.0


def request_sync() -> None:
    """Ask for a Silver/Gold sync soon (coalesces bursts of edits)."""
    global _worker
    if not enabled() or not settings.lake_auto_sync:
        return
    _wake.set()
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, name="lake-sync", daemon=True)
        _worker.start()


def _loop() -> None:
    while True:
        _wake.wait()
        time.sleep(DEBOUNCE_S)
        _wake.clear()
        sync()
