"""File routing + assembly of a canonical ExtractionResult.

Flow per file:
  route by extension -> deterministic extract -> detect (type/entity/period/currency)
  -> map raw labels to canonical metrics -> fill gaps with local LLM (flagged) ->
  assign confidence + auto/needs_review status.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..config import settings
from ..models import (ExtractionResult, FactStatus, FinancialFact, RawItem,
                      detect_currency, map_label_to_metric, parse_number)
from ..report_types import REPORT_TYPES, Unit, guess_report_type
from ..llm import extract_with_llm, get_provider
from .base import (classify_document, file_sha256, guess_entity_name,
                   normalize_period)
from .excel import extract_excel
from .pdf_native import ScannedPdfError, extract_pdf

EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}
PDF_EXT = {".pdf"}


def _canonical_value(metric_unit: Unit, value: Optional[Decimal],
                     parse_notes: list[str]) -> tuple[Optional[Decimal], list[str]]:
    """Normalize a parsed value against the metric's unit and return flags."""
    flags: list[str] = []
    if value is None:
        return None, ["no_value"]
    # a percent metric read as "45" vs "0.45": if value looks like a fraction but
    # metric expects percent, flag rather than silently transform.
    if metric_unit == Unit.PERCENT and "percent" not in parse_notes and 0 < value < 1:
        flags.append("percent_scale_uncertain")
    return value, flags


def assemble(raw_items: list[RawItem], text_blob: str, filename: str,
             file_hash: str, extractor: str) -> ExtractionResult:
    rt_key, rt_conf = guess_report_type(text_blob, filename)
    rt = REPORT_TYPES[rt_key]

    lines = [ln for ln in text_blob.splitlines() if ln.strip()]
    # Filename classification (Fund N / Company X / Note X + document type) gives a
    # canonical entity name; prefer it, falling back to a text-derived guess.
    classified = classify_document(filename)
    entity = classified["entity_name"] or guess_entity_name(lines, filename)
    period = normalize_period(text_blob) or normalize_period(filename) or "UNKNOWN"
    currency = detect_currency(text_blob) or "USD"

    # --- map raw items -> best candidate per metric ---
    candidates: dict[str, list[tuple[float, RawItem]]] = {}
    unmapped: list[RawItem] = []
    for item in raw_items:
        mkey, mconf = map_label_to_metric(rt_key, item.label)
        if mkey and mconf >= 0.7:
            candidates.setdefault(mkey, []).append((mconf, item))
        else:
            unmapped.append(item)

    facts: list[FinancialFact] = []
    warnings: list[str] = []

    # In multi-sheet workbooks the same label can appear on several sheets (e.g. a
    # per-entity capital-account sheet AND a fund-level summary with far larger
    # numbers). Identify the "primary" statement sheet — the one contributing the
    # most distinct metrics — and prefer its candidates so we don't pull a value
    # off the wrong sheet.
    def _sheet_of(item: RawItem) -> str:
        p = item.provenance or ""
        return p.split("!", 1)[0] if "!" in p else ""

    sheet_metric_counts: dict[str, set] = {}
    for mkey, cands in candidates.items():
        for _c, item in cands:
            sh = _sheet_of(item)
            if sh:
                sheet_metric_counts.setdefault(sh, set()).add(mkey)
    primary_sheet = (max(sheet_metric_counts, key=lambda s: len(sheet_metric_counts[s]))
                     if sheet_metric_counts else "")

    for mkey, cands in candidates.items():
        spec = rt.metric_by_key(mkey)
        # choose best: prefer the primary sheet, then higher mapping confidence,
        # then last (right-most / most recent column).
        cands_sorted = sorted(
            cands, key=lambda c: (_sheet_of(c[1]) == primary_sheet, c[0]))
        mconf, item = cands_sorted[-1]
        value, notes = parse_number(item.value_raw)
        value, unit_flags = _canonical_value(spec.unit, value, notes)

        flags = list(unit_flags)
        # Only conflicting values ON THE CHOSEN SHEET should nag for review — a
        # duplicate label on a different sheet isn't a genuine ambiguity here.
        chosen_sheet = _sheet_of(item)
        same_sheet = [c for c in cands if _sheet_of(c[1]) == chosen_sheet] or cands
        distinct_vals = {parse_number(c[1].value_raw)[0] for c in same_sheet}
        distinct_vals.discard(None)
        if len(distinct_vals) > 1:
            flags.append(f"multiple_candidates({len(distinct_vals)})")
        # note (informational) when the same label also appeared on other sheets
        other_sheets = {_sheet_of(c[1]) for c in cands} - {chosen_sheet, ""}
        if other_sheets:
            flags.append(f"also_on_other_sheets({len(other_sheets)})")

        confidence = mconf if value is not None else 0.0
        status = (FactStatus.AUTO
                  if confidence >= settings.auto_accept_confidence and not flags
                  else FactStatus.NEEDS_REVIEW)

        facts.append(FinancialFact(
            entity_name=entity, entity_kind=rt.entity_kind, report_type=rt_key,
            period=period, metric_key=mkey, metric_label=spec.label,
            value=value, unit=spec.unit, currency=currency,
            raw_label=item.label, raw_value=item.value_raw,
            provenance=f"{extractor}:{item.provenance}",
            confidence=round(confidence, 3), status=status, flags=flags))

    # --- LLM fallback: fill required metrics we couldn't map deterministically ---
    have = {f.metric_key for f in facts}
    missing_required = [m for m in rt.metrics if m.required and m.key not in have]
    used_llm = False
    if missing_required and settings.llm_enabled:
        llm = extract_with_llm(text_blob, rt_key)
        if llm.get("_error"):
            warnings.append(f"LLM fallback unavailable: {llm['_error']}")
        elif llm.get("fields"):
            used_llm = True
            if entity == "UNKNOWN" and llm.get("entity_name"):
                entity = str(llm["entity_name"])
            if period == "UNKNOWN" and llm.get("period"):
                period = normalize_period(str(llm["period"])) or period
            for mkey, payload in llm["fields"].items():
                if mkey in have:
                    continue
                spec = rt.metric_by_key(mkey)
                value, notes = parse_number(payload.get("value", ""))
                value, unit_flags = _canonical_value(spec.unit, value, notes)
                facts.append(FinancialFact(
                    entity_name=entity, entity_kind=rt.entity_kind,
                    report_type=rt_key, period=period, metric_key=mkey,
                    metric_label=spec.label, value=value, unit=spec.unit,
                    currency=currency, raw_label=payload.get("source_label", ""),
                    raw_value=str(payload.get("value", "")),
                    provenance=f"llm:{settings.llm_provider}",
                    confidence=0.5,  # LLM output is a suggestion
                    status=FactStatus.NEEDS_REVIEW,  # ALWAYS reviewed
                    flags=["llm_suggested"] + unit_flags))
    if used_llm:
        warnings.append(f"Some values came from the '{settings.llm_provider}' model "
                        "and require confirmation against the source.")

    # propagate final entity/period onto facts (in case LLM improved them)
    for f in facts:
        if f.entity_name == "UNKNOWN":
            f.entity_name = entity
        if f.period == "UNKNOWN":
            f.period = period

    if period == "UNKNOWN":
        warnings.append("Reporting period could not be determined — please set it.")
    if rt_conf < 0.3:
        warnings.append(f"Report type detected as '{rt.label}' with low confidence "
                        "— please confirm.")

    return ExtractionResult(
        filename=filename, file_hash=file_hash, detected_report_type=rt_key,
        report_type_confidence=rt_conf, entity_name=entity, period=period,
        currency=currency, extractor=extractor, facts=facts,
        unmapped=unmapped[:200], warnings=warnings)


def extract_file(path: Path, original_name: Optional[str] = None) -> ExtractionResult:
    """Top-level entry: route a single file to the right extractor and assemble."""
    ext = path.suffix.lower()
    file_hash = file_sha256(path)
    name = original_name or path.name

    if ext in EXCEL_EXT:
        raw_items, text = extract_excel(path)
        return assemble(raw_items, text, name, file_hash, "excel")

    if ext in PDF_EXT:
        try:
            raw_items, text = extract_pdf(path)
        except ScannedPdfError as e:
            return ExtractionResult(
                filename=name, file_hash=file_hash,
                detected_report_type="fund_financials", report_type_confidence=0.0,
                extractor="pdf_native", facts=[],
                warnings=[f"SCANNED PDF: {e} No data extracted. "
                          "Add OCR (Tesseract/PaddleOCR) or provide a native PDF."])
        return assemble(raw_items, text, name, file_hash, "pdf_native")

    return ExtractionResult(
        filename=name, file_hash=file_hash,
        detected_report_type="fund_financials", report_type_confidence=0.0,
        extractor="none", facts=[],
        warnings=[f"Unsupported file type '{ext}'. Supported: PDF, XLSX, XLSM, XLS."])
