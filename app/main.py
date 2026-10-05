"""FastAPI application — dashboard, upload, human-in-the-loop review, export.

Bind is localhost-only by default (see config.host). The server makes no outbound
calls in the default deterministic mode; the only outbound traffic is to the
configured LLM provider (Azure OpenAI / OpenAI / Ollama), and only when the LLM
fallback is triggered for a non-standard layout.
"""
from __future__ import annotations

import csv
import io
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db
from .config import settings
from .llm import get_provider
from .pipeline import process_upload
from .report_types import REPORT_TYPES, all_report_types

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="Portfolio Financials Studio", version="1.0.0")
app.add_middleware(auth.AuthMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
    return {"status": "ok", "llm_enabled": settings.llm_enabled,
            "llm_provider": settings.llm_provider,
            "provider_status": get_provider().status()}


@app.get("/api/report-types")
def report_types():
    out = {}
    for rt in all_report_types():
        out[rt.key] = {
            "label": rt.label,
            "entity_kind": rt.entity_kind,
            "metrics": [{"key": m.key, "label": m.label, "unit": m.unit.value,
                         "required": m.required} for m in rt.metrics],
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


@app.get("/api/ledger")
def ledger():
    """The grand ledger — all trusted facts across companies/periods, consolidated."""
    rows = db.grand_ledger()
    return {"rows": rows, "n": len(rows)}


@app.get("/api/timeseries")
def timeseries():
    """Portfolio value over time (Total + per-entity series)."""
    return db.timeseries()


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
        "report_type_metrics": [
            {"key": m.key, "label": m.label, "unit": m.unit.value}
            for m in REPORT_TYPES.get(sub["report_type"],
                                      REPORT_TYPES["fund_financials"]).metrics],
    }


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
    return {"status": "ok"}


@app.post("/api/submissions/{sub_id}/confirm")
def confirm_submission(sub_id: int):
    # confirm any still-auto facts and mark submission reviewed
    for f in db.get_facts(sub_id):
        if f["status"] == "auto":
            db.update_fact(f["id"], status="confirmed", note="bulk confirm on review")
    db.mark_submission_reviewed(sub_id)
    return {"status": "ok"}


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
