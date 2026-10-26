"""Format memory: how statement lines map to fields, for every fund and per fund.

Four sources, most specific first:

  1. taught (this fund)  — a reviewer taught this fund's wording in the app
  2. learned             — lines behind values a reviewer CONFIRMED for this fund
  3. taught (all funds)  — wording a reviewer taught for every fund
  4. preset              — app/data/statement_terms.json, shipped with the app

The deterministic parser consults them before its generic synonyms, and the
model step is shown them (plus the fund's confirmed columns, last confirmed
quarter and fund parameters) so it reads a familiar layout the way a reviewer
already confirmed. Nothing here is fund-specific code: it's data that grows
as statements are reviewed.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, insert, select, update

from . import db
from .models import label_key

PRESETS_PATH = Path(__file__).parent / "data" / "statement_terms.json"
IGNORE = "ignore"
# source -> (rank, confidence)
_SOURCES = {"taught_fund": (0, 0.95), "learned": (1, 0.95), "taught": (2, 0.92),
            "preset": (3, 0.9)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry(source: str, label: str, metric: str, section: str = "", **extra) -> dict:
    return {"source": source, "label": label, "section": section or "",
            "metric_key": metric, "label_key": label_key(label),
            "section_key": label_key(section) if section else "", **extra}


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _preset_file() -> dict:
    return json.loads(PRESETS_PATH.read_text())


def presets(report_type: str) -> list[dict]:
    return [_entry("preset", t["label"], t["metric"], t.get("section", ""),
                   id=f"preset-{i}", note=t.get("note", ""), entity_name=None)
            for i, t in enumerate(_preset_file().get(report_type, []))]


def taught(report_type: Optional[str] = None) -> list[dict]:
    stmt = select(db.statement_terms).order_by(db.statement_terms.c.id)
    if report_type:
        stmt = stmt.where(db.statement_terms.c.report_type == report_type)
    with db.get_engine().connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [_entry("taught_fund" if r["entity_name"] else "taught", r["label"],
                   r["metric_key"], r["section"] or "", id=r["id"],
                   entity_name=r["entity_name"], note=r["note"] or "",
                   report_type=r["report_type"], created_by=r["created_by"],
                   created_at=r["created_at"])
            for r in rows]


def add_term(report_type: str, label: str, metric_key: str, *, section: str = "",
             entity_name: Optional[str] = None, note: str = "", user: str = "local") -> int:
    """Teach a wording. Re-teaching the same line (same scope) replaces it."""
    lk, sk = label_key(label), label_key(section) if section else ""
    if not lk:
        raise ValueError("empty label")
    t = db.statement_terms
    with db.get_engine().begin() as conn:
        same = (select(t.c.id).where(t.c.report_type == report_type)
                .where(t.c.label_key == lk).where(t.c.section_key == sk)
                .where(t.c.entity_name.is_(None) if entity_name is None
                       else t.c.entity_name == entity_name))
        existing = conn.execute(same).first()
        vals = dict(report_type=report_type, entity_name=entity_name, section=section or "",
                    label=label, section_key=sk, label_key=lk, metric_key=metric_key,
                    note=note, created_by=user, created_at=_now())
        if existing:
            conn.execute(update(t).where(t.c.id == existing[0]).values(**vals))
            term_id = int(existing[0])
        else:
            term_id = int(conn.execute(insert(t).values(**vals)).inserted_primary_key[0])
        conn.execute(insert(db.audit).values(
            ts=_now(), action="teach_term", user=user, field=metric_key, new_value=label,
            note=f"{entity_name or 'all funds'} · {report_type}"
                 + (f" · section '{section}'" if section else "")))
    return term_id


def delete_term(term_id: int, user: str = "local") -> bool:
    t = db.statement_terms
    with db.get_engine().begin() as conn:
        row = conn.execute(select(t).where(t.c.id == term_id)).mappings().first()
        if not row:
            return False
        conn.execute(delete(t).where(t.c.id == term_id))
        conn.execute(insert(db.audit).values(
            ts=_now(), action="forget_term", user=user, field=row["metric_key"],
            old_value=row["label"], note=row["entity_name"] or "all funds"))
    return True


_PROV_COLUMNS = re.compile(r"\[(.+?) \| (.+?)\]")


def _confirmed_facts(entity: str, report_type: str) -> list[dict]:
    f = db.facts
    stmt = (select(f).where(f.c.entity_name == entity).where(f.c.report_type == report_type)
            .where(f.c.status == "confirmed").order_by(f.c.id))
    with db.get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def learned(entity: str, report_type: str, rows: Optional[list[dict]] = None) -> list[dict]:
    """Line wording behind this fund's confirmed values (latest confirmation wins)."""
    rows = _confirmed_facts(entity, report_type) if rows is None else rows
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        if not r.get("raw_label") or "carry_forward" in (r.get("provenance") or "")[:13]:
            continue
        parts = r["raw_label"].split(" + ")
        try:
            secs = json.loads(r.get("raw_sections") or "[]")
        except ValueError:
            secs = []
        secs = (secs + [""] * len(parts))[:len(parts)]
        for label, section in zip(parts, secs):
            e = _entry("learned", label.strip(), r["metric_key"], section or "",
                       fact_id=r["id"], period=r["period"], entity_name=entity)
            if e["label_key"]:
                seen[(e["section_key"], e["label_key"])] = e
    return list(seen.values())


def fund_profile(entity: str) -> dict:
    with db.get_engine().connect() as conn:
        row = conn.execute(select(db.fund_profiles)
                           .where(db.fund_profiles.c.entity_name == entity)).mappings().first()
    return dict(row) if row else {"entity_name": entity, "investor_aliases": "", "notes": ""}


def set_fund_profile(entity: str, *, investor_aliases: str = "", notes: str = "",
                     user: str = "local") -> dict:
    p = db.fund_profiles
    vals = dict(investor_aliases=investor_aliases.strip(), notes=notes.strip(),
                updated_by=user, updated_at=_now())
    with db.get_engine().begin() as conn:
        if conn.execute(select(p.c.entity_name).where(p.c.entity_name == entity)).first():
            conn.execute(update(p).where(p.c.entity_name == entity).values(**vals))
        else:
            conn.execute(insert(p).values(entity_name=entity, **vals))
        conn.execute(insert(db.audit).values(
            ts=_now(), action="fund_profile", user=user, field=entity,
            new_value=json.dumps({"investor_aliases": vals["investor_aliases"],
                                  "notes": vals["notes"]})))
    return fund_profile(entity)


def fund_memory(entity: str, report_type: str) -> dict:
    """What reviewers have confirmed for this fund so far (for the prompt and UI)."""
    rows = _confirmed_facts(entity, report_type)
    by_q: dict[str, dict[str, Decimal]] = {}
    columns: Counter = Counter()
    currencies: Counter = Counter()
    for r in rows:
        if r["value"] is not None:
            by_q.setdefault(r["period"], {})[r["metric_key"]] = Decimal(r["value"])
        m = _PROV_COLUMNS.search(r.get("provenance") or "")
        if m and m.group(1) != "?":
            columns[(m.group(1), m.group(2))] += 1
        if r.get("currency"):
            currencies[r["currency"]] += 1
    quarters = sorted(by_q, key=db._period_order)
    taught_here = [t for t in taught(report_type) if t["entity_name"] == entity]
    return {
        "entity_name": entity,
        "quarters": quarters,
        "last_quarter": ({"period": quarters[-1], "values": by_q[quarters[-1]]}
                         if quarters else None),
        "columns": [{"investor": i, "period": p, "count": n}
                    for (i, p), n in columns.most_common(3)],
        "currency": currencies.most_common(1)[0][0] if currencies else None,
        "learned": learned(entity, report_type, rows),
        "taught": taught_here,
        "profile": fund_profile(entity),
    }


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------
class TermIndex:
    """Recognise a statement line from taught, learned and preset wording."""

    def __init__(self, entries: list[dict]):
        self.entries = sorted(entries, key=lambda e: _SOURCES[e["source"]][0])
        self.by_label: dict[str, list[dict]] = {}
        for e in self.entries:
            self.by_label.setdefault(e["label_key"], []).append(e)

    @classmethod
    def for_entity(cls, entity: str, report_type: str) -> "TermIndex":
        entries = presets(report_type)
        try:
            entries += [t for t in taught(report_type)
                        if t["entity_name"] in (None, entity)]
            if entity and entity != "UNKNOWN":
                entries += learned(entity, report_type)
        except Exception:
            pass  # no database (e.g. extracting outside the app): presets only
        return cls(entries)

    def lookup(self, section: str, label: str) -> Optional[tuple[str, float, str]]:
        """(metric_key or "ignore", confidence, source) — or None if unknown/ambiguous."""
        cands = self.by_label.get(label_key(label))
        if not cands:
            return None
        sk = label_key(section) if section else ""
        # 1. same line under the same heading; 2. wording with no heading attached;
        # 3. a line with no heading here, when every known reading agrees
        for pool in ([e for e in cands if sk and e["section_key"] == sk],
                     [e for e in cands if not e["section_key"]],
                     cands if not sk else []):
            if not pool:
                continue
            best_rank = _SOURCES[pool[0]["source"]][0]
            top = [e for e in pool if _SOURCES[e["source"]][0] == best_rank]
            if len({e["metric_key"] for e in top}) == 1:
                e = top[0]
                return e["metric_key"], _SOURCES[e["source"]][1], e["source"]
            return None   # the most specific source disagrees with itself: don't guess
        return None
