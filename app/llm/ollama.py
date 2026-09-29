"""Local Ollama provider — an on-machine model, nothing leaves the box.

Talks to a local Ollama server (default 127.0.0.1:11434). Useful when no cloud
is permitted at all and the customer prefers a fully-local model.
"""
from __future__ import annotations

import requests

from ..config import settings
from .base import (LLMProvider, LLMResult, build_prompt, extract_json,
                   normalize_result)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def available(self) -> bool:
        try:
            r = requests.get(f"{settings.ollama_host}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def status(self) -> dict:
        return {"provider": "ollama", "available": self.available(),
                "host": settings.ollama_host, "model": settings.ollama_model}

    def extract(self, text: str, report_type_key: str) -> LLMResult:
        try:
            payload = {
                "model": settings.ollama_model,
                "prompt": build_prompt(text, report_type_key),
                "stream": False,
                "format": "json",
                "options": {"temperature": settings.llm_temperature},
            }
            r = requests.post(f"{settings.ollama_host}/api/generate",
                              json=payload, timeout=settings.llm_timeout_s)
            r.raise_for_status()
            raw = r.json().get("response", "")
        except Exception as e:
            return {"_error": str(e)}
        return normalize_result(extract_json(raw) or {}, report_type_key)
