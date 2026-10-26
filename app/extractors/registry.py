"""File routing + assembly of a canonical ExtractionResult.

Flow per file:
  route by extension -> deterministic extract -> detect (type/entity/period/currency)
  -> map raw labels to canonical metrics -> fill gaps with local LLM (flagged) ->
  assign confidence + auto/needs_review status.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..config import settings
from ..models import (ExtractionResult, FactStatus, FinancialFact, RawItem,
                      detect_currency, detect_decimal_comma, label_key,
                      map_label_to_metric, parse_number)
from ..terms import IGNORE, TermIndex
from ..report_types import REPORT_TYPES, Unit, guess_report_type
from ..llm import extract_with_llm, get_provider
from ..llm.capital_account import read_capital_account, select_pages
from .base import (classify_document, detect_statement_dates, file_sha256,
                   guess_entity_name, normalize_period, quarter_bounds, quarter_label)
from .csv_native import extract_csv
from .excel import extract_excel
from .pdf_native import (ScannedPdfError, extract_pdf, parse_statement_lines,
                         pdf_pages, render_page_png)

EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}
PDF_EXT = {".pdf"}
CSV_EXT = {".csv"}


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
             file_hash: str, extractor: str, *, force_type: Optional[str] = None,
             allow_llm: bool = True) -> ExtractionResult:
    rt_key, rt_conf = ((force_type, 1.0) if force_type
                       else guess_report_type(text_blob, filename))
    rt = REPORT_TYPES[rt_key]
    dc = detect_decimal_comma(text_blob)   # "1.234.567,89" style document?

    lines = [ln for ln in text_blob.splitlines() if ln.strip()]
    # Filename classification (Fund N / Company X / Note X + document type) gives a
    # canonical entity name; prefer it, falling back to a text-derived guess.
    classified = classify_document(filename)
    entity = classified["entity_name"] or guess_entity_name(lines, filename)
    dates = detect_statement_dates(text_blob, filename)
    if dates.quarter and (rt_key == "investment_roll_forward" or dates.explicit_quarter):
        # roll-forwards (and any file that names its quarter) are recorded per
        # calendar quarter (Q2 = Apr 1–Jun 30), inferred from what the document
        # covers, not from its first date
        period = dates.quarter
    elif dates.statement_end:
        period = dates.statement_end.isoformat()
    else:
        period = normalize_period(text_blob) or normalize_period(filename) or "UNKNOWN"
    currency = detect_currency(text_blob) or "USD"

    # --- map raw items -> best candidate per metric ---
    # Known wording first (taught for this fund > this fund's confirmed history >
    # taught for all funds > presets), then the report type's generic synonyms.
    terms = TermIndex.for_entity(entity, rt_key)
    candidates: dict[str, list[tuple[float, RawItem]]] = {}
    term_source: dict[int, str] = {}           # id(item) -> where its wording is known from
    term_lines: dict[str, set] = {}            # metric -> distinct known lines feeding it
    unmapped: list[RawItem] = []
    for item in raw_items:
        if re.search(r"\d{1,3}(?:[.,]\d{3})+", item.label):
            # a "label" holding other figures is side-by-side statements read
            # across one text line — not a label/value pair we can trust
            unmapped.append(item)
            continue
        hit = terms.lookup(item.section, item.label)
        if hit and hit[0] == IGNORE:
            continue                           # a known line that isn't a quarter value
        if hit and rt.metric_by_key(hit[0]):
            mkey, mconf = hit[0], hit[1]
            term_source[id(item)] = hit[2]
            term_lines.setdefault(mkey, set()).add(
                (label_key(item.section), label_key(item.label)))
        else:
            mkey, mconf = map_label_to_metric(rt_key, item.label)
        spec = rt.metric_by_key(mkey) if mkey else None
        if spec and spec.unit == Unit.CURRENCY and (
                "%" in item.value_raw
                or re.search(r"\((?:dpi|tvpi|rvpi|moic|irr)\)|\bmultiple\b|\bratio\b",
                             item.label, re.I)):
            # "Ending capital account balance as % of total partners' capital 0.19%"
            # is not the ending balance
            mkey = None
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

    def _column_of(item: RawItem) -> str:
        """Sheet + column ("Fund 1!D") for cells, page for PDF text — component
        lines of one total sit in the same column as each other."""
        p = item.provenance or ""
        m = re.match(r"^(.*!)([A-Z]+)\d+", p)
        if m:
            return m.group(1) + m.group(2)
        m = re.search(r"col(\d+)", p)
        if m:
            return "col" + m.group(1)
        return p.split(" ")[0]

    sheet_metric_counts: dict[str, set] = {}
    for mkey, cands in candidates.items():
        for _c, item in cands:
            sh = _sheet_of(item)
            if sh:
                sheet_metric_counts.setdefault(sh, set()).add(mkey)
    primary_sheet = (max(sheet_metric_counts, key=lambda s: len(sheet_metric_counts[s]))
                     if sheet_metric_counts else "")

    # A single net-income line and its breakdown are alternatives, never both: when
    # known wording supplies the breakdown, a generic "net surplus"-style match goes.
    breakdown = ("investment_mtm", "net_realized_gain", "other", "accrued_interest")
    if "equity_income_loss" in candidates and any(
            id(it) in term_source for k in breakdown for _c, it in candidates.get(k, [])) \
            and not any(id(it) in term_source for _c, it in candidates["equity_income_loss"]):
        unmapped.extend(it for _c, it in candidates.pop("equity_income_loss"))

    for mkey, cands in candidates.items():
        spec = rt.metric_by_key(mkey)
        known = [c for c in cands if id(c[1]) in term_source]
        if known and not spec.aggregate:
            cands = known      # known wording beats a generic synonym for the same field
        # several known lines feed one ACTIVITY field ("Capital Loss Q1" + "Capital FX
        # Gain Q1"); balances, commitments and since-inception totals never add up
        flow = (spec.category.value in ("income", "distribution") and not spec.since_inception)
        aggregate = spec.aggregate or (flow and len(term_lines.get(mkey, ())) > 1)
        # choose best: prefer the primary sheet, then higher mapping confidence,
        # then last (right-most / most recent column).
        cands_sorted = sorted(
            cands, key=lambda c: (_sheet_of(c[1]) == primary_sheet, c[0]))
        mconf, item = cands_sorted[-1]
        value, notes = parse_number(item.value_raw, dc)
        if value is None and id(item) in term_source and item.value_raw.strip() in {"-", "–", "—"}:
            value, notes = Decimal(0), []   # a printed dash on a known line is nil
        value, unit_flags = _canonical_value(spec.unit, value, notes)
        raw_label, raw_value, provenance = item.label, item.value_raw, item.provenance
        raw_sections = [item.section]
        sources = {term_source[id(item)]} if id(item) in term_source else set()

        if aggregate:
            # sum each distinct component line on the chosen sheet/page once
            def _part_key(it: RawItem) -> tuple[str, str]:
                return it.section.strip().lower(), it.label.strip().lower()
            parts: dict[tuple[str, str], tuple[RawItem, Decimal]] = {}
            for _c, it in cands:
                v = parse_number(it.value_raw, dc)[0]
                if it.value_raw.strip() in {"-", "–", "—"}:
                    v = Decimal(0)
                if v is not None and _column_of(it) == _column_of(item):
                    parts.setdefault(_part_key(it), (it, v))
            if len(parts) > 1:
                value = sum((v for _it, v in parts.values()), Decimal(0))
                raw_label = " + ".join(it.label for it, _v in parts.values())
                raw_value = " + ".join(it.value_raw for it, _v in parts.values())
                provenance = "; ".join(it.provenance for it, _v in parts.values())
                raw_sections = [it.section for it, _v in parts.values()]
                sources = {term_source[id(it)] for it, _v in parts.values()
                           if id(it) in term_source}
                cands = [(c, it) for c, it in cands
                         if parts.get(_part_key(it), (None,))[0] is it]

        flags = list(unit_flags)
        ncols = re.search(r"cols=(\d+)", provenance or "")
        if ncols:
            flags.append(f"multi_column({ncols.group(1)})")
        # Only conflicting values ON THE CHOSEN SHEET should nag for review — a
        # duplicate label on a different sheet isn't a genuine ambiguity here.
        chosen_sheet = _sheet_of(item)
        same_sheet = [c for c in cands if _sheet_of(c[1]) == chosen_sheet] or cands
        distinct_vals = {parse_number(c[1].value_raw, dc)[0] for c in same_sheet}
        distinct_vals.discard(None)
        if len(distinct_vals) > 1 and not aggregate:
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
            raw_label=raw_label, raw_value=raw_value,
            provenance=f"{extractor}:{provenance}"
                       + (f" · known wording ({', '.join(sorted(sources))})" if sources else ""),
            raw_sections=raw_sections if any(raw_sections) else [],
            confidence=round(confidence, 3), status=status, flags=flags))

    # --- LLM fallback: fill required metrics we couldn't map deterministically ---
    have = {f.metric_key for f in facts}
    missing_required = [m for m in rt.metrics if m.required and m.key not in have]
    used_llm = False
    if missing_required and settings.llm_enabled and allow_llm:
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
                value, notes = parse_number(payload.get("value", ""), dc)
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

    bounds = quarter_bounds(period) if period == dates.quarter else None
    return ExtractionResult(
        filename=filename, file_hash=file_hash, detected_report_type=rt_key,
        report_type_confidence=rt_conf, entity_name=entity, period=period,
        period_start=bounds[0].isoformat() if bounds else None,
        period_end=bounds[1].isoformat() if bounds else None,
        statement_start=(dates.statement_start.isoformat()
                         if dates.statement_start else None),
        booking_date=dates.booking_date.isoformat() if dates.booking_date else None,
        currency=currency, number_format="1.234,56" if dc else "1,234.56",
        extractor=extractor, facts=facts,
        unmapped=unmapped[:200], warnings=warnings)


def extract_file(path: Path, original_name: Optional[str] = None) -> ExtractionResult:
    """Top-level entry: route a single file to the right extractor and assemble."""
    ext = path.suffix.lower()
    file_hash = file_sha256(path)
    name = original_name or path.name

    if ext in EXCEL_EXT:
        raw_items, text = extract_excel(path)
        return _with_model(path, raw_items, text, name, file_hash, "excel")

    if ext in CSV_EXT:
        raw_items, text = extract_csv(path)
        return _with_model(path, raw_items, text, name, file_hash, "csv")

    if ext in PDF_EXT:
        try:
            raw_items, text = extract_pdf(path)
        except ScannedPdfError as e:
            return _scanned_pdf(path, name, file_hash, str(e))
        return _with_model(path, raw_items, text, name, file_hash, "pdf_native")

    return ExtractionResult(
        filename=name, file_hash=file_hash,
        detected_report_type="fund_financials", report_type_confidence=0.0,
        extractor="none", facts=[],
        warnings=[f"Unsupported file type '{ext}'. Supported: PDF, XLSX, XLSM, XLS, CSV."])


def _with_model(path: Path, raw_items: list[RawItem], text: str, name: str,
                file_hash: str, extractor: str) -> ExtractionResult:
    """Parse deterministically; when the result needs it and a model is
    configured, let the capital-account model step read the document."""
    result = assemble(raw_items, text, name, file_hash, extractor, allow_llm=False)
    note = None
    if settings.llm_enabled and _needs_model(result, text):
        if extractor == "pdf_native":
            pages = pdf_pages(path)
            images = [(pg["page"], render_page_png(path, pg["page"]))
                      for pg in pages if not pg["has_text"]][:settings.llm_max_image_pages]
        else:   # a workbook's sheets (or a CSV) as text "pages"
            pages, images = _text_pages(text), []
        answer = read_capital_account(select_pages(pages, settings.llm_max_doc_chars), images,
                                      name, _model_context(result.entity_name))
        if not answer.get("_error") and answer["document_kind"] in (
                "investor_statement", "fund_level"):
            return _apply_model(result, answer, raw_items, text, extractor)
        note = f"Model step unavailable: {answer['_error']}" if answer.get("_error") else None
    # no (usable) model answer: the generic fill-the-gaps fallback, as before
    out = assemble(raw_items, text, name, file_hash, extractor)
    if note:
        out.warnings.append(note)
    return out


def _text_pages(text: str) -> list[dict]:
    """Split extracted workbook text into one "page" per sheet."""
    chunks = re.split(r"(?m)^(?=# sheet: )", text or "")
    return [{"page": i, "text": c.strip(), "has_text": bool(c.strip())}
            for i, c in enumerate([c for c in chunks if c.strip()], start=1)]


def _model_context(entity: str) -> dict:
    """Known wording + what reviewers confirmed for this fund, for the prompt."""
    from ..terms import fund_memory, presets, taught
    ctx: dict = {"entity": entity, "terms": presets(RF), "memory": None}
    try:
        ctx["terms"] = ([t for t in taught(RF) if t["entity_name"] == entity]
                        + [t for t in taught(RF) if t["entity_name"] is None]
                        + presets(RF))
        if entity and entity != "UNKNOWN":
            mem = fund_memory(entity, RF)
            prof = mem["profile"]
            if mem["quarters"] or prof.get("investor_aliases") or prof.get("notes"):
                ctx["memory"] = mem
    except Exception:
        pass   # no database available: presets only
    return ctx


# --------------------------------------------------------------------------
# Model step for investor capital-account statements (see llm/capital_account)
# --------------------------------------------------------------------------
RF = "investment_roll_forward"
_FLOW_KEYS = ("funding", "equity_income_loss", "return_of_capital", "dividends", "other",
              "investment_mtm", "sale_of_investment", "net_realized_gain",
              "accrued_interest", "note_conversion", "gain_loss_on_conversion",
              "acquisition_impact", "impairment")
_STATEMENT_CUES = ("capital account", "limited partner", "partner's capital",
                   "partners' capital", "partner capital")


def _needs_model(result: ExtractionResult, text: str) -> bool:
    """Should the capital-account model step read this document?"""
    low = (text or "").lower()
    names = [n.strip().lower() for n in settings.lp_names.split(",") if n.strip()]
    if result.detected_report_type != RF:
        # fund reports and unrecognised layouts that talk about capital accounts
        # or name our investor: let the model say whose figures they are
        return any(c in low for c in _STATEMENT_CUES) or any(n in low for n in names)
    by = {f.metric_key: f.value for f in result.facts if f.value is not None}
    if "beginning_balance" not in by or "ending_balance" not in by:
        return True
    if any(fl.startswith("multi_column") for f in result.facts for fl in f.flags):
        return True
    activity = sum((by[k] for k in _FLOW_KEYS if k in by), Decimal(0))
    change = by["ending_balance"] - by["beginning_balance"]
    return abs(activity - change) > max(Decimal(1), abs(change) * Decimal("0.01"))


def _apply_model(result: ExtractionResult, answer: dict, raw_items: list[RawItem],
                 text: str, extractor: str) -> ExtractionResult:
    """Turn the model's reading into the submission's facts."""
    lp = settings.lp_names.split(",")[0].strip() or "our investor"
    provider = settings.llm_provider
    note = f" Note: {answer['notes']}" if answer.get("notes") else ""

    if answer["document_kind"] != "investor_statement" or not answer["fields"]:
        # fund-level figures must never land in our investor's roll-forward
        if result.detected_report_type == RF:
            result = assemble(raw_items, text, result.filename, result.file_hash, extractor,
                              force_type="fund_financials", allow_llm=False)
        result.warnings.append(
            f"The '{provider}' model read this as a fund-level report — {lp}'s own capital "
            f"account isn't identifiable in it, so nothing was added to {lp}'s "
            f"roll-forward. Upload {lp}'s capital-account statement for this fund.{note}")
        return result

    if answer.get("number_format") in ("1.234,56", "1,234.56"):
        dc = answer["number_format"] == "1.234,56"
    else:
        dc = detect_decimal_comma(text)
    rt = REPORT_TYPES[RF]

    # deterministic reading under the roll-forward template, for corroboration
    parsed = {f.metric_key: f for f in assemble(
        raw_items, text, result.filename, result.file_hash, extractor,
        force_type=RF, allow_llm=False).facts if f.value is not None} if raw_items else {}

    def _single_column(f: FinancialFact) -> bool:
        return not any(fl.startswith("multi_column") for fl in f.flags)

    facts: list[FinancialFact] = []
    unreadable: list[str] = []
    for mkey, comps in answer["fields"].items():
        spec = rt.metric_by_key(mkey)
        if spec is None:
            continue
        values = []
        for c in comps:
            raw = c["value"].strip()
            # a printed dash is a nil line, not a missing one
            v = Decimal(0) if raw in {"-", "–", "—", "nil", "Nil"} else parse_number(raw, dc)[0]
            values.append(v)
        if any(v is None for v in values):
            unreadable.append(f"{spec.label} ({' + '.join(c['value'] for c in comps)})")
            continue
        value = sum(values, Decimal(0))
        flags = ["llm_suggested"]
        pf = parsed.get(mkey)
        if pf is not None and pf.value == value:
            flags.append("parser_agrees")
        elif pf is not None and _single_column(pf):
            flags.append(f"parser_reads({pf.value})")   # worth a look in review
        facts.append(FinancialFact(
            entity_name=result.entity_name, entity_kind=rt.entity_kind, report_type=RF,
            period=result.period, metric_key=mkey, metric_label=spec.label, value=value,
            unit=spec.unit, currency=answer.get("currency") or result.currency,
            raw_label=" + ".join(c["label"] for c in comps),
            raw_value=" + ".join(c["value"] for c in comps),
            raw_sections=([c.get("section", "") for c in comps]
                          if any(c.get("section") for c in comps) else []),
            provenance=(f"llm:{provider} [{answer.get('investor_column') or '?'} | "
                        f"{answer.get('period_column') or '?'}]"),
            confidence=0.75 if "parser_agrees" in flags else 0.5,
            status=FactStatus.NEEDS_REVIEW, flags=flags))

    # the parser's clean single-column readings of fields the model didn't return
    for mkey, pf in parsed.items():
        if mkey not in answer["fields"] and _single_column(pf) and mkey in (
                "beginning_balance", "ending_balance", "commitment", "remaining_commitment"):
            pf.flags.append("not_in_model_reading")
            pf.status = FactStatus.NEEDS_REVIEW
            facts.append(pf)

    # period: a quarter the filename names wins; else the model's period end
    dates = detect_statement_dates(text, result.filename)
    period = dates.quarter if dates.explicit_quarter else None
    model_end = _iso(answer.get("period_end"))
    if not period and model_end:
        period = quarter_label(model_end)
    period = period or (result.period if result.period != "UNKNOWN" else "UNKNOWN")
    for f in facts:
        f.period = period
    start = _iso(answer.get("period_start")) or dates.statement_start
    bounds = quarter_bounds(period) if re.fullmatch(r"20\d{2}-Q[1-4]", period) else None

    warnings = [f"Read by the '{provider}' model from column "
                f"'{answer.get('investor_column') or '?'}', period "
                f"'{answer.get('period_column') or '?'}' — check against the source.{note}"]
    if unreadable:
        warnings.append("Couldn't read these values the model quoted: "
                        + "; ".join(unreadable) + ".")
    if model_end and bounds and not (bounds[0] <= model_end <= bounds[1]):
        warnings.append(f"Model reported period end {model_end.isoformat()}, outside "
                        f"{period} taken from the filename — confirm the period.")
    return ExtractionResult(
        filename=result.filename, file_hash=result.file_hash, detected_report_type=RF,
        report_type_confidence=max(result.report_type_confidence, 0.7),
        entity_name=result.entity_name, period=period,
        period_start=bounds[0].isoformat() if bounds else None,
        period_end=bounds[1].isoformat() if bounds else None,
        statement_start=start.isoformat() if start else None,
        booking_date=result.booking_date,
        currency=answer.get("currency") or result.currency,
        extractor=f"{extractor}+llm", facts=facts, unmapped=result.unmapped,
        warnings=warnings)


def _iso(v):
    from datetime import date
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _scanned_pdf(path: Path, name: str, file_hash: str, why: str) -> ExtractionResult:
    """Image-only PDF: read it with a vision model, else local OCR, else say so."""
    from .ocr import ocr_pages, tesseract_available
    classified = classify_document(name)
    empty = ExtractionResult(
        filename=name, file_hash=file_hash, detected_report_type="fund_financials",
        report_type_confidence=0.0, entity_name=classified["entity_name"] or "UNKNOWN",
        extractor="pdf_native", facts=[], warnings=[])
    dates = detect_statement_dates("", name)
    if dates.quarter:
        empty.period = dates.quarter
    pages = pdf_pages(path)

    if settings.llm_enabled and get_provider().supports_vision:
        images = [(pg["page"], render_page_png(path, pg["page"]))
                  for pg in pages[:settings.llm_max_image_pages]]
        answer = read_capital_account([], images, name, _model_context(empty.entity_name))
        if not answer.get("_error") and answer["document_kind"] in (
                "investor_statement", "fund_level"):
            out = _apply_model(empty, answer, [], "", "scan")
            out.warnings.insert(0, "Scanned PDF — read from the page images.")
            return out
        empty.warnings.append(f"Vision model could not read the scan: "
                              f"{answer.get('_error') or answer.get('document_kind')}.")

    if tesseract_available():
        texts = ocr_pages(path, [pg["page"] for pg in pages])
        raw_items = [it for n, t in texts.items() for it in parse_statement_lines(t, f"p{n} ocr")]
        text = "\n".join(texts.values())
        result = assemble(raw_items, text, name, file_hash, "ocr_tesseract", allow_llm=False)
        for f in result.facts:
            f.flags.append("ocr")
            f.status = FactStatus.NEEDS_REVIEW
        if settings.llm_enabled and _needs_model(result, text):
            answer = read_capital_account(sorted(texts.items()), [], name,
                                          _model_context(result.entity_name))
            if not answer.get("_error") and answer["document_kind"] in (
                    "investor_statement", "fund_level"):
                return _apply_model(result, answer, raw_items, text, "ocr_tesseract")
        result.warnings.insert(0, "Scanned PDF — text read by local OCR (Tesseract); "
                                  "check every value against the page.")
        return result

    empty.warnings.append(
        f"SCANNED PDF: {why} No data extracted. Configure a vision model (Azure OpenAI "
        "or OpenAI with an API key) or install local OCR (`brew install tesseract` and "
        "`pip install pytesseract`).")
    return empty
