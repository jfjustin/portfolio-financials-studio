"""Storage + immutable audit trail, on SQLAlchemy Core.

Works across SQLite (local default), Azure SQL, and PostgreSQL — pick the backend
with `DATABASE_URL` (see config.sqlalchemy_url). The public functions below keep
the same signatures regardless of backend, so the rest of the app is unaware of
where the data lives.

Design choices for confidential financial data:
- Money is stored as TEXT (exact Decimal string), never float, so no rounding drift.
- Every change to a fact writes an `audit` row (who/when/old/new). Audit rows are
  append-only; the app never updates or deletes them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text,
                        create_engine, func, insert, select, update)
from sqlalchemy.engine import Engine

from .config import settings
from .models import ExtractionResult

metadata = MetaData()

submissions = Table(
    "submissions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("filename", String(512), nullable=False),
    Column("file_hash", String(128), nullable=False, index=True),
    Column("report_type", String(64), nullable=False),
    Column("report_type_confidence", Float, default=0),
    Column("entity_name", String(512)),
    Column("period", String(64)),
    Column("currency", String(16)),
    Column("extractor", String(64)),
    Column("status", String(32), default="pending"),   # pending|reviewed
    Column("warnings", Text),                            # json list
    Column("unmapped", Text),                            # json list
    Column("uploaded_at", String(40), nullable=False),
)

facts = Table(
    "facts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("submission_id", Integer, nullable=False, index=True),
    Column("entity_name", String(512)),
    Column("entity_kind", String(64)),
    Column("report_type", String(64)),
    Column("period", String(64)),
    Column("metric_key", String(128)),
    Column("metric_label", String(256)),
    Column("value", String(64)),            # Decimal as string; NULL == no value found
    Column("unit", String(32)),
    Column("currency", String(16)),
    Column("raw_label", Text),
    Column("raw_value", Text),
    Column("provenance", Text),
    Column("confidence", Float),
    Column("status", String(32)),           # auto|needs_review|confirmed|rejected
    Column("flags", Text),                  # json list
    Column("created_at", String(40)),
    Column("updated_at", String(40)),
)

audit = Table(
    "audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(40), nullable=False),
    Column("submission_id", Integer),
    Column("fact_id", Integer),
    Column("action", String(32), nullable=False),  # ingest|auto_accept|flag|edit|confirm|reject
    Column("field", String(64)),
    Column("old_value", Text),
    Column("new_value", Text),
    Column("user", String(128), default="local"),
    Column("note", Text),
)

# --------------------------------------------------------------------------
# Investment read-model (ported from the lam-fund-intelligence schema).
# These are a projection of TRUSTED facts (auto+confirmed), rebuilt on demand,
# so they always agree with the reviewed data and never drift.
# --------------------------------------------------------------------------
investment_entities = Table(
    "investment_entities", metadata,
    Column("entity_id", Integer, primary_key=True, autoincrement=True),
    Column("entity_name", String(512), unique=True),
    Column("entity_type", String(64)),
    Column("accounting_method", String(64)),
    Column("investment_balance", Float),
    Column("equity_income_loss", Float),
    Column("return_of_capital", Float),
    Column("total_commitment", Float),
    Column("remaining_commitment", Float),
    Column("ownership_value", Float),
    Column("ownership_text", String(64)),
    Column("initial_investment_date", String(40)),
    Column("gl_account", String(64)),
    Column("company_code", String(64)),
    Column("country", String(64)),
    Column("latest_period", String(64)),
    Column("updated_at", String(40)),
)

investment_quarters = Table(
    "investment_quarters", metadata,
    Column("record_id", Integer, primary_key=True, autoincrement=True),
    Column("entity_name", String(512)),
    Column("period_label", String(64)),
    Column("period_order", Integer),
    Column("accounting_method", String(64)),
    Column("beginning_balance", Float),
    Column("funding", Float),
    Column("equity_income_loss", Float),
    Column("return_of_capital", Float),
    Column("dividends", Float),
    Column("other", Float),
    Column("investment_mtm", Float),
    Column("sale_of_investment", Float),
    Column("accrued_interest", Float),
    Column("note_conversion", Float),
    Column("gain_loss_on_conversion", Float),
    Column("acquisition_impact", Float),
    Column("impairment", Float),
    Column("change", Float),
    Column("ending_balance", Float),
)


# --------------------------------------------------------------------------
# Engine (lazy singleton)
# --------------------------------------------------------------------------
_engine: Optional[Engine] = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = settings.sqlalchemy_url
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            settings.ensure_dirs()
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
    return _engine


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    metadata.create_all(get_engine())


def _fact_value_str(v: Optional[Decimal]) -> Optional[str]:
    return None if v is None else str(v)


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------
def save_extraction(result: ExtractionResult) -> int:
    """Persist an ExtractionResult and all its facts. Returns submission_id."""
    eng = get_engine()
    with eng.begin() as conn:
        res = conn.execute(insert(submissions).values(
            filename=result.filename, file_hash=result.file_hash,
            report_type=result.detected_report_type,
            report_type_confidence=result.report_type_confidence,
            entity_name=result.entity_name, period=result.period,
            currency=result.currency, extractor=result.extractor, status="pending",
            warnings=json.dumps(result.warnings),
            unmapped=json.dumps([i.model_dump() for i in result.unmapped]),
            uploaded_at=_now()))
        sub_id = int(res.inserted_primary_key[0])
        conn.execute(insert(audit).values(
            ts=_now(), submission_id=sub_id, action="ingest",
            note=(f"{result.extractor}: {len(result.facts)} facts, "
                  f"{len(result.unmapped)} unmapped")))
        for f in result.facts:
            unit = f.unit if isinstance(f.unit, str) else f.unit.value
            status = f.status if isinstance(f.status, str) else f.status.value
            fres = conn.execute(insert(facts).values(
                submission_id=sub_id, entity_name=f.entity_name,
                entity_kind=f.entity_kind, report_type=f.report_type, period=f.period,
                metric_key=f.metric_key, metric_label=f.metric_label,
                value=_fact_value_str(f.value), unit=unit, currency=f.currency,
                raw_label=f.raw_label, raw_value=f.raw_value, provenance=f.provenance,
                confidence=f.confidence, status=status, flags=json.dumps(f.flags),
                created_at=_now(), updated_at=_now()))
            fact_id = int(fres.inserted_primary_key[0])
            conn.execute(insert(audit).values(
                ts=_now(), submission_id=sub_id, fact_id=fact_id,
                action="auto_accept" if status == "auto" else "flag",
                new_value=_fact_value_str(f.value),
                note="; ".join(f.flags) if f.flags else None))
        return sub_id


def update_fact(fact_id: int, *, value: Optional[str] = None,
                metric_key: Optional[str] = None, status: Optional[str] = None,
                note: str = "", user: str = "local") -> None:
    """Edit a fact and append an audit row for each changed field."""
    eng = get_engine()
    with eng.begin() as conn:
        row = conn.execute(select(facts).where(facts.c.id == fact_id)).mappings().first()
        if not row:
            raise KeyError(f"fact {fact_id} not found")
        changes: list[tuple[str, Any, Any]] = []
        if value is not None and value != (row["value"] or ""):
            changes.append(("value", row["value"], value))
        if metric_key is not None and metric_key != row["metric_key"]:
            changes.append(("metric_key", row["metric_key"], metric_key))
        if status is not None and status != row["status"]:
            changes.append(("status", row["status"], status))

        if changes:
            vals = {field: new for field, _old, new in changes}
            vals["updated_at"] = _now()
            conn.execute(update(facts).where(facts.c.id == fact_id).values(**vals))
        for field, old, new in changes:
            action = ("confirm" if field == "status" and new == "confirmed" else
                      "reject" if field == "status" and new == "rejected" else "edit")
            conn.execute(insert(audit).values(
                ts=_now(), submission_id=row["submission_id"], fact_id=fact_id,
                action=action, field=field, old_value=str(old), new_value=str(new),
                user=user, note=note or None))


def mark_submission_reviewed(sub_id: int, user: str = "local") -> None:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(update(submissions).where(submissions.c.id == sub_id)
                     .values(status="reviewed"))
        conn.execute(insert(audit).values(
            ts=_now(), submission_id=sub_id, action="review_complete", user=user))


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
def list_submissions() -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        subs = conn.execute(select(submissions).order_by(submissions.c.id.desc())
                            ).mappings().all()
        # counts per submission
        total = dict(conn.execute(
            select(facts.c.submission_id, func.count())
            .group_by(facts.c.submission_id)).all())
        review = dict(conn.execute(
            select(facts.c.submission_id, func.count())
            .where(facts.c.status == "needs_review")
            .group_by(facts.c.submission_id)).all())
        out = []
        for s in subs:
            d = dict(s)
            d["n_facts"] = int(total.get(s["id"], 0))
            d["n_review"] = int(review.get(s["id"], 0))
            out.append(d)
        return out


def get_submission(sub_id: int) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(submissions).where(submissions.c.id == sub_id)
                          ).mappings().first()
        return dict(row) if row else None


def get_facts(sub_id: Optional[int] = None,
              status: Optional[str] = None) -> list[dict[str, Any]]:
    stmt = select(facts)
    if sub_id is not None:
        stmt = stmt.where(facts.c.submission_id == sub_id)
    if status is not None:
        stmt = stmt.where(facts.c.status == status)
    stmt = stmt.order_by(facts.c.id)
    eng = get_engine()
    with eng.connect() as conn:
        out = []
        for r in conn.execute(stmt).mappings().all():
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            out.append(d)
        return out


def get_audit(sub_id: Optional[int] = None, limit: int = 500) -> list[dict[str, Any]]:
    stmt = select(audit)
    if sub_id is not None:
        stmt = stmt.where(audit.c.submission_id == sub_id)
    stmt = stmt.order_by(audit.c.id.desc()).limit(limit)
    eng = get_engine()
    with eng.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def file_hash_exists(file_hash: str) -> Optional[int]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            select(submissions.c.id).where(submissions.c.file_hash == file_hash)
            .order_by(submissions.c.id.desc()).limit(1)).first()
        return int(row[0]) if row else None


# --------------------------------------------------------------------------
# Aggregations for the dashboard (only CONFIRMED + AUTO facts count as trusted)
# --------------------------------------------------------------------------
def trusted_facts() -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(facts).where(facts.c.status.in_(("auto", "confirmed")))
            .where(facts.c.value.is_not(None))).mappings().all()
        return [dict(r) for r in rows]


def dashboard_summary() -> dict[str, Any]:
    facts_ = trusted_facts()
    subs = list_submissions()
    n_review = sum(s["n_review"] for s in subs)

    def dec(v):
        try:
            return Decimal(v)
        except Exception:
            return Decimal(0)

    by_type: dict[str, Decimal] = {}
    by_entity: dict[str, Decimal] = {}
    periods: set[str] = set()
    for f in facts_:
        periods.add(f["period"])
        if f["unit"] == "currency" and f["metric_key"] in (
                "nav", "fair_value", "carrying_value", "jv_total_equity",
                "ending_balance"):
            by_type[f["report_type"]] = by_type.get(f["report_type"], Decimal(0)) + dec(f["value"])
            by_entity[f["entity_name"]] = by_entity.get(f["entity_name"], Decimal(0)) + dec(f["value"])

    return {
        "n_submissions": len(subs),
        "n_facts": len(facts_),
        "n_pending_review": n_review,
        "n_entities": len({f["entity_name"] for f in facts_}),
        "periods": sorted(periods),
        "value_by_type": {k: str(v) for k, v in by_type.items()},
        "value_by_entity": {k: str(v) for k, v in sorted(
            by_entity.items(), key=lambda kv: kv[1], reverse=True)[:12]},
    }


# --------------------------------------------------------------------------
# Investment read-model projection (lam-fund-intelligence shape)
# --------------------------------------------------------------------------
_QUARTER_KEYS = ("beginning_balance", "funding", "equity_income_loss",
                 "return_of_capital", "dividends", "other", "investment_mtm",
                 "sale_of_investment", "accrued_interest", "note_conversion",
                 "gain_loss_on_conversion", "acquisition_impact", "impairment",
                 "change", "ending_balance")
_BALANCE_KEYS = ("ending_balance", "nav", "fair_value", "carrying_value",
                 "jv_total_equity")


def _period_order(period: str) -> int:
    """Sortable integer from a normalized period (YYYY-Qn / YYYY-MM-DD / YYYY-FY)."""
    import re
    if not period:
        return 0
    y = re.search(r"(20\d{2})", period)
    year = int(y.group(1)) if y else 0
    q = re.search(r"Q([1-4])", period)
    if q:
        return year * 100 + int(q.group(1))
    m = re.search(r"-(\d{2})-\d{2}", period) or re.search(r"-(\d{2})$", period)
    if m:
        return year * 100 + (int(m.group(1)) - 1) // 3 + 1
    return year * 100


def _as_float(v):
    try:
        return float(Decimal(v))
    except Exception:
        return None


def rebuild_investment_views() -> None:
    """Rebuild investment_entities / investment_quarters from trusted facts."""
    facts_ = trusted_facts()
    # group facts by (entity, period)
    groups: dict[tuple[str, str], dict[str, str]] = {}
    for f in facts_:
        key = (f["entity_name"], f["period"])
        groups.setdefault(key, {})[f["metric_key"]] = f["value"]
    report_type_by_key: dict[tuple[str, str], str] = {}
    kind_by_entity: dict[str, str] = {}
    for f in facts_:
        report_type_by_key[(f["entity_name"], f["period"])] = f["report_type"]
        kind_by_entity.setdefault(f["entity_name"], f["entity_kind"])

    quarter_rows = []
    for (entity, period), metrics in groups.items():
        if report_type_by_key.get((entity, period)) != "investment_roll_forward":
            continue
        row = {"entity_name": entity, "period_label": period,
               "period_order": _period_order(period), "accounting_method": None}
        for k in _QUARTER_KEYS:
            row[k] = _as_float(metrics[k]) if k in metrics else None
        quarter_rows.append(row)

    # one entity row each, using the latest period's balance
    entity_rows = []
    by_entity: dict[str, list[tuple[int, str, dict]]] = {}
    for (entity, period), metrics in groups.items():
        by_entity.setdefault(entity, []).append((_period_order(period), period, metrics))
    for entity, periods_ in by_entity.items():
        periods_.sort(key=lambda t: t[0])
        latest_order, latest_period, _ = periods_[-1]
        merged: dict[str, str] = {}
        for _o, _p, metrics in periods_:
            merged.update(metrics)
        bal = next((_as_float(merged[k]) for k in _BALANCE_KEYS if k in merged), None)
        own = _as_float(merged.get("ownership_pct")) if "ownership_pct" in merged else None
        entity_rows.append({
            "entity_name": entity, "entity_type": kind_by_entity.get(entity, "unknown"),
            "accounting_method": None,
            "investment_balance": bal,
            "equity_income_loss": _as_float(merged.get("equity_income_loss")
                                            or merged.get("equity_income")),
            "return_of_capital": _as_float(merged.get("return_of_capital")),
            "total_commitment": _as_float(merged.get("commitment")
                                          or merged.get("cost_basis")),
            "remaining_commitment": None,
            "ownership_value": own, "ownership_text": (f"{own}" if own is not None else None),
            "initial_investment_date": None, "gl_account": None, "company_code": None,
            "country": None, "latest_period": latest_period, "updated_at": _now(),
        })

    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(investment_quarters.delete())
        conn.execute(investment_entities.delete())
        if quarter_rows:
            conn.execute(insert(investment_quarters), quarter_rows)
        if entity_rows:
            conn.execute(insert(investment_entities), entity_rows)


def grand_ledger() -> list[dict[str, Any]]:
    """Every TRUSTED fact across all companies/periods as one consolidated ledger,
    normalized to a common shape regardless of the original report format."""
    out = []
    for f in trusted_facts():
        out.append({
            "entity_name": f["entity_name"], "entity_kind": f["entity_kind"],
            "report_type": f["report_type"], "period": f["period"],
            "period_order": _period_order(f["period"]),
            "metric_key": f["metric_key"], "metric_label": f["metric_label"],
            "value": f["value"], "unit": f["unit"], "currency": f["currency"],
            "source": f["provenance"],
        })
    out.sort(key=lambda r: (r["period_order"], r["entity_name"], r["metric_key"]))
    return out


def timeseries() -> dict[str, Any]:
    """Portfolio value over time: one balance per (entity, period) chosen by the
    preferred balance metric, giving a Total line plus per-entity series."""
    rank = {k: i for i, k in enumerate(_BALANCE_KEYS)}
    best: dict[tuple[str, str], tuple[int, float]] = {}
    for f in trusted_facts():
        if f["unit"] != "currency" or f["metric_key"] not in rank:
            continue
        val = _as_float(f["value"])
        if val is None:
            continue
        key = (f["entity_name"], f["period"])
        r = rank[f["metric_key"]]
        if key not in best or r < best[key][0]:
            best[key] = (r, val)
    periods = sorted({p for (_e, p) in best}, key=_period_order)
    entities = sorted({e for (e, _p) in best})
    series = []
    for e in entities:
        pts = [{"period": p, "value": best[(e, p)][1]} for p in periods if (e, p) in best]
        if pts:
            series.append({"name": e, "points": pts})
    total = [{"period": p,
              "value": sum(best[(e, p)][1] for e in entities if (e, p) in best)}
             for p in periods]
    return {"periods": periods, "total": total, "series": series}


def get_investments() -> dict[str, Any]:
    """Return the investment read-model (rebuilt from trusted facts)."""
    rebuild_investment_views()
    eng = get_engine()
    with eng.connect() as conn:
        entities = [dict(r) for r in conn.execute(
            select(investment_entities)
            .order_by(investment_entities.c.entity_type.desc(),
                      investment_entities.c.entity_name)).mappings().all()]
        quarters = [dict(r) for r in conn.execute(
            select(investment_quarters)
            .order_by(investment_quarters.c.entity_name,
                      investment_quarters.c.period_order)).mappings().all()]
    return {"entities": entities, "quarters": quarters}
