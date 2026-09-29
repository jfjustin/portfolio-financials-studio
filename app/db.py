"""SQLite storage + immutable audit trail.

Design choices for confidential financial data:
- Money is stored as TEXT (exact Decimal string), never float, so no rounding drift.
- Every change to a fact writes an `audit` row (who/when/old/new). Audit rows are
  append-only; the app never updates or deletes them.
- A single local file (data/pipeline.db). No network, no external DB server.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import settings
from .models import ExtractionResult, FinancialFact

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    file_hash       TEXT NOT NULL,
    report_type     TEXT NOT NULL,
    report_type_confidence REAL DEFAULT 0,
    entity_name     TEXT,
    period          TEXT,
    currency        TEXT,
    extractor       TEXT,
    status          TEXT DEFAULT 'pending',   -- pending|reviewed
    warnings        TEXT,                      -- json list
    unmapped        TEXT,                      -- json list
    uploaded_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id),
    entity_name     TEXT,
    entity_kind     TEXT,
    report_type     TEXT,
    period          TEXT,
    metric_key      TEXT,
    metric_label    TEXT,
    value           TEXT,          -- Decimal as string; NULL means "no value found"
    unit            TEXT,
    currency        TEXT,
    raw_label       TEXT,
    raw_value       TEXT,
    provenance      TEXT,
    confidence      REAL,
    status          TEXT,          -- auto|needs_review|confirmed|rejected
    flags           TEXT,          -- json list
    created_at      TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    submission_id   INTEGER,
    fact_id         INTEGER,
    action          TEXT NOT NULL,   -- ingest|auto_accept|flag|edit|confirm|reject
    field           TEXT,
    old_value       TEXT,
    new_value       TEXT,
    user            TEXT DEFAULT 'local',
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_submission ON facts(submission_id);
CREATE INDEX IF NOT EXISTS idx_facts_lookup ON facts(report_type, entity_name, period, metric_key);
CREATE INDEX IF NOT EXISTS idx_audit_fact ON audit(fact_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    settings.ensure_dirs()
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def _fact_value_str(v: Optional[Decimal]) -> Optional[str]:
    return None if v is None else str(v)


def save_extraction(result: ExtractionResult) -> int:
    """Persist an ExtractionResult and all its facts. Returns submission_id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO submissions
               (filename, file_hash, report_type, report_type_confidence,
                entity_name, period, currency, extractor, status, warnings,
                unmapped, uploaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (result.filename, result.file_hash, result.detected_report_type,
             result.report_type_confidence, result.entity_name, result.period,
             result.currency, result.extractor, "pending",
             json.dumps(result.warnings),
             json.dumps([i.model_dump() for i in result.unmapped]), _now()),
        )
        sub_id = cur.lastrowid
        conn.execute(
            "INSERT INTO audit (ts, submission_id, action, note) VALUES (?,?,?,?)",
            (_now(), sub_id, "ingest",
             f"{result.extractor}: {len(result.facts)} facts, "
             f"{len(result.unmapped)} unmapped"),
        )
        for f in result.facts:
            fcur = conn.execute(
                """INSERT INTO facts
                   (submission_id, entity_name, entity_kind, report_type, period,
                    metric_key, metric_label, value, unit, currency, raw_label,
                    raw_value, provenance, confidence, status, flags,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sub_id, f.entity_name, f.entity_kind, f.report_type, f.period,
                 f.metric_key, f.metric_label, _fact_value_str(f.value),
                 f.unit if isinstance(f.unit, str) else f.unit.value,
                 f.currency, f.raw_label, f.raw_value, f.provenance, f.confidence,
                 f.status if isinstance(f.status, str) else f.status.value,
                 json.dumps(f.flags), _now(), _now()),
            )
            conn.execute(
                "INSERT INTO audit (ts, submission_id, fact_id, action, new_value, note)"
                " VALUES (?,?,?,?,?,?)",
                (_now(), sub_id, fcur.lastrowid,
                 "auto_accept" if f.status == "auto" else "flag",
                 _fact_value_str(f.value),
                 "; ".join(f.flags) if f.flags else None),
            )
        return sub_id


def list_submissions() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM facts f WHERE f.submission_id=s.id) AS n_facts,
                      (SELECT COUNT(*) FROM facts f WHERE f.submission_id=s.id
                         AND f.status='needs_review') AS n_review
               FROM submissions s ORDER BY s.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_submission(sub_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
        return dict(row) if row else None


def get_facts(sub_id: Optional[int] = None,
              status: Optional[str] = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM facts WHERE 1=1"
    args: list[Any] = []
    if sub_id is not None:
        q += " AND submission_id=?"
        args.append(sub_id)
    if status is not None:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id"
    with get_conn() as conn:
        rows = conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            out.append(d)
        return out


def update_fact(fact_id: int, *, value: Optional[str] = None,
                metric_key: Optional[str] = None, status: Optional[str] = None,
                note: str = "", user: str = "local") -> None:
    """Edit a fact and append an audit row for each changed field."""
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        if not cur:
            raise KeyError(f"fact {fact_id} not found")
        cur = dict(cur)
        changes: list[tuple[str, Any, Any]] = []
        if value is not None and value != (cur["value"] or ""):
            changes.append(("value", cur["value"], value))
        if metric_key is not None and metric_key != cur["metric_key"]:
            changes.append(("metric_key", cur["metric_key"], metric_key))
        if status is not None and status != cur["status"]:
            changes.append(("status", cur["status"], status))

        sets, args = [], []
        for field, _old, new in changes:
            sets.append(f"{field}=?")
            args.append(new)
        if sets:
            sets.append("updated_at=?")
            args.append(_now())
            args.append(fact_id)
            conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id=?", args)
        for field, old, new in changes:
            action = "confirm" if field == "status" and new == "confirmed" else \
                     "reject" if field == "status" and new == "rejected" else "edit"
            conn.execute(
                "INSERT INTO audit (ts, submission_id, fact_id, action, field,"
                " old_value, new_value, user, note) VALUES (?,?,?,?,?,?,?,?,?)",
                (_now(), cur["submission_id"], fact_id, action, field,
                 str(old), str(new), user, note or None),
            )


def mark_submission_reviewed(sub_id: int, user: str = "local") -> None:
    with get_conn() as conn:
        conn.execute("UPDATE submissions SET status='reviewed' WHERE id=?", (sub_id,))
        conn.execute(
            "INSERT INTO audit (ts, submission_id, action, user, note) VALUES (?,?,?,?,?)",
            (_now(), sub_id, "review_complete", user, None),
        )


def get_audit(sub_id: Optional[int] = None, limit: int = 500) -> list[dict[str, Any]]:
    q = "SELECT * FROM audit"
    args: list[Any] = []
    if sub_id is not None:
        q += " WHERE submission_id=?"
        args.append(sub_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def file_hash_exists(file_hash: str) -> Optional[int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM submissions WHERE file_hash=? ORDER BY id DESC LIMIT 1",
            (file_hash,)).fetchone()
        return row["id"] if row else None


# --------------------------------------------------------------------------
# Aggregations for the dashboard (only CONFIRMED + AUTO facts count as trusted)
# --------------------------------------------------------------------------
def trusted_facts() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM facts WHERE status IN ('auto','confirmed') AND value IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def dashboard_summary() -> dict[str, Any]:
    facts = trusted_facts()
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
    for f in facts:
        periods.add(f["period"])
        if f["unit"] == "currency" and f["metric_key"] in (
                "nav", "fair_value", "carrying_value", "jv_total_equity",
                "ending_balance"):
            by_type[f["report_type"]] = by_type.get(f["report_type"], Decimal(0)) + dec(f["value"])
            by_entity[f["entity_name"]] = by_entity.get(f["entity_name"], Decimal(0)) + dec(f["value"])

    return {
        "n_submissions": len(subs),
        "n_facts": len(facts),
        "n_pending_review": n_review,
        "n_entities": len({f["entity_name"] for f in facts}),
        "periods": sorted(periods),
        "value_by_type": {k: str(v) for k, v in by_type.items()},
        "value_by_entity": {k: str(v) for k, v in sorted(
            by_entity.items(), key=lambda kv: kv[1], reverse=True)[:12]},
    }
