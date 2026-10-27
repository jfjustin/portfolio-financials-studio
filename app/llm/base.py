"""LLM provider interface + shared prompt building / JSON parsing.

A provider's only job is: given raw document text and a target report type,
return structured field suggestions. All prompt construction and response
sanitizing is shared here so every provider behaves identically and only the
transport differs.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Optional, TypedDict

from ..config import settings
from ..report_types import REPORT_TYPES


class LLMResult(TypedDict, total=False):
    fields: dict[str, dict]          # {metric_key: {"value": str, "source_label": str}}
    entity_name: Optional[str]
    period: Optional[str]
    currency: Optional[str]
    _error: str                       # present only on failure


# --------------------------------------------------------------------------
# Shared prompt + parsing helpers
# --------------------------------------------------------------------------
def build_prompt(text: str, report_type_key: str) -> str:
    rt = REPORT_TYPES[report_type_key]
    fields = "\n".join(
        f'  - "{m.key}": {m.label} ({m.unit.value})' for m in rt.metrics)
    doc = text[: settings.llm_max_doc_chars]
    return f"""You are a meticulous financial-data extraction assistant.
Extract values for a "{rt.label}" report from the document text below.

Return ONLY a JSON object. For each field you find, use this shape:
{{ "field_key": {{ "value": "<number exactly as written, keep sign/parentheses>",
                   "source_label": "<the label text you saw in the document>" }} }}

Only include a field if you actually find it in the text. Do NOT guess or compute.
If a value is not present, omit that field entirely.
Activity fields are amounts for the statement's own period. Never use a
cumulative / since-inception figure (e.g. "contributions from Initial Closing
Date through ...") for a period field. If the statement is year-to-date, report
the values exactly as printed; the system converts them to the quarter.

Available fields:
{fields}

Also include, if present:
  - "entity_name": the company/fund/JV name
  - "period": the period end date as written (e.g. "June 30, 2026"), not an
    opening "as of" date or a booking/posting date
  - "currency": the currency

Document text:
\"\"\"
{doc}
\"\"\"
JSON:"""


def chat_completion_json(client, *, model: str, prompt: str = "", temperature: float,
                         timeout: int, messages: Optional[list] = None):
    """Chat completion requesting a JSON object response, resilient to models
    that reject a custom `temperature` (newer reasoning-tier models — e.g. the
    gpt-5 family — only accept their default and 400 on anything else).
    Retries once without `temperature` when that specific error is detected.
    `messages` (OpenAI chat format, may include image parts) overrides `prompt`.
    """
    kwargs = dict(model=model,
                  messages=messages or [{"role": "user", "content": prompt}],
                  response_format={"type": "json_object"}, timeout=timeout)
    optional = {"temperature": temperature}
    if settings.llm_reasoning_effort:
        optional["reasoning_effort"] = settings.llm_reasoning_effort
    # Models differ in which tuning parameters they accept (the gpt-5 family
    # rejects a custom temperature; older models reject reasoning_effort): drop
    # whichever one the model refuses and try again.
    for _ in range(len(optional) + 1):
        try:
            return client.chat.completions.create(**kwargs, **optional)
        except Exception as e:
            msg = str(e).lower()
            refused = [k for k in optional if k in msg and any(
                w in msg for w in ("unsupported", "not support", "unrecognized",
                                   "unknown parameter", "invalid"))]
            if not refused:
                raise
            for k in refused:
                optional.pop(k)
    return client.chat.completions.create(**kwargs)


def strip_reasoning(s: str) -> str:
    """Remove <think>...</think> blocks some reasoning models emit."""
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()


def extract_json(s: str) -> Optional[dict]:
    s = strip_reasoning(s)
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def normalize_result(data: Optional[dict], report_type_key: str) -> LLMResult:
    """Coerce arbitrary model JSON into the canonical LLMResult shape."""
    if not isinstance(data, dict):
        return {}
    rt = REPORT_TYPES[report_type_key]
    valid_keys = {m.key for m in rt.metrics}
    fields: dict[str, dict] = {}
    for k, v in data.items():
        if k in valid_keys and isinstance(v, dict) and "value" in v:
            fields[k] = {"value": str(v.get("value", "")),
                         "source_label": str(v.get("source_label", k))}
        elif k in valid_keys and isinstance(v, (str, int, float)):
            fields[k] = {"value": str(v), "source_label": k}
    return {
        "fields": fields,
        "entity_name": data.get("entity_name"),
        "period": data.get("period"),
        "currency": data.get("currency"),
    }


# --------------------------------------------------------------------------
# Provider interface
# --------------------------------------------------------------------------
class LLMProvider(ABC):
    """A pluggable LLM backend. Implementations must never raise from extract()."""

    name: str = "base"

    @abstractmethod
    def extract(self, text: str, report_type_key: str) -> LLMResult:
        """Return field suggestions for the document, or {} on any failure."""

    # Whether complete_json() accepts image parts (scanned pages).
    supports_vision: bool = False

    def complete_json(self, messages: list) -> dict:
        """Run a chat request (OpenAI message format) that must answer with one
        JSON object; returns the parsed object or {"_error": ...}. Never raises."""
        return {"_error": f"{self.name}: free-form requests not supported"}

    def available(self) -> bool:
        """Whether this provider is configured/reachable (best-effort, cheap)."""
        return True

    def status(self) -> dict:
        return {"provider": self.name, "available": self.available()}


class NoOpProvider(LLMProvider):
    """Deterministic-only mode: no model, no network, no suggestions.

    This is the default. The pipeline runs entirely on deterministic extraction
    and reconciliation; anything it can't map is surfaced for human review.
    """

    name = "none"

    def extract(self, text: str, report_type_key: str) -> LLMResult:
        return {}

    def available(self) -> bool:
        return True

    def status(self) -> dict:
        return {"provider": "none", "available": True,
                "detail": "deterministic-only (no model)"}
