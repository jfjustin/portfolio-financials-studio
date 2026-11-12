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
                      detect_currency, detect_decimal_comma, detect_scale, label_key,
                      map_label_to_metric, parse_number)
from .. import db
from ..terms import IGNORE, TermIndex
from ..report_types import REPORT_TYPES, Unit, guess_report_type, report_type_scores
from ..llm import extract_with_llm, get_provider
from ..llm.capital_account import read_capital_account, select_pages
from .base import (classify_document, detect_statement_dates, file_sha256,
                   guess_entity_name, is_quarter_label, normalize_period, quarter_bounds,
                   quarter_label)
from .csv_native import extract_csv
from .excel import extract_excel
from .pdf_native import (ScannedPdfError, choose_period_columns, extract_pdf,
                         parse_statement_lines, pdf_pages, render_page_png)

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
    # Filename classification (Fund N / Company X / Note X + document type) gives a
    # canonical entity name; prefer it, falling back to a text-derived guess.
    classified = classify_document(filename)
    if force_type:
        rt_key, rt_conf = force_type, 1.0
    else:
        rt_key, rt_conf = guess_report_type(text_blob, filename)
        if classified["entity_kind"] == "direct_investment":
            # "Company X" / "Note X": the company's own statements, unless the file
            # reads as a valuation of Lam's holding (cost basis, fair value, ownership)
            scores = report_type_scores(text_blob, filename)
            rt_key = ("direct_investment"
                      if scores["direct_investment"] > scores["company_financials"]
                      else "company_financials")
            rt_conf = max(rt_conf, 0.8)
        elif classified["entity_kind"] == "fund" and REPORT_TYPES[rt_key].entity_kind == \
                "direct_investment":
            # "Fund N" is never a direct investment ("fair value" in a JV equity
            # workbook): the best-scoring fund report type, ties to the roll-forward
            scores = report_type_scores(text_blob, filename)
            order = ("investment_roll_forward", "jv_equity_income", "fund_financials",
                     "joint_venture")
            rt_key = max(order, key=lambda k: (scores[k], -order.index(k)))
            rt_conf = min(rt_conf, 0.25)     # overridden: the reviewer confirms the type
    rt = REPORT_TYPES[rt_key]
    dc = detect_decimal_comma(text_blob)   # "1.234.567,89" style document?

    lines = [ln for ln in text_blob.splitlines() if ln.strip()]
    entity = classified["entity_name"] or guess_entity_name(lines, filename)
    dates = detect_statement_dates(text_blob, filename)
    if dates.quarter and (rt_key in ("investment_roll_forward", "company_financials")
                          or dates.explicit_quarter):
        # roll-forwards, company statements (and any file that names its quarter)
        # are recorded per calendar quarter (Q2 = Apr 1–Jun 30), inferred from what
        # the document covers, not from its first date
        period = dates.quarter
        if (rt_key == "company_financials" and dates.statement_end and not dates.explicit_quarter
                and re.search(r"\bmonth ended\b", text_blob, re.I)
                and not re.search(r"\bmonths ended\b|\bquarter", text_blob, re.I)):
            period = dates.statement_end.strftime("%Y-%m")   # a one-month statement
    elif dates.statement_end:
        period = dates.statement_end.isoformat()
    else:
        period = normalize_period(text_blob) or normalize_period(filename) or "UNKNOWN"
    currency = detect_currency(text_blob) or "USD"
    choose_period_columns(raw_items, period)
    if rt_key == "company_financials":
        return _company_result(raw_items, text_blob, filename, file_hash, extractor, rt_conf,
                               entity, period, dates, currency, dc)

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
        # PDF text: the page — and the statement block when a page holds
        # several side by side ("p3 block2 text")
        return p.split(" text")[0].split(" ocr")[0]

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


def _company_result(raw_items: list[RawItem], text_blob: str, filename: str, file_hash: str,
                    extractor: str, rt_conf: float, entity: str, period: str, dates,
                    currency: str, dc: Optional[bool]) -> ExtractionResult:
    """A portfolio company's own statements -> the Direct Investment figures
    (see extractors/company.py)."""
    from .company import CORE, read_statements
    rt = REPORT_TYPES["company_financials"]
    scale = detect_scale(text_blob)
    picks, notes = read_statements(raw_items, period, dc, scale,
                                   TermIndex.for_entity(entity, rt.key))
    facts: list[FinancialFact] = []
    for spec in rt.metrics:
        pk = picks.get(spec.key)
        if pk is None:
            continue
        confidence = pk.confidence if pk.value is not None else 0.0
        facts.append(FinancialFact(
            entity_name=entity, entity_kind=rt.entity_kind, report_type=rt.key,
            period=period, metric_key=spec.key, metric_label=spec.label, value=pk.value,
            unit=spec.unit, currency=currency, raw_label=pk.label, raw_value=pk.raw_value,
            provenance=f"{extractor}:{pk.provenance}"
                       + (f" · known wording ({pk.source})" if pk.source else ""),
            raw_sections=pk.sections, confidence=round(confidence, 3),
            breakdown={m: str(v) for m, v in pk.breakdown.items()},
            status=(FactStatus.AUTO
                    if confidence >= settings.auto_accept_confidence and not pk.flags
                    else FactStatus.NEEDS_REVIEW),
            flags=pk.flags))
    used = {id(it) for pk in picks.values() for it in pk.items}
    warnings = list(notes)
    missing = [rt.metric_by_key(k).label for k in CORE if k not in picks]
    if missing and raw_items:
        warnings.append("Not in this file: " + ", ".join(missing) + " — upload the statement "
                        "that shows it, or add the value on review.")
    if scale != 1:
        warnings.append(f"Figures are stated in {'thousands' if scale == 1000 else 'millions'}"
                        f" — recorded in whole {currency}.")
    if period == "UNKNOWN":
        warnings.append("Reporting period could not be determined — please set it.")
    bounds = quarter_bounds(period) if is_quarter_label(period) else None
    return ExtractionResult(
        filename=filename, file_hash=file_hash, detected_report_type=rt.key,
        report_type_confidence=rt_conf, entity_name=entity, period=period,
        period_start=bounds[0].isoformat() if bounds else None,
        period_end=bounds[1].isoformat() if bounds else None,
        statement_start=(dates.statement_start.isoformat() if dates.statement_start else None),
        booking_date=dates.booking_date.isoformat() if dates.booking_date else None,
        currency=currency, number_format="1.234,56" if dc else "1,234.56",
        extractor=extractor, facts=facts,
        unmapped=[it for it in raw_items if id(it) not in used][:200], warnings=warnings)


def extract_file(path: Path, original_name: Optional[str] = None) -> ExtractionResult:
    """Top-level entry: route a single file to the right extractor and assemble."""
    ext = path.suffix.lower()
    file_hash = file_sha256(path)
    name = original_name or path.name

    if ext in EXCEL_EXT:
        from .master import period_from_name, read_master
        reading = read_master(path)
        if reading:
            n_funds = sum(1 for r in reading["records"] if r["kind"] == "fund")
            n_cos = len(reading["records"]) - n_funds
            return ExtractionResult(
                filename=name, file_hash=file_hash, detected_report_type="master_workbook",
                report_type_confidence=1.0,
                entity_name=f"{n_funds} fund(s), {n_cos} direct investment(s)",
                period=period_from_name(name) or reading.get("period") or "",
                extractor="master", facts=[],
                warnings=([f"Sheets not read (not an entity or summary sheet): "
                           + ", ".join(reading["skipped"])] if reading["skipped"] else []),
                master=reading)
        raw_items, text = extract_excel(path)
        return _with_schedule_totals(_with_model(path, raw_items, text, name, file_hash, "excel"),
                                     text)

    if ext in CSV_EXT:
        raw_items, text = extract_csv(path)
        return _with_schedule_totals(_with_model(path, raw_items, text, name, file_hash, "csv"),
                                     text)

    if ext in PDF_EXT:
        try:
            raw_items, text = extract_pdf(path)
        except ScannedPdfError as e:
            return _scanned_pdf(path, name, file_hash, str(e))
        return _with_schedule_totals(
            _with_model(path, raw_items, text, name, file_hash, "pdf_native"), text)

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
    if result.detected_report_type == "company_financials":
        return _company_with_model(path, result, raw_items, text, name, file_hash, extractor)
    note = None
    tried = settings.llm_enabled and _needs_model(result, text)
    if tried:
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
            out = _with_fund_facts(_apply_model(result, answer, raw_items, text, extractor),
                                   answer)
            out.raw_items, out.model_reading = raw_items, answer
            return out
        note = f"Model step unavailable: {answer['_error']}" if answer.get("_error") else None
    # no (usable) model answer. The generic fill-the-gaps fallback is another
    # model call, so it's only used when the model step didn't already run —
    # two long calls would overrun the request limit.
    out = assemble(raw_items, text, name, file_hash, extractor, allow_llm=not tried)
    if note:
        out.warnings.append(note)
    out.raw_items = raw_items
    return out


# --------------------------------------------------------------------------
# Model step for a portfolio company's own statements (see llm/company_statements)
# --------------------------------------------------------------------------
_STATEMENT_OF = {"revenue": ("income_statement",), "operating_expense": ("income_statement",),
                 "operating_cash_flow": ("cash_flow",), "capex": ("cash_flow",),
                 "ending_cash": ("balance_sheet", "cash_flow")}


def _company_with_model(path: Path, result: ExtractionResult, raw_items: list[RawItem],
                        text: str, name: str, file_hash: str,
                        extractor: str) -> ExtractionResult:
    """Company statements: scanned pages, and figures whose statement is in the
    file but whose wording wasn't recognised, go to OCR / the model."""
    from .company import CORE
    from .ocr import ocr_pages, tesseract_available
    from .pdf_native import page_statement
    result.raw_items = raw_items
    pages = pdf_pages(path) if extractor == "pdf_native" else _text_pages(text)
    scanned = [pg["page"] for pg in pages if not pg["has_text"]] if extractor == "pdf_native" else []
    vision = settings.llm_enabled and get_provider().supports_vision

    if scanned and not vision and tesseract_available():
        texts = ocr_pages(path, scanned)
        kind, ocr_items = "", []
        for n, t in sorted(texts.items()):
            kind = page_statement(t, kind)
            for it in parse_statement_lines(t, f"p{n} ocr"):
                it.statement = it.statement or kind
                ocr_items.append(it)
        result = assemble(raw_items + ocr_items, "\n".join([text] + list(texts.values())), name,
                          file_hash, "pdf_native", force_type="company_financials",
                          allow_llm=False)
        for f in result.facts:
            if " ocr" in f.provenance:
                f.flags.append("ocr")
                f.status = FactStatus.NEEDS_REVIEW
        result.warnings.insert(0, f"Scanned page(s) {_pages(scanned)} read by local OCR "
                                  "(Tesseract) — check every value against the page.")
        result.raw_items = raw_items + ocr_items
        scanned = []
    elif scanned and not vision:
        result.warnings.insert(0, f"Page(s) {_pages(scanned)} are scanned images and weren't "
                                  "read: configure a vision model (Azure OpenAI) or local OCR "
                                  "(Tesseract), or enter the figures on review.")

    have = {f.metric_key for f in result.facts if f.value is not None}
    shown = {it.statement for it in result.raw_items or [] if it.statement}
    unread = [k for k in CORE if k not in have and (not shown or set(_STATEMENT_OF[k]) & shown)]
    if not settings.llm_enabled or not (unread or (scanned and vision)):
        return result
    texts = []
    used = 0
    for pg in pages:
        if pg["has_text"] and used + len(pg["text"]) <= settings.llm_max_doc_chars:
            texts.append((pg["page"], pg["text"]))
            used += len(pg["text"])
    limit = max(settings.llm_max_image_pages, 6)
    if len(scanned) > limit and scanned[0] == 1:
        scanned = scanned[1:]           # a scanned cover page goes first
    images = ([(n, render_page_png(path, n)) for n in scanned[:limit]] if vision else [])
    from ..llm.company_statements import read_company_statements
    answer = read_company_statements(texts, images, name)
    if answer.get("_error"):
        result.warnings.append(f"Model step unavailable: {answer['_error']}")
        return result
    out = _apply_company_model(result, answer, [n for n, _png in images])
    out.model_reading = answer
    return out


def _apply_company_model(result: ExtractionResult, answer: dict,
                         image_pages: list[int]) -> ExtractionResult:
    """Merge the model's reading of company statements into the parsed facts:
    agreement is noted, disagreement flagged, gaps filled for review."""
    from .company import CORE
    rt = REPORT_TYPES["company_financials"]
    provider = settings.llm_provider
    dc = result.number_format == "1.234,56"
    scale = answer.get("scale") or 1
    if result.period == "UNKNOWN" and _iso(answer.get("period_end")):
        result.period = quarter_label(_iso(answer["period_end"]))
        bounds = quarter_bounds(result.period)
        result.period_start, result.period_end = bounds[0].isoformat(), bounds[1].isoformat()
    if result.entity_name == "UNKNOWN" and answer.get("company"):
        result.entity_name = answer["company"]
    if image_pages and not result.facts and answer.get("currency"):
        result.currency = answer["currency"].upper()[:3]   # nothing else to go on in a scan
    by_key = {f.metric_key: f for f in result.facts}
    used_model = False
    for key, comps in answer["fields"].items():
        spec = rt.metric_by_key(key)
        vals = [parse_number(c["value"], dc)[0] for c in comps]
        if spec is None or not vals or any(v is None for v in vals):
            continue
        value = sum(vals, Decimal(0)) * (scale if spec.unit == Unit.CURRENCY else 1)
        pages = sorted({c["page"] for c in comps if c.get("page")})
        from_image = any(n in image_pages for n in pages)
        prov = (f"llm:{provider}" + (f" p{', p'.join(str(n) for n in pages)}" if pages else "")
                + (" (scanned page image)" if from_image else ""))
        f = by_key.get(key)
        if f is not None and f.value is not None:
            if abs(f.value - value) <= max(Decimal(1), abs(f.value) * Decimal("0.005")):
                f.provenance += " · model agrees"
            else:
                f.flags.append(f"model_reads({value})")
                f.status = FactStatus.NEEDS_REVIEW
            continue
        flags = ["llm_suggested"] + (["vision"] if from_image else []) + (
            [f"scaled_x{scale}"] if scale != 1 and spec.unit == Unit.CURRENCY else [])
        fact = FinancialFact(
            entity_name=result.entity_name, entity_kind=rt.entity_kind, report_type=rt.key,
            period=result.period, metric_key=key, metric_label=spec.label, value=value,
            unit=spec.unit, currency=result.currency,
            raw_label=" + ".join(c["label"] for c in comps),
            raw_value=" + ".join(c["value"] for c in comps), provenance=prov,
            confidence=0.5, status=FactStatus.NEEDS_REVIEW, flags=flags)
        if f is not None:
            result.facts.remove(f)
        result.facts.append(fact)
        used_model = True
    order = [m.key for m in rt.metrics]
    result.facts.sort(key=lambda f: order.index(f.metric_key) if f.metric_key in order else 99)
    have = {f.metric_key for f in result.facts if f.value is not None}
    result.warnings = [w for w in result.warnings if not w.startswith("Not in this file:")]
    missing = [rt.metric_by_key(k).label for k in CORE if k not in have]
    if missing:
        result.warnings.append("Not in this file: " + ", ".join(missing) + " — upload the "
                               "statement that shows it, or add the value on review.")
    if used_model:
        result.warnings.append(f"Some values were read by the '{provider}' model"
                               + (" from scanned page images" if image_pages else "")
                               + " — confirm each against the page.")
    if answer.get("notes"):
        result.warnings.append(f"Model note: {answer['notes']}")
    return result


def _pages(nums: list[int]) -> str:
    return ", ".join(str(n) for n in nums)


def _text_pages(text: str) -> list[dict]:
    """Split extracted workbook text into one "page" per sheet."""
    chunks = re.split(r"(?m)^(?=# sheet: )", text or "")
    return [{"page": i, "text": c.strip(), "has_text": bool(c.strip())}
            for i, c in enumerate([c for c in chunks if c.strip()], start=1)]


def _model_context(entity: str) -> dict:
    """Known wording + what reviewers confirmed for this fund, for the prompt."""
    from ..terms import fund_memory, presets, taught
    ctx: dict = {"entity": entity, "terms": presets(RF), "memory": None, "commitment": None}
    try:
        from ..terms import our_commitment
        amount, src = our_commitment(entity, RF) if entity and entity != "UNKNOWN" else (None, "")
        if amount:
            ctx["commitment"] = f"{amount:,.0f} ({src})"
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


_SCHEDULE = re.compile(r"schedules? of (?:portfolio )?investments|summary of investments|"
                       r"portfolio summary", re.I)
_SCHEDULE_TOTAL = re.compile(r"^\s*total\s+(?:portfolio\s+|non-marketable\s+)?investments\b"
                             r"(?P<rest>[^a-z]*)$", re.I | re.M)
_FIGURE_TOKEN = re.compile(r"\(?-?\$?\s?\d[\d,.]*\)?")


def _with_schedule_totals(out: ExtractionResult, text: str) -> ExtractionResult:
    """A fund report's schedule of investments, when the model step didn't read
    it: the grand-total line's cost and fair value ("Total Portfolio Investments
    95.6  $ 40,000,000  $ 55,000,000  $ 15,000,000"). Cost and fair value are
    the first two amounts; when a third follows it must be their difference
    (unrealized appreciation), or the line isn't trusted."""
    rt = REPORT_TYPES.get(out.detected_report_type)
    if rt is None or not rt.metric_by_key("portfolio_cost") or not _SCHEDULE.search(text or ""):
        return out
    have = {f.metric_key for f in out.facts if f.value is not None}
    if {"portfolio_cost", "portfolio_fair_value"} <= have:
        return out
    dc = out.number_format == "1.234,56"
    found = None
    for m in _SCHEDULE_TOTAL.finditer(text):
        amounts = [v for v in (parse_number(t, dc)[0] for t in _FIGURE_TOKEN.findall(m.group("rest")))
                   if v is not None and abs(v) >= 1000]     # not a % of capital or a count
        if len(amounts) < 2:
            continue
        cost, fv = amounts[0], amounts[1]
        if len(amounts) >= 3 and abs((fv - cost) - amounts[2]) > max(Decimal(1), abs(fv) * Decimal("0.0001")):
            continue
        found = (cost, fv, m.group(0).strip())          # the last grand total wins
    if found is None:
        return out
    cost, fv, line = found
    label = re.match(r"\s*([^\d$(]*)", line).group(1).strip() or "Total investments"
    for key, value in (("portfolio_cost", cost), ("portfolio_fair_value", fv)):
        if key in have:
            continue
        spec = rt.metric_by_key(key)
        out.facts.append(FinancialFact(
            entity_name=out.entity_name, entity_kind=rt.entity_kind, report_type=rt.key,
            period=out.period, metric_key=key, metric_label=spec.label, value=abs(value),
            unit=spec.unit, currency=out.currency, raw_label=label, raw_value=line,
            provenance=f"{out.extractor}: schedule of investments, grand total",
            confidence=0.8, status=FactStatus.NEEDS_REVIEW, flags=["fund_wide"]))
    return out


def _with_fund_facts(out: ExtractionResult, answer: dict) -> ExtractionResult:
    """The fund as a whole — its size and its schedule of investments' totals —
    as read by the model step, recorded with the submission for the Investment
    Portfolio report ("Latest Stats"). Suggestions, always reviewed."""
    rt = REPORT_TYPES.get(out.detected_report_type)
    if rt is None or not rt.metric_by_key("fund_size"):
        return out
    dc = out.number_format == "1.234,56"
    have = {f.metric_key for f in out.facts if f.value is not None}
    pf = answer.get("portfolio") or {}
    size = answer.get("fund_size") or (
        ((answer.get("fund_fields") or {}).get("commitment") or [{}])[0].get("value"))
    as_of = f" as of {pf['as_of']}" if pf.get("as_of") else ""
    picks = [("fund_size", size, "fund's total commitments"),
             ("portfolio_investments", pf.get("n_investments"),
              f"schedule of investments{as_of}: portfolio companies"),
             ("portfolio_cost", pf.get("total_cost"), f"schedule of investments{as_of}: total cost"),
             ("portfolio_fair_value", pf.get("total_fair_value"),
              f"schedule of investments{as_of}: total fair value")]
    for key, raw, label in picks:
        spec = rt.metric_by_key(key)
        value = parse_number(str(raw), dc)[0] if raw not in (None, "") else None
        if spec is None or value is None or key in have:
            continue
        if spec.unit == Unit.COUNT:
            value = value.to_integral_value()
        out.facts.append(FinancialFact(
            entity_name=out.entity_name, entity_kind=rt.entity_kind, report_type=rt.key,
            period=out.period, metric_key=key, metric_label=spec.label, value=abs(value),
            unit=spec.unit, currency=out.currency, raw_label=label, raw_value=str(raw),
            provenance=f"llm:{settings.llm_provider} (the fund as a whole)", confidence=0.5,
            status=FactStatus.NEEDS_REVIEW, flags=["llm_suggested", "fund_wide"]))
    return out


def _apply_model(result: ExtractionResult, answer: dict, raw_items: list[RawItem],
                 text: str, extractor: str) -> ExtractionResult:
    """Turn the model's reading into the submission's facts."""
    lp = settings.lp_names.split(",")[0].strip() or "our investor"
    provider = settings.llm_provider
    note = f" Note: {answer['notes']}" if answer.get("notes") else ""

    if answer["document_kind"] != "investor_statement" or not answer["fields"]:
        return _apply_fund_level(result, answer, raw_items, text, extractor)

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
            pf.currency = answer.get("currency") or pf.currency
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


# whole-fund figures (roll-forward field names) -> fund-level metrics
_FUND_LEVEL_MAP = {"ending_balance": "nav", "funding": "capital_called",
                   "return_of_capital": "distributions", "accrued_interest": "total_income",
                   "other": "total_expenses", "net_realized_gain": "realized_gain",
                   "investment_mtm": "unrealized_gain", "equity_income_loss": "net_income"}
_PRO_RATA_KEYS = ("beginning_balance", "funding", "return_of_capital", "dividends",
                  "accrued_interest", "other", "net_realized_gain", "investment_mtm",
                  "equity_income_loss")


def _apply_fund_level(result: ExtractionResult, answer: dict, raw_items: list[RawItem],
                      text: str, extractor: str) -> ExtractionResult:
    """A fund-level report (whole-fund statements, maybe a table of investors).

    Our investor's quarter is the fund's quarter x our share, when the share is
    known — our commitment (our row in the report, the fund parameter, or a
    confirmed commitment) over total commitments. Funds allocate pro-rata to
    commitment, which our row's capital account cross-checks. Without a share,
    the fund's own figures are kept as fund-level values and nothing is added to
    our roll-forward.
    """
    from ..terms import our_commitment
    from decimal import ROUND_HALF_UP
    lp = settings.lp_names.split(",")[0].strip() or "our investor"
    provider = settings.llm_provider
    note = f" Note: {answer['notes']}" if answer.get("notes") else ""
    entity = result.entity_name
    if answer.get("number_format") in ("1.234,56", "1,234.56"):
        dc = answer["number_format"] == "1.234,56"
    else:
        dc = detect_decimal_comma(text)

    def num(raw: Optional[str]) -> Optional[Decimal]:
        if raw is None:
            return None
        raw = raw.strip()
        return Decimal(0) if raw in {"-", "–", "—", "nil", "Nil"} else parse_number(raw, dc)[0]

    fund: dict[str, tuple[Decimal, list[dict]]] = {}
    for key, comps in (answer.get("fund_fields") or {}).items():
        vals = [num(c["value"]) for c in comps]
        if vals and all(v is not None for v in vals):
            fund[key] = (sum(vals, Decimal(0)), comps)
    # Fund statements print expenses and distributions as positive amounts under
    # their headings, and the model copies figures as printed. The fund's quarter
    # must reconcile (beginning + activity = ending): use the signs that make it.
    flipped: list[str] = []
    if fund.get("beginning_balance") and fund.get("ending_balance"):
        flows = [k for k in ("funding", "return_of_capital", "dividends", "accrued_interest",
                             "other", "net_realized_gain", "investment_mtm",
                             "equity_income_loss") if k in fund]
        target = fund["ending_balance"][0] - fund["beginning_balance"][0]
        tol = max(Decimal(1), abs(target) * Decimal("0.005"))
        for flip in ([], ["other"], ["return_of_capital"], ["other", "return_of_capital"]):
            flip = [k for k in flip if k in fund and fund[k][0] > 0]
            total = sum((fund[k][0] * (-1 if k in flip else 1) for k in flows), Decimal(0))
            if abs(total - target) <= tol:
                for k in flip:
                    fund[k] = (-fund[k][0], fund[k][1])
                flipped = flip
                break
    row = answer.get("investor_row") or {}
    row_label = row.get("row_label") or "?"
    row_commit, row_cap = num(row.get("commitment")), num(row.get("capital_account"))

    commit, commit_src = (abs(row_commit), f"row '{row_label}'") if row_commit else (None, "")
    if commit is None:
        try:
            commit, commit_src = our_commitment(entity, RF)
        except Exception:
            commit, commit_src = None, ""
    total_commit = abs(fund["commitment"][0]) if fund.get("commitment") else None
    share, basis = None, ""
    if commit and total_commit:
        share = commit / total_commit
        basis = f"commitment {commit:,.0f} ({commit_src}) of total commitments {total_commit:,.0f}"
    elif row_cap and fund.get("ending_balance") and fund["ending_balance"][0]:
        share = row_cap / fund["ending_balance"][0]
        basis = (f"row '{row_label}' capital account {row_cap:,.0f} of fund net assets "
                 f"{fund['ending_balance'][0]:,.0f}")

    # period: a quarter the filename names wins; else the model's period end
    dates = detect_statement_dates(text, result.filename)
    model_end = _iso(answer.get("period_end"))
    period = (dates.quarter if dates.explicit_quarter
              else quarter_label(model_end) if model_end else result.period)
    bounds = quarter_bounds(period) if re.fullmatch(r"20\d{2}-Q[1-4]", period or "") else None
    currency = answer.get("currency") or result.currency
    col = f"[whole fund | {answer.get('period_column') or '?'}]"

    # our own statement for this quarter already on file: don't compete with it
    try:
        own = [f for f in db.facts_for_quarter(entity, RF, period)
               if f["status"] != "rejected" and "pro_rata" not in f["flags"]]
    except Exception:
        own = []

    base = dict(filename=result.filename, file_hash=result.file_hash, entity_name=entity,
                period=period, period_start=bounds[0].isoformat() if bounds else None,
                period_end=bounds[1].isoformat() if bounds else None,
                booking_date=result.booking_date, currency=currency,
                number_format="1.234,56" if dc else "1,234.56",
                extractor=f"{extractor}+llm", unmapped=result.unmapped)

    if share is None or own:
        # fund-level values: the parser's reading of the fund's statements, with
        # the model's figures as a cross-check and to fill what the parser missed
        if result.detected_report_type != "fund_financials":
            result = assemble(raw_items, text, result.filename, result.file_hash,
                              extractor, force_type="fund_financials", allow_llm=False)
        facts_out = list(result.facts)
        by_key = {f.metric_key: f for f in facts_out}
        frt = REPORT_TYPES["fund_financials"]
        for key, (val, comps) in fund.items():
            spec = frt.metric_by_key(_FUND_LEVEL_MAP.get(key, ""))
            if spec is None:
                continue
            pf = by_key.get(spec.key)
            if pf is not None and pf.value is None:
                # the parser found the line but no figure ("-"): the model's reading fills it
                pf.value, pf.status = val, FactStatus.NEEDS_REVIEW
                pf.flags = [fl for fl in pf.flags if fl != "no_value"] + ["llm_suggested"]
                pf.provenance += f" · value read by llm:{provider}"
                continue
            if pf is not None:
                if abs(pf.value) == abs(val):
                    pf.flags.append("model_agrees")
                else:
                    pf.flags.append(f"model_reads({val})")
                    pf.status = FactStatus.NEEDS_REVIEW
                continue
            facts_out.append(FinancialFact(
                entity_name=entity, entity_kind=frt.entity_kind, report_type=frt.key,
                period=period, metric_key=spec.key, metric_label=spec.label, value=val,
                unit=spec.unit, currency=currency,
                raw_label=" + ".join(c["label"] for c in comps),
                raw_value=" + ".join(c["value"] for c in comps),
                provenance=f"llm:{provider} {col}", confidence=0.5,
                status=FactStatus.NEEDS_REVIEW, flags=["llm_suggested"]))
        for f in facts_out:
            f.period = period
            if f.unit in (Unit.CURRENCY, "currency"):
                f.currency = currency       # the currency the document reports in
        rt_key, conf = "fund_financials", max(result.report_type_confidence, 0.7)
        if own:
            ours = {f["metric_key"]: f["value"] for f in own}
            check = ""
            if share is not None and fund.get("ending_balance") and ours.get("ending_balance"):
                computed = fund["ending_balance"][0] * share
                check = (f" Pro-rata check: fund net assets x {share:.4%} = {computed:,.0f} vs "
                         f"{lp}'s statement {Decimal(ours['ending_balance']):,.0f}.")
            msg = (f"Fund-level report — {lp}'s own statement for {entity} {period} is already "
                   f"on file, so this report's figures are kept as fund-level values only."
                   f"{check}{note}")
        else:
            msg = (f"Fund-level report — {lp}'s own figures for {entity} aren't identifiable in it "
                   f"(no column for {lp}, or investor names are redacted). Enter {lp}'s "
                   f"commitment to {entity} (or its row name) under Format memory → Fund "
                   f"parameters and click 'Save & re-read' — {lp}'s quarter is then computed "
                   f"from the fund's figures. The fund's own figures were kept for reference.{note}")
        return ExtractionResult(detected_report_type=rt_key, report_type_confidence=conf,
                                facts=facts_out, warnings=[msg], **base)

    # our quarter = the fund's quarter x our share
    rt = REPORT_TYPES[RF]
    unit = Decimal(1)
    facts_out: list[FinancialFact] = []
    # the parser's reading of the same fund figures, to corroborate the model's
    parsed_fund = {f.metric_key: f.value for f in assemble(
        raw_items, text, result.filename, result.file_hash, extractor,
        force_type=RF, allow_llm=False).facts if f.value is not None} if raw_items else {}

    def add(key: str, value: Decimal, raw_label: str, raw_value: str, prov: str,
            extra: tuple = ()) -> None:
        spec = rt.metric_by_key(key)
        facts_out.append(FinancialFact(
            entity_name=entity, entity_kind=rt.entity_kind, report_type=RF, period=period,
            metric_key=key, metric_label=spec.label, value=value, unit=spec.unit,
            currency=currency, raw_label=raw_label, raw_value=raw_value,
            provenance=f"{prov} · llm:{provider}", confidence=0.5,
            status=FactStatus.NEEDS_REVIEW, flags=["llm_suggested", "pro_rata", *extra]))

    for key in _PRO_RATA_KEYS:
        if key in fund:
            val, comps = fund[key]
            agree = (("parser_agrees",) if parsed_fund.get(key) is not None
                     and abs(parsed_fund[key]) == abs(val) else ())
            add(key, (val * share).quantize(unit, ROUND_HALF_UP),
                " + ".join(c["label"] for c in comps), f"{val} x {share:.6f}",
                f"pro-rata {share:.4%} of fund {rt.metric_by_key(key).label} {val:,.0f}", agree)
    if fund.get("ending_balance") or row_cap is not None:
        computed = (fund["ending_balance"][0] * share).quantize(unit, ROUND_HALF_UP) \
            if fund.get("ending_balance") else None
        if row_cap is not None:
            extra = ()
            if computed is not None and abs(row_cap - computed) > max(unit, abs(computed) * Decimal("0.005")):
                extra = (f"pro_rata_mismatch({computed})",)
            add("ending_balance", row_cap, f"{row_label}: capital account", row.get("capital_account") or "",
                f"row '{row_label}' capital account"
                + (f" (pro-rata check {computed:,.0f})" if computed is not None else ""), extra)
        else:
            val, comps = fund["ending_balance"]
            add("ending_balance", computed, " + ".join(c["label"] for c in comps),
                f"{val} x {share:.6f}", f"pro-rata {share:.4%} of fund net assets {val:,.0f}")
    add("commitment", commit, f"{lp} commitment", f"{commit}", f"{commit_src}")
    if row.get("paid_in") and num(row["paid_in"]) is not None:
        add("cumulative_contributions", num(row["paid_in"]), f"{row_label}: paid in",
            row["paid_in"], f"row '{row_label}'")
    if row.get("distributions") and num(row["distributions"]) is not None:
        add("cumulative_distributions", num(row["distributions"]), f"{row_label}: distributions",
            row["distributions"], f"row '{row_label}'")

    signs = (f" Printed as positive amounts, {' and '.join(rt.metric_by_key(k).label for k in flipped)} "
             f"were taken as outflows so the fund's quarter reconciles." if flipped else "")
    warning = (f"Fund-level report: {lp}'s quarter for {entity} computed pro-rata — {basis} "
               f"= {share:.4%}. This assumes the fund allocates to investors in proportion "
               f"to commitment; check the share and the fund's figures before confirming."
               f"{signs}{note}")
    return ExtractionResult(detected_report_type=RF, report_type_confidence=0.7,
                            facts=facts_out, warnings=[warning],
                            statement_start=None, **base)


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
    if classified["entity_kind"] == "direct_investment":
        # a portfolio company's scanned statements
        result = assemble([], "", name, file_hash, "pdf_native",
                          force_type="company_financials", allow_llm=False)
        return _company_with_model(path, result, [], "", name, file_hash, "pdf_native")
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
            out = _with_fund_facts(_apply_model(empty, answer, [], "", "scan"), answer)
            out.warnings.insert(0, "Scanned PDF — read from the page images.")
            out.model_reading = answer
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
                out = _with_fund_facts(
                    _apply_model(result, answer, raw_items, text, "ocr_tesseract"), answer)
                out.raw_items, out.model_reading = raw_items, answer
                return out
        result.warnings.insert(0, "Scanned PDF — text read by local OCR (Tesseract); "
                                  "check every value against the page.")
        result.raw_items = raw_items
        return result

    empty.warnings.append(
        f"SCANNED PDF: {why} No data extracted. Configure a vision model (Azure OpenAI "
        "or OpenAI with an API key) or install local OCR (`brew install tesseract` and "
        "`pip install pytesseract`).")
    return empty
