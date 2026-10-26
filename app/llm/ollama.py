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

    def complete_json(self, messages: list) -> dict:
        # text-only: flatten the chat into one prompt (image parts are skipped)
        prompt = "\n\n".join(
            part["text"] if isinstance(part, dict) else str(part)
            for m in messages
            for part in (m["content"] if isinstance(m["content"], list) else [m["content"]])
            if not isinstance(part, dict) or part.get("type", "text") == "text")
        try:
            r = requests.post(f"{settings.ollama_host}/api/generate", json={
                "model": settings.ollama_model, "prompt": prompt, "stream": False,
                "format": "json", "options": {"temperature": settings.llm_temperature}},
                timeout=settings.llm_timeout_s)
            r.raise_for_status()
            raw = r.json().get("response", "")
        except Exception as e:
            return {"_error": str(e)}
        return extract_json(raw) or {"_error": "model did not return JSON"}

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
