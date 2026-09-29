"""FastAPI application — dashboard, upload, human-in-the-loop review, export.

Bind is localhost-only by default (see config.host). The server makes no outbound
calls in the default deterministic mode; the only outbound traffic is to the
configured LLM provider (Azure OpenAI / OpenAI / Ollama), and only when the LLM
fallback is triggered for a non-standard layout.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .config import settings
from .llm import get_provider
from .pipeline import process_upload
from .report_types import REPORT_TYPES, all_report_types

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="Portfolio Financials Studio", version="1.0.0")
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
    return templates.TemplateResponse("index.html", {
        "request": request,
        "report_types": all_report_types(),
        "llm_provider": settings.llm_provider,
        "llm_enabled": settings.llm_enabled,
    })


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
