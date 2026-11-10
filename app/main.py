"""FastAPI application — dashboard, upload, human-in-the-loop review, export.

Bind is localhost-only by default (see config.host). The server makes no outbound
calls in the default deterministic mode; the only outbound traffic is to the
configured LLM provider (Azure OpenAI / OpenAI / Ollama), and only when the LLM
fallback is triggered for a non-standard layout.
"""
from __future__ import annotations

import csv
import hashlib
import re
import io
import json
import secrets
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates

from . import (auth, carry_forward, db, direct_investments, fabric_export, lake, performance,
               portfolio, terms)
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
async def lake_follows_changes(request: Request, call_next):
    """Any successful change (upload, edit, confirm, teach, clear, ...) is
    followed by a background Silver/Gold sync of the lake."""
    response = await call_next(request)
    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.url.path.startswith("/api/") and response.status_code < 400
            and not request.url.path.startswith("/api/lake/")):
        lake.request_sync()
    return response


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if "v" in request.query_params
            else "no-cache")
    elif (response.headers.get("content-type", "").startswith("text/html")
          and "Cache-Control" not in response.headers):
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


@app.get("/portfolio-dashboard", response_class=HTMLResponse)
def portfolio_dashboard(request: Request):
    """Show the separately supplied workbook snapshot through the Studio's auth."""
    snapshot_path = settings.data_dir / "portfolio-dashboard.json"
    snapshot = None
    comparison = []
    charts = []
    source = None
    period = None
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        rows = snapshot["sections"][1]["rows"]
        categories = [("Core", rows[2]), ("Life Sciences", rows[1])]
        amounts = [
            [Decimal(value.replace("$", "").replace("M", "").replace(",", "")) for value in row[2:4]]
            for _, row in categories
        ]
        scale = max(Decimal(1), *(abs(value) for pair in amounts for value in pair))
        comparison = [
            (name, [
                (index, label, row[column], round(abs(amount) * 100 / scale, 2), False)
                for index, (label, column, amount) in enumerate(zip(
                    ("Paid-in capital", "Total value"), (2, 3), pair
                ))
            ])
            for (name, row), pair in zip(categories, amounts)
        ]
        direct = snapshot["sections"][0]["rows"]
        investments = [row for row in direct if not row[0].startswith("Total")]
        charts = [
            _snapshot_chart("Capital by investment type", "USD millions", investments,
                            [(2, "Investment cost")]),
            _snapshot_chart("Realized and unrealized gains", "USD millions", investments,
                            [(7, "Realized"), (8, "Unrealized")]),
            _snapshot_chart("Portfolio multiples by category", "Multiple of paid-in capital", rows[1:],
                            [(11, "TVPI"), (12, "DPI")]),
            _snapshot_chart("Capital allocation by category", "Percent of capital", rows[1:],
                            [(14, "Capital allocation")], maximum=Decimal(100), donut=True),
            _snapshot_chart("Impairment by category", "USD millions",
                            [row for _, row in categories], [(5, "Impairment")]),
        ]
        url = urlsplit(snapshot.get("source", ""))
        period = snapshot.get("period")
        if url.scheme == "https" and url.hostname:
            source = url.geturl()
            source_month, *_ = parse_qs(url.query).get("asOf", [""])
            if re.fullmatch(r"\d{4}-\d{2}", source_month):
                try:
                    period = date.fromisoformat(f"{source_month}-01").strftime("%B %Y")
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return templates.TemplateResponse(request, "portfolio_dashboard.html", {
        "snapshot": snapshot,
        "comparison": comparison,
        "charts": charts,
        "source": source,
        "period": period,
        "auth_mode": settings.auth_mode,
        "user": auth.current_user(request) or "",
    }, headers={"Cache-Control": "private, no-store"})


def _snapshot_chart(title, unit, rows, series, maximum=None, donut=False):
    """Keep the source labels and use numeric values only for chart geometry."""
    groups = []
    for row in rows:
        marks = []
        for index, (column, label) in enumerate(series):
            if column >= len(row):
                continue
            display = str(row[column])
            try:
                value = Decimal(display.translate(str.maketrans("", "", "$M,%x")))
            except InvalidOperation:
                continue
            if value.is_finite():
                marks.append((index, label, display, value))
        if marks:
            groups.append((row[0], marks))

    values = [mark[3] for _, marks in groups for mark in marks]
    signed = any(value < 0 for value in values)
    scale = max(Decimal(1), maximum or Decimal(0), *(abs(value) for value in values))
    return {
        "title": title,
        "unit": unit,
        "series": [label for _, label in series],
        "signed": signed,
        "donut_split": values[0] if donut and len(values) == 2 and not signed
        and sum(values) == 100 else None,
        "groups": [
            (name, [(index, label, display, round(abs(value) * (50 if signed else 100) / scale, 2),
                     value < 0) for index, label, display, value in marks])
            for name, marks in groups
        ],
    }


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
            # model calls take a while: keep them off the event loop so the rest
            # of the app stays responsive during an upload
            res = await run_in_threadpool(process_upload, f.filename, raw,
                                          allow_duplicate=allow_duplicate)
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


@app.get("/api/direct-investments")
def direct_investment_report(year: Optional[int] = None):
    """Lam's "Direct Investment Financials": Budget | Actual per company and quarter."""
    return direct_investments.report(year)


@app.post("/api/budgets")
async def set_budget(request: Request):
    """Enter (or clear, with an empty value) one budget figure for a company."""
    body = await request.json()
    entity = str(body.get("entity") or "").strip()
    period = str(body.get("period") or "").strip()
    metric = str(body.get("metric") or "").strip()
    if not entity:
        raise HTTPException(400, "entity is required")
    if not re.fullmatch(r"20\d{2}(?:-Q[1-4])?", period):
        raise HTTPException(400, "period must be a quarter (2026-Q1) or a year (2026)")
    if metric not in direct_investments.BASE:
        raise HTTPException(400, f"metric must be one of {', '.join(direct_investments.BASE)}")
    raw = str(body.get("value") if body.get("value") is not None else "").strip()
    value = None
    if raw:
        from .models import parse_number
        value = parse_number(raw)[0]
        if value is None:
            raise HTTPException(400, f"can't read a number from '{raw}'")
        value *= Decimal(str(body.get("scale") or 1))
    db.set_budget(entity, period, metric, value, user=auth.current_user(request) or "local")
    return {"ok": True, "entity": entity, "period": period, "metric": metric,
            "value": None if value is None else str(value)}


def _typed_value(raw, scale, spec) -> Optional[Decimal]:
    """A number a person typed ("1,250", "(45)", "2.4m"); money may be typed in
    $000 / $M (scale). Empty means "no value"."""
    from .models import parse_number
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    value = parse_number(text)[0]
    if value is None:
        raise HTTPException(400, f"can't read a number from '{text}'")
    if spec.unit.value == "currency":
        value *= Decimal(str(scale or 1))
    return value


@app.post("/api/manual-values")
async def enter_manual_value(request: Request):
    """Type in a figure that no statement on file shows — the file was empty,
    unreadable, or simply doesn't carry it. Kept as its own "Entered manually"
    submission for the entity and period, confirmed by the person who typed it,
    flagged manual_entry and audited. An empty value withdraws it."""
    body = await request.json()
    entity = str(body.get("entity") or "").strip()
    rt = REPORT_TYPES.get(str(body.get("report_type") or ""))
    period = str(body.get("period") or "").strip()
    if not entity or rt is None:
        raise HTTPException(400, "entity and a known report_type are required")
    spec = rt.metric_by_key(str(body.get("metric") or ""))
    if spec is None:
        raise HTTPException(400, f"metric must be one of {', '.join(m.key for m in rt.metrics)}")
    if not re.fullmatch(r"20\d{2}-Q[1-4]|20\d{2}-\d{2}-\d{2}", period):
        raise HTTPException(400, "period must be a quarter (2026-Q1) or a date (2026-03-31)")
    value = _typed_value(body.get("value"), body.get("scale"), spec)
    currency = str(body.get("currency") or "USD").upper()[:3]
    user = auth.current_user(request) or "local"
    sid = db.manual_submission(entity, rt.key, period, currency)
    fid = db.set_manual_value(sid, metric_key=spec.key, metric_label=spec.label,
                              unit=spec.unit.value, value=value, currency=currency,
                              entity_kind=rt.entity_kind, user=user,
                              note=str(body.get("note") or "").strip()[:500])
    if rt.key == carry_forward.RF:
        carry_forward.rederive_following(entity, period)
    return {"submission_id": sid, "fact_id": fid,
            "value": None if value is None else str(value)}


@app.post("/api/submissions/{sub_id}/facts")
async def add_value_to_submission(sub_id: int, request: Request):
    """While reviewing a file: add a value it doesn't show (or shows unreadably),
    for the file's entity and period. Confirmed, flagged manual_entry, audited."""
    sub = db.get_submission(sub_id)
    if sub is None:
        raise HTTPException(404, "submission not found")
    body = await request.json()
    rt = REPORT_TYPES.get(sub["report_type"])
    spec = rt.metric_by_key(str(body.get("metric") or "")) if rt else None
    if spec is None:
        raise HTTPException(400, "metric must be one of this report type's metrics")
    value = _typed_value(body.get("value"), body.get("scale"), spec)
    if value is None:
        raise HTTPException(400, "a value is required")
    fid = db.set_manual_value(
        sub_id, metric_key=spec.key, metric_label=spec.label, unit=spec.unit.value, value=value,
        currency=str(body.get("currency") or sub.get("currency") or "USD").upper()[:3],
        entity_kind=rt.entity_kind, user=auth.current_user(request) or "local",
        note=f"added while reviewing {sub['filename']}")
    if rt.key == carry_forward.RF:
        carry_forward.rederive_following(sub["entity_name"], sub["period"])
    return {"fact_id": fid, "value": str(value)}


@app.get("/api/exports/direct-investment-financials.xlsx")
def export_direct_investments(year: Optional[int] = None, quarter: Optional[int] = None):
    """The "Direct Investment Financials Qn YYYY" workbook in Lam's layout."""
    if quarter is not None and quarter not in (1, 2, 3, 4):
        raise HTTPException(400, "quarter must be 1-4")
    data, name = direct_investments.workbook(year, quarter)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/portfolio")
def portfolio_report(as_of: Optional[str] = None):
    """Lam's "Investment Portfolio": funds and direct investments as of a quarter."""
    if as_of and not re.fullmatch(r"20\d{2}-Q[1-4]", as_of):
        raise HTTPException(400, "as_of must be a quarter, e.g. 2026-Q2")
    return portfolio.report(as_of)


def _parse_date(raw: str) -> Optional[str]:
    """"3/15/2019", "2019-03-15", "15 Mar 2019" -> "2019-03-15"."""
    from .extractors.base import normalize_period
    raw = (raw or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", raw)
    if m:
        y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        try:
            from datetime import date
            return date(y, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    p = normalize_period(raw)
    return p if p and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p) else None


@app.get("/api/portfolio/summary")
def portfolio_summary(as_of: Optional[str] = None):
    """Cost, value, book value, impairment, proceeds, gains, TVPI, DPI and AUM —
    direct investments by status, funds, and all investments by group."""
    if as_of and not re.fullmatch(r"20\d{2}-Q[1-4]", as_of):
        raise HTTPException(400, "as_of must be a quarter, e.g. 2026-Q2")
    return portfolio.summary(as_of)


@app.post("/api/portfolio/{entity}/details")
async def set_portfolio_details(entity: str, request: Request):
    """Master data for a fund or direct investment (type, vintage, fund size,
    region / sector, accounting details, theme, round, status, exit...)."""
    body = await request.json()
    user = auth.current_user(request) or "local"
    fields = {k: t for k, _l, t in portfolio.FUND_FIELDS + portfolio.COMPANY_FIELDS}
    values: dict = {}
    for key, raw in body.items():
        if key == "kind":
            if raw in (None, ""):
                values["kind"] = None          # a hand-added entry leaves the report
                continue
            if raw not in ("fund", "direct_investment"):
                raise HTTPException(400, "kind must be fund or direct_investment")
            values["kind"] = raw
            continue
        if key == "commitment":
            continue
        if key not in fields:
            raise HTTPException(400, f"unknown field '{key}'")
        text = str(raw if raw is not None else "").strip()
        kind = fields[key]
        if not text:
            values[key] = None
        elif kind in ("money", "int", "percent", "year", "rate"):
            from .models import parse_number
            v = parse_number(text)[0]
            if v is None:
                raise HTTPException(400, f"{key}: can't read a number from '{text}'")
            values[key] = str(int(v)) if kind in ("int", "year") else str(v)
        elif kind == "date":
            iso = _parse_date(text)
            if iso is None:
                raise HTTPException(400, f"{key}: can't read a date from '{text}'")
            values[key] = iso
        elif kind.startswith("choice:"):
            if text not in kind[7:].split(","):
                raise HTTPException(400, f"{key} must be one of {kind[7:]}")
            values[key] = text
        else:
            values[key] = text[:2000]
    details = db.set_details(entity, values, user=user)
    if "commitment" in body:
        commitment = str(body.get("commitment") or "").strip()
        from .models import parse_number
        if commitment and parse_number(commitment)[0] is None:
            raise HTTPException(400, f"can't read a number from '{commitment}'")
        prof = terms.fund_profile(entity)
        terms.set_fund_profile(entity, investor_aliases=prof.get("investor_aliases") or "",
                               notes=prof.get("notes") or "", commitment=commitment, user=user)
    return {"entity": entity, "details": details}


@app.post("/api/portfolio/{entity}/calls")
async def add_portfolio_calls(entity: str, request: Request):
    """Add dated capital calls / investments: one {date, description, amount}, or
    pasted lines "date <tab> description <tab> amount" (scale 1000000 for $M)."""
    from .models import parse_number
    body = await request.json()
    user = auth.current_user(request) or "local"
    scale = Decimal(str(body.get("scale") or 1))
    lines = ([str(body["lines"])] if body.get("lines") else [])
    rows = []
    if lines:
        for ln in lines[0].splitlines():
            if not ln.strip():
                continue
            cells = [c.strip() for c in re.split(r"\t|\s*;\s*|\s{2,}", ln.strip()) if c.strip()]
            date_iso = _parse_date(cells[0]) if cells else None
            amount = parse_number(cells[-1])[0] if len(cells) >= 2 else None
            if date_iso is None or amount is None:
                raise HTTPException(400, f"can't read 'date, description, amount' from: {ln!r}")
            rows.append((date_iso, " ".join(cells[1:-1]).strip(" -") , amount))
    else:
        date_iso = _parse_date(str(body.get("date") or ""))
        amount = parse_number(str(body.get("amount") or ""))[0]
        if date_iso is None or amount is None:
            raise HTTPException(400, "date and amount are required")
        rows.append((date_iso, str(body.get("description") or ""), amount))
    ids = [db.add_capital_call(entity, d, abs(a) * scale, desc, user=user) for d, desc, a in rows]
    return {"added": len(ids), "ids": ids}


@app.delete("/api/portfolio/calls/{call_id}")
def delete_portfolio_call(call_id: int, request: Request):
    if not db.delete_capital_call(call_id, user=auth.current_user(request) or "local"):
        raise HTTPException(404, "call not found")
    return {"ok": True}


@app.get("/api/exports/investment-portfolio.xlsx")
def export_portfolio(as_of: Optional[str] = None):
    """The "Investment Portfolio <Mon'YY>" workbook in Lam's layout."""
    if as_of and not re.fullmatch(r"20\d{2}-Q[1-4]", as_of):
        raise HTTPException(400, "as_of must be a quarter, e.g. 2026-Q2")
    data, name = portfolio.workbook(as_of)
    from urllib.parse import quote
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"})


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
                        "commitment": mem["profile"].get("commitment") or "",
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
    commitment = str(body.get("commitment") or "").strip()
    if commitment:
        from .models import parse_number
        if parse_number(commitment)[0] is None:
            raise HTTPException(400, f"can't read a number from '{commitment}'")
    return terms.set_fund_profile(entity, investor_aliases=str(body.get("investor_aliases") or ""),
                                  notes=str(body.get("notes") or ""), commitment=commitment,
                                  user=auth.current_user(request) or "local")


@app.post("/api/submissions/{sub_id}/reread")
def reread_submission(sub_id: int, request: Request):
    """Re-read a stored file with what the app knows now (taught terms, the
    fund's memory and parameters, the current model). Reviewed values stay."""
    from .pipeline import reprocess_submission
    try:
        return reprocess_submission(sub_id, user=auth.current_user(request) or "local")
    except KeyError:
        raise HTTPException(404, "submission not found")
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))


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
    currency = body.get("currency")
    if currency is not None:
        currency = _currency_code(currency)
    try:
        db.update_fact(
            fact_id,
            value=body.get("value"),
            metric_key=body.get("metric_key"),
            status=body.get("status"),
            currency=currency,
            note=body.get("note", ""),
            user=auth.current_user(request) or "local",
        )
    except KeyError:
        raise HTTPException(404, "fact not found")
    # an edited/confirmed quarter re-anchors the quarters that follow it
    fact = db.get_fact(fact_id)
    if fact and fact["report_type"] == carry_forward.RF:
        carry_forward.rederive_following(fact["entity_name"], fact["period"])
    return {"status": "ok"}


def _currency_code(raw) -> str:
    code = str(raw or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise HTTPException(400, "currency must be a 3-letter code such as EUR or USD")
    return code


@app.post("/api/submissions/{sub_id}/currency")
async def set_submission_currency(sub_id: int, request: Request):
    """Set a file's currency and every money value in it."""
    code = _currency_code((await request.json()).get("currency"))
    try:
        n = db.set_submission_currency(sub_id, code, user=auth.current_user(request) or "local")
    except KeyError:
        raise HTTPException(404, "submission not found")
    return {"status": "ok", "currency": code, "changed": n}


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


@app.get("/api/lake/status")
def lake_status():
    """Where the lake is and what was last written to each layer."""
    return lake.status()


@app.post("/api/lake/sync")
def lake_sync():
    """Write Silver and Gold now (normally automatic after each change)."""
    if not lake.enabled():
        raise HTTPException(409, "no lake configured (LAKE_BACKEND)")
    return lake.sync()


@app.post("/api/lake/backfill")
def lake_backfill():
    """Land files uploaded before the lake was switched on, then sync."""
    if not lake.enabled():
        raise HTTPException(409, "no lake configured (LAKE_BACKEND)")
    counts = lake.backfill()
    return {**counts, "status": lake.sync()}


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
