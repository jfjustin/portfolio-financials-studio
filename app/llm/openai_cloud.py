"""Public OpenAI cloud provider (optional).

Uses api.openai.com (or an OpenAI-compatible gateway via OPENAI_BASE_URL).
Included for completeness; for a customer's own Microsoft model use the Azure
provider instead, which keeps data inside their Azure tenant.
"""
from __future__ import annotations

from ..config import settings
from .base import (LLMProvider, LLMResult, build_prompt, extract_json,
                   normalize_result)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self) -> None:
        self._client = None
        self._init_error: str | None = None

    def _client_or_error(self):
        if self._client is not None or self._init_error is not None:
            return self._client, self._init_error
        if not settings.openai_api_key:
            self._init_error = "OpenAI not configured: set OPENAI_API_KEY."
            return None, self._init_error
        try:
            from openai import OpenAI
        except Exception as e:  # pragma: no cover
            self._init_error = f"openai SDK not installed ({e}). Run: pip install openai"
            return None, self._init_error
        kwargs = {"api_key": settings.openai_api_key}
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        try:
            self._client = OpenAI(**kwargs)
        except Exception as e:  # pragma: no cover
            self._init_error = f"OpenAI client init failed: {e}"
        return self._client, self._init_error

    def available(self) -> bool:
        client, _ = self._client_or_error()
        return client is not None

    def status(self) -> dict:
        client, err = self._client_or_error()
        return {"provider": "openai", "available": client is not None,
                "model": settings.openai_model, "detail": err}

    def extract(self, text: str, report_type_key: str) -> LLMResult:
        client, err = self._client_or_error()
        if client is None:
            return {"_error": err or "OpenAI unavailable"}
        try:
            resp = client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user",
                           "content": build_prompt(text, report_type_key)}],
                temperature=settings.llm_temperature,
                response_format={"type": "json_object"},
                timeout=settings.llm_timeout_s,
            )
            raw = resp.choices[0].message.content or ""
        except Exception as e:
            return {"_error": str(e)}
        return normalize_result(extract_json(raw) or {}, report_type_key)
