"""Pipeline orchestrator: ingest a file end-to-end.

    file  ->  extract  ->  carry-forward  ->  validate  ->  persist (+ audit)  ->  summary

Returns a JSON-able summary the API/dashboard renders. Deterministic and
side-effect-contained: the only writes are the saved copy under data/uploads
and rows in the SQLite DB.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

from . import carry_forward, db, storage
from .config import settings
from .extractors.registry import extract_file
from .validate import validate_facts


def process_path(path: Path, *, save_copy: bool = True,
                 allow_duplicate: bool = False,
                 display_name: Optional[str] = None,
                 source_key: Optional[str] = None) -> dict[str, Any]:
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

    # roll-forwards: follow on from the prior quarter (carry forward its ending,
    # de-cumulate YTD statements, reconcile) BEFORE anything is validated/saved
    carry_forward.apply_to_result(result)

    # run validation (mutates fact flags/status in place)
    checks = validate_facts(result.detected_report_type, result.facts)

    # optionally keep an immutable copy of the source (local dir or Azure Blob)
    if save_copy:
        try:
            source_key = storage.store_source(path.read_bytes(), path.name) or source_key
        except Exception:
            pass  # copy is best-effort; extraction already succeeded

    sub_id = db.save_extraction(result, source_key=source_key)
    _after_save(result, sub_id)

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


def _after_save(result, sub_id: int) -> None:
    if result.detected_report_type != carry_forward.RF:
        return
    # the investor's own statement replaces figures computed pro-rata from a
    # fund-level report for the same quarter
    if result.facts and not any("pro_rata" in f.flags for f in result.facts):
        db.supersede_pro_rata(result.entity_name, carry_forward.RF, result.period, sub_id)
    # an earlier quarter arriving late re-anchors the quarters already on file
    carry_forward.rederive_following(result.entity_name, result.period)


def reprocess_submission(sub_id: int, user: str = "local") -> dict[str, Any]:
    """Re-read a stored file with everything the app knows now (taught terms,
    the fund's memory and parameters, the current model). Reviewed values stay."""
    sub = db.get_submission(sub_id)
    if not sub:
        raise KeyError(f"submission {sub_id} not found")
    path = storage.load_source(sub.get("source_key"), sub["filename"], sub["file_hash"])
    if path is None:
        raise FileNotFoundError("the stored copy of this file wasn't found — upload it again")
    result = extract_file(path, original_name=sub["filename"])
    carry_forward.apply_to_result(result)
    checks = validate_facts(result.detected_report_type, result.facts)
    counts = db.replace_extraction(sub_id, result, user=user)
    _after_save(result, sub_id)
    return {"status": "ok", "submission_id": sub_id, "report_type": result.detected_report_type,
            "entity_name": result.entity_name, "period": result.period,
            "extractor": result.extractor, **counts,
            "checks": [c.to_dict() for c in checks], "warnings": result.warnings}


def process_upload(filename: str, raw_bytes: bytes,
                   allow_duplicate: bool = False) -> dict[str, Any]:
    """Process bytes received from the web upload endpoint."""
    settings.ensure_dirs()
    safe_name = Path(filename).name
    # keep the canonical immutable copy (local dir or Azure Blob)
    source_key = storage.store_source(raw_bytes, safe_name)
    # extractors need a path with the right suffix; use a short-lived temp file
    suffix = Path(safe_name).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False,
                                     dir=settings.processed_dir) as tf:
        tf.write(raw_bytes)
        tmp = Path(tf.name)
    try:
        return process_path(tmp, save_copy=False, allow_duplicate=allow_duplicate,
                            display_name=safe_name, source_key=source_key or None)
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
