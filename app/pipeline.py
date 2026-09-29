"""Pipeline orchestrator: ingest a file end-to-end.

    file  ->  extract  ->  validate  ->  persist (+ audit)  ->  summary

Returns a JSON-able summary the API/dashboard renders. Deterministic and
side-effect-contained: the only writes are the saved copy under data/uploads
and rows in the SQLite DB.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import db
from .config import settings
from .extractors.registry import extract_file
from .validate import validate_facts


def process_path(path: Path, *, save_copy: bool = True,
                 allow_duplicate: bool = False,
                 display_name: Optional[str] = None) -> dict[str, Any]:
    """Process a file already on disk. Returns a summary dict."""
    result = extract_file(path, original_name=display_name)

    # duplicate guard (same bytes already ingested)
    existing = db.file_hash_exists(result.file_hash)
    if existing and not allow_duplicate:
        return {
            "status": "duplicate",
            "message": f"This exact file was already ingested as submission #{existing}.",
            "existing_submission_id": existing,
            "filename": result.filename,
        }

    # run validation (mutates fact flags/status in place)
    checks = validate_facts(result.detected_report_type, result.facts)

    # optionally keep an immutable copy of the source next to the DB
    if save_copy:
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            dest = settings.upload_dir / f"{ts}__{path.name}"
            if path.resolve() != dest.resolve():
                shutil.copy2(path, dest)
        except Exception:
            pass  # copy is best-effort; extraction already succeeded

    sub_id = db.save_extraction(result)

    n_auto = sum(1 for f in result.facts if f.status == "auto")
    n_review = sum(1 for f in result.facts if f.status == "needs_review")
    errors = [c.to_dict() for c in checks if not c.ok and c.severity == "error"]

    return {
        "status": "ok",
        "submission_id": sub_id,
        "filename": result.filename,
        "report_type": result.detected_report_type,
        "report_type_confidence": result.report_type_confidence,
        "entity_name": result.entity_name,
        "period": result.period,
        "currency": result.currency,
        "extractor": result.extractor,
        "n_facts": len(result.facts),
        "n_auto_accepted": n_auto,
        "n_needs_review": n_review,
        "n_unmapped": len(result.unmapped),
        "checks": [c.to_dict() for c in checks],
        "n_errors": len(errors),
        "warnings": result.warnings,
    }


def process_upload(filename: str, raw_bytes: bytes,
                   allow_duplicate: bool = False) -> dict[str, Any]:
    """Process bytes received from the web upload endpoint."""
    settings.ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_name = Path(filename).name
    tmp = settings.upload_dir / f"{ts}__{safe_name}"
    with open(tmp, "wb") as f:
        f.write(raw_bytes)
    # already saved the copy above; don't double-copy
    return process_path(tmp, save_copy=False, allow_duplicate=allow_duplicate,
                        display_name=safe_name)
