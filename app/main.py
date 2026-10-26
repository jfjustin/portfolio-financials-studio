"""FastAPI application — dashboard, upload, human-in-the-loop review, export.

Bind is localhost-only by default (see config.host). The server makes no outbound
calls in the default deterministic mode; the only outbound traffic is to the
configured LLM provider (Azure OpenAI / OpenAI / Ollama), and only when the LLM
fallback is triggered for a non-standard layout.
"""
from __future__ import annotations

import csv
import hashlib
import io
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, carry_forward, db, fabric_export, performance, terms
from .config import settings
from .llm import get_provider
from .pipeline import process_upload
from .report_types import (CATEGORY_LABELS, CATEGORY_ORDER, REPORT_TYPES,
                           all_report_types)

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="Portfolio Financials Studio", version="1.0.0")
app.add_middleware(auth.AuthMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------
# Browser caching. Every deploy changes static/js/app.js etc. under the same
# URL; without Cache-Control, Chrome heuristically reuses its old copy, so new
# HTML ran against old JavaScript and the page broke. Asset URLs therefore carry
# a version (?v=...) that changes with the file and responses say exactly how they may be cached.
# --------------------------------------------------------------------------
def _asset_version(path: str) -> str:
    """Changes whenever the file does (each deploy, or an edit in development)."""
    try:
        st = (BASE_DIR / "static" / path).stat()
    except OSError:
        return "0"
    return hashlib.sha256(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:12]


def asset_url(path: str) -> str:
    """/static URL with a content-hash version, e.g. js/app.js -> /static/js/app.js?v=1a2b..."""
    return f"/static/{path}?v={_asset_version(path)}"


templates.env.globals["asset_url"] = asset_url


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if "v" in request.query_params
            else "no-cache")
    elif response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "report_types": all_report_types(),
        "llm_provider": settings.llm_provider,
        "llm_enabled": settings.llm_enabled,
        "auth_mode": settings.auth_mode,
        "user": auth.current_user(request) or "",
        "fiscal_year_end_month": settings.fiscal_year_end_month,
    })


# --------------------------------------------------------------------------
# Auth pages / routes (active only when AUTH_MODE != none)
# --------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {
        "auth_mode": settings.auth_mode, "error": error,
    })


@app.post("/api/login")
def login_submit(password: str = Form("")):
    if settings.auth_mode != "password":
        raise HTTPException(400, "password login is not enabled")
    if not auth.verify_password(password):
        return RedirectResponse("/login?error=1", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    auth.issue_session(resp, "workspace")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    auth.clear_session(resp)
    return resp


@app.get("/auth/login")
def entra_login(request: Request):
    if settings.auth_mode != "entra":
        raise HTTPException(400, "entra login is not enabled")
    redirect_uri = str(request.base_url).rstrip("/") + settings.entra_redirect_path
    state = secrets.token_urlsafe(16)
    url = auth.entra_auth_url(redirect_uri, state)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("pfs_oauth_state", state, httponly=True, samesite="lax", path="/")
    return resp


@app.get(settings.entra_redirect_path)
def entra_callback(request: Request, code: str = "", state: str = ""):
    if settings.auth_mode != "entra":
        raise HTTPException(400, "entra login is not enabled")
    if not code or state != request.cookies.get("pfs_oauth_state"):
        return RedirectResponse("/login?error=1", status_code=302)
    redirect_uri = str(request.base_url).rstrip("/") + settings.entra_redirect_path
    user = auth.entra_exchange_code(code, redirect_uri)
    if not user:
        return RedirectResponse("/login?error=1", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    auth.issue_session(resp, user)
    resp.delete_cookie("pfs_oauth_state", path="/")
    return resp


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.get("/api/health")
def health():
    n_subs = len(db.list_submissions())
    return {"status": "ok", "llm_enabled": settings.llm_enabled,
            "llm_provider": settings.llm_provider,
            "provider_status": get_provider().status(),
            "data_dir": str(settings.data_dir),
            "database_url": settings.database_url or "sqlite (local file)",
            "n_submissions": n_subs,
            "storage_persistent": bool(settings.database_url) or
                "/home/" in str(settings.data_dir)}


@app.get("/api/report-types")
def report_types():
    out = {}
    for rt in all_report_types():
        out[rt.key] = {
            "label": rt.label,
            "entity_kind": rt.entity_kind,
            "metrics": [{"key": m.key, "label": m.label, "unit": m.unit.value,
                         "required": m.required, "category": m.category.value,
                         "category_label": CATEGORY_LABELS[m.category]}
                        for m in rt.metrics],
        }
    out["_categories"] = {
        "order": [c.value for c in CATEGORY_ORDER],
        "labels": {c.value: label for c, label in CATEGORY_LABELS.items()},
    }
    return out


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...),
                 allow_duplicate: bool = Form(False)):
    results = []
    for f in files:
        raw = await f.read()
        try:
            res = process_upload(f.filename, raw, allow_duplicate=allow_duplicate)
        except Exception as e:  # never crash the whole batch on one bad file
            res = {"status": "error", "filename": f.filename, "message": str(e)}
        results.append(res)
    return {"results": results}


@app.get("/api/dashboard")
def dashboard():
    return db.dashboard_summary()


@app.get("/api/investments")
def investments():
    """Investment read-model (entities + quarterly roll-forward), ported schema."""
    return db.get_investments()


@app.get("/api/performance")
def performance_summary():
    """Direct Investment / Fund Investment summary tables + portfolio TVPI/DPI/IRR."""
    return {
        "direct_investments": performance.direct_investment_summary(),
        "operating_metrics": performance.operating_metrics_summary(),
        "funds": performance.fund_summary(),
        "portfolio": performance.portfolio_performance(),
    }


@app.get("/api/ledger")
def ledger():
    """The grand ledger — all trusted facts across companies/periods, consolidated."""
    rows = db.grand_ledger()
    return {"rows": rows, "n": len(rows)}


@app.get("/api/timeseries")
def timeseries():
    """Portfolio value over time (Total + per-entity series)."""
    return db.timeseries()


@app.get("/api/fabric/schema")
def fabric_schema():
    """Machine-readable export contract (tables, columns, types, grain)."""
    return fabric_export.schema_manifest()


@app.get("/api/fabric/{table}.csv")
def fabric_table_csv(table: str):
    """Fabric-ready table as CSV. table = fact_financial_metric | dim_entity."""
    if table not in (fabric_export.FACT_TABLE, fabric_export.DIM_TABLE):
        raise HTTPException(404, f"unknown table '{table}'")
    body = fabric_export.to_csv(table)
    return StreamingResponse(
        iter([body]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={table}.csv"})


@app.get("/api/fabric/{table}.parquet")
def fabric_table_parquet(table: str):
    """Fabric-ready table as Parquet (typed; decimal money preserved)."""
    if table not in (fabric_export.FACT_TABLE, fabric_export.DIM_TABLE):
        raise HTTPException(404, f"unknown table '{table}'")
    try:
        body = fabric_export.to_parquet(table)
    except ImportError:
        raise HTTPException(
            501, "Parquet export needs pyarrow — pip install 'pyarrow>=15' "
                 "(or use the .csv endpoint)")
    return StreamingResponse(
        iter([body]), media_type="application/vnd.apache.parquet",
        headers={"Content-Disposition": f"attachment; filename={table}.parquet"})


@app.get("/api/ledger.csv")
def ledger_csv():
    rows = db.grand_ledger()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["period", "entity_name", "entity_kind", "report_type",
                "metric_key", "metric_label", "value", "unit", "currency", "source"])
    for r in rows:
        w.writerow([r["period"], r["entity_name"], r["entity_kind"], r["report_type"],
                    r["metric_key"], r["metric_label"], r["value"], r["unit"],
                    r["currency"], r["source"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=grand_ledger.csv"})


@app.get("/api/submissions")
def submissions():
    return {"submissions": db.list_submissions()}


@app.get("/api/submissions/{sub_id}")
def submission_detail(sub_id: int):
    sub = db.get_submission(sub_id)
    if not sub:
        raise HTTPException(404, "submission not found")
    return {
        "submission": sub,
        "facts": db.get_facts(sub_id),
        "audit": db.get_audit(sub_id, limit=200),
        "memory": _memory_summary(sub["entity_name"], sub["report_type"]),
        "report_type_metrics": [
            {"key": m.key, "label": m.label, "unit": m.unit.value,
             "category": m.category.value, "category_label": CATEGORY_LABELS[m.category]}
            for m in REPORT_TYPES.get(sub["report_type"],
                                      REPORT_TYPES["fund_financials"]).metrics],
        "category_order": [c.value for c in CATEGORY_ORDER],
    }


# --------------------------------------------------------------------------
# Format memory: statement wording (preset / taught / learned) and per-fund
# parameters — see app/terms.py
# --------------------------------------------------------------------------
_BALANCE_KEYS = ("beginning_balance", "ending_balance", "commitment", "remaining_commitment")


def _memory_summary(entity: Optional[str], report_type: str) -> Optional[dict]:
    if not entity or entity == "UNKNOWN":
        return None
    mem = terms.fund_memory(entity, report_type)
    return {"entity_name": entity, "quarters": mem["quarters"], "columns": mem["columns"],
            "currency": mem["currency"], "n_learned": len(mem["learned"]),
            "n_taught": len(mem["taught"]),
            "profile": {"investor_aliases": mem["profile"].get("investor_aliases") or "",
                        "notes": mem["profile"].get("notes") or ""}}


def _metric_choices(report_type: str) -> list[dict]:
    rt = REPORT_TYPES.get(report_type, REPORT_TYPES["fund_financials"])
    return [{"key": m.key, "label": m.label} for m in rt.metrics]


@app.get("/api/terms")
def list_terms(report_type: str = carry_forward.RF):
    if report_type not in REPORT_TYPES:
        raise HTTPException(400, "unknown report type")
    return {"report_type": report_type, "presets": terms.presets(report_type),
            "taught": terms.taught(report_type), "metrics": _metric_choices(report_type)}


@app.post("/api/terms")
async def add_term(request: Request):
    body = await request.json()
    rt = body.get("report_type") or carry_forward.RF
    metric = body.get("metric_key", "")
    if rt not in REPORT_TYPES or not (metric == terms.IGNORE
                                      or REPORT_TYPES[rt].metric_by_key(metric)):
        raise HTTPException(400, "unknown report type or field")
    label = str(body.get("label", "")).strip()
    if not label:
        raise HTTPException(400, "label is required")
    term_id = terms.add_term(rt, label, metric, section=str(body.get("section") or "").strip(),
                             entity_name=(body.get("entity_name") or None),
                             note=str(body.get("note") or ""),
                             user=auth.current_user(request) or "local")
    return {"status": "ok", "id": term_id}


@app.delete("/api/terms/{term_id}")
def delete_term(term_id: int, request: Request):
    if not terms.delete_term(term_id, user=auth.current_user(request) or "local"):
        raise HTTPException(404, "term not found")
    return {"status": "ok"}


@app.get("/api/funds/{entity}/memory")
def fund_memory(entity: str, report_type: str = carry_forward.RF):
    mem = terms.fund_memory(entity, report_type)
    if mem["last_quarter"]:
        mem["last_quarter"]["values"] = {k: str(v) for k, v in mem["last_quarter"]["values"].items()}
    return mem


@app.post("/api/funds/{entity}/profile")
async def set_fund_profile(entity: str, request: Request):
    body = await request.json()
    return terms.set_fund_profile(entity, investor_aliases=str(body.get("investor_aliases") or ""),
                                  notes=str(body.get("notes") or ""),
                                  user=auth.current_user(request) or "local")


@app.post("/api/submissions/{sub_id}/teach")
async def teach_line(sub_id: int, request: Request):
    """A reviewer maps a line the app didn't recognise: remember the wording
    (for this fund or every fund) and record the value now."""
    from decimal import Decimal

    from .extractors.base import is_quarter_label, previous_quarter
    from .models import parse_number

    body = await request.json()
    sub = db.get_submission(sub_id)
    if not sub:
        raise HTTPException(404, "submission not found")
    rt_key = sub["report_type"]
    rt = REPORT_TYPES.get(rt_key)
    metric = body.get("metric_key", "")
    spec = rt.metric_by_key(metric) if rt else None
    if metric != terms.IGNORE and spec is None:
        raise HTTPException(400, "unknown field for this report type")
    label = str(body.get("label", "")).strip()
    value_raw = str(body.get("value_raw", "")).strip()
    section = str(body.get("section") or "").strip()
    entity = sub["entity_name"]
    scope_fund = body.get("scope", "fund") == "fund"
    if scope_fund and (not entity or entity == "UNKNOWN"):
        raise HTTPException(400, "this file has no fund identified — teach it for all funds")
    user = auth.current_user(request) or "local"

    value = None
    if metric != terms.IGNORE:
        dashes = {"-", "–", "—"}
        value = (Decimal(0) if value_raw in dashes
                 else parse_number(value_raw, sub.get("number_format") == "1.234,56")[0])
        if value is None:
            raise HTTPException(400, f"can't read a number from '{value_raw}'")

    term_id = terms.add_term(rt_key, label, metric, section=section,
                             entity_name=entity if scope_fund else None,
                             note=f"taught from submission #{sub_id}", user=user)
    fact_id = None
    if metric != terms.IGNORE:
        existing = [f for f in db.get_facts(sub_id)
                    if f["metric_key"] == metric and f["status"] != "rejected"]
        note = f"taught line '{label}'"
        if existing and (metric in _BALANCE_KEYS or spec.since_inception):
            fact_id = existing[0]["id"]
            db.update_fact(fact_id, value=str(value), status="confirmed", note=note, user=user)
            db.set_fact_source(fact_id, raw_label=label, raw_value=value_raw, section=section)
        elif existing:
            fact_id = existing[0]["id"]
            db.extend_fact(fact_id, add_value=value, label=label, value_raw=value_raw,
                           section=section, user=user, note=note)
        else:
            facts_ = db.get_facts(sub_id)
            base = facts_[0] if facts_ else {
                "entity_name": entity, "entity_kind": rt.entity_kind, "report_type": rt_key,
                "period": sub["period"], "currency": sub.get("currency") or "USD"}
            fact_id = db.add_fact(sub_id, base, metric_key=metric, metric_label=spec.label,
                                  value=value, flags=["taught"], provenance=f"taught by {user}",
                                  note=note, raw_label=label, raw_value=value_raw,
                                  raw_sections=[section], status="confirmed",
                                  action="teach", user=user)
    db.remove_unmapped(sub_id, label, value_raw)
    if rt_key == carry_forward.RF and is_quarter_label(sub["period"]):
        carry_forward.rederive_following(entity, previous_quarter(sub["period"]))
    return {"status": "ok", "term_id": term_id, "fact_id": fact_id}


@app.post("/api/facts/{fact_id}")
async def edit_fact(fact_id: int, request: Request):
    body = await request.json()
    try:
        db.update_fact(
            fact_id,
            value=body.get("value"),
            metric_key=body.get("metric_key"),
            status=body.get("status"),
            note=body.get("note", ""),
        )
    except KeyError:
        raise HTTPException(404, "fact not found")
    # an edited/confirmed quarter re-anchors the quarters that follow it
    fact = db.get_fact(fact_id)
    if fact and fact["report_type"] == carry_forward.RF:
        carry_forward.rederive_following(fact["entity_name"], fact["period"])
    return {"status": "ok"}


@app.post("/api/submissions/{sub_id}/confirm")
def confirm_submission(sub_id: int):
    # confirm any still-auto facts and mark submission reviewed
    for f in db.get_facts(sub_id):
        if f["status"] == "auto":
            db.update_fact(f["id"], status="confirmed", note="bulk confirm on review")
    db.mark_submission_reviewed(sub_id)
    sub = db.get_submission(sub_id)
    if sub and sub["report_type"] == carry_forward.RF:
        carry_forward.rederive_following(sub["entity_name"], sub["period"])
    return {"status": "ok"}


@app.post("/api/submissions/{sub_id}/period")
async def set_period(sub_id: int, request: Request):
    """Manually correct a submission's reporting period ("2026-Q2" or an ISO
    date). Roll-forwards are re-derived so quarters still follow on."""
    import re
    from datetime import date

    from .extractors.base import is_quarter_label, previous_quarter

    period = str((await request.json()).get("period", "")).strip().upper()
    ok = is_quarter_label(period)
    if not ok and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", period):
        try:
            date.fromisoformat(period)
            ok = True
        except ValueError:
            pass
    if not ok:
        raise HTTPException(400, "Period must be a calendar quarter like 2026-Q2 "
                                 "or a date like 2026-06-30.")
    sub = db.get_submission(sub_id)
    if not sub:
        raise HTTPException(404, "submission not found")
    old = db.set_submission_period(sub_id, period,
                                   user=auth.current_user(request) or "local")
    if sub["report_type"] == carry_forward.RF:
        # the moved quarter follows on from its new predecessor, and whatever
        # followed its old slot re-anchors too
        if is_quarter_label(period):
            carry_forward.rederive_following(sub["entity_name"], previous_quarter(period))
        if is_quarter_label(old):
            carry_forward.rederive_following(sub["entity_name"], old)
    return {"status": "ok", "old_period": old, "period": period,
            "fiscal_label": db.fiscal_info(period)["fiscal_label"]}


@app.post("/api/admin/clear")
async def clear_data(request: Request):
    """Empty the database (submissions, facts, investment views). Requires the
    body {"confirm": "CLEAR"} so it can't be triggered by a stray request."""
    body = await request.json()
    if body.get("confirm") != "CLEAR":
        raise HTTPException(400, 'Send {"confirm": "CLEAR"} to clear all data.')
    return {"status": "ok",
            **db.clear_data(user=auth.current_user(request) or "local")}


@app.get("/api/audit")
def audit(limit: int = 300):
    return {"audit": db.get_audit(limit=limit)}


@app.get("/api/export.csv")
def export_csv():
    facts = db.trusted_facts()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["entity_name", "entity_kind", "report_type", "period",
                "metric_key", "metric_label", "value", "unit", "currency",
                "status", "provenance"])
    for f in facts:
        w.writerow([f["entity_name"], f["entity_kind"], f["report_type"],
                    f["period"], f["metric_key"], f["metric_label"], f["value"],
                    f["unit"], f["currency"], f["status"], f["provenance"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trusted_facts.csv"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
