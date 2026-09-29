"""Azure OpenAI provider — the customer's Microsoft model.

This connects to an Azure OpenAI *deployment* running inside the customer's own
Azure subscription/tenant. It is neither a public cloud vendor nor a local model:
requests stay within the customer's Azure boundary and are not shared with OpenAI.

Two authentication modes:
  * API key   — set AZURE_OPENAI_API_KEY.
  * Entra ID  — set AZURE_USE_ENTRA_ID=true and authenticate via `azure-identity`
                (DefaultAzureCredential: managed identity, az login, env vars, ...).
                Keyless; preferred for enterprise.

Config (see app/config.py):
  AZURE_OPENAI_ENDPOINT      https://<resource>.openai.azure.com
  AZURE_OPENAI_DEPLOYMENT    the deployment name (e.g. "gpt-4o")
  AZURE_OPENAI_API_VERSION   e.g. "2024-10-21"
  AZURE_OPENAI_API_KEY       api key  (or use Entra ID)
  AZURE_USE_ENTRA_ID         "true" to use Microsoft Entra ID instead of a key
"""
from __future__ import annotations

from ..config import settings
from .base import (LLMProvider, LLMResult, build_prompt, extract_json,
                   normalize_result)


class AzureOpenAIProvider(LLMProvider):
    name = "azure"

    def __init__(self) -> None:
        self._client = None
        self._init_error: str | None = None

    # -- lazy client construction so a misconfig doesn't crash import/startup --
    def _client_or_error(self):
        if self._client is not None or self._init_error is not None:
            return self._client, self._init_error
        if not settings.azure_openai_endpoint or not settings.azure_openai_deployment:
            self._init_error = ("Azure OpenAI not configured: set "
                                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT.")
            return None, self._init_error
        try:
            from openai import AzureOpenAI
        except Exception as e:  # pragma: no cover - depends on optional dep
            self._init_error = (f"openai SDK not installed ({e}). "
                                "Run: pip install openai")
            return None, self._init_error
        try:
            if settings.azure_use_entra_id:
                from azure.identity import (DefaultAzureCredential,
                                            get_bearer_token_provider)
                token_provider = get_bearer_token_provider(
                    DefaultAzureCredential(),
                    "https://cognitiveservices.azure.com/.default")
                self._client = AzureOpenAI(
                    azure_endpoint=settings.azure_openai_endpoint,
                    api_version=settings.azure_openai_api_version,
                    azure_ad_token_provider=token_provider,
                )
            else:
                if not settings.azure_openai_api_key:
                    self._init_error = ("Azure OpenAI: set AZURE_OPENAI_API_KEY, "
                                        "or AZURE_USE_ENTRA_ID=true for keyless auth.")
                    return None, self._init_error
                self._client = AzureOpenAI(
                    azure_endpoint=settings.azure_openai_endpoint,
                    api_version=settings.azure_openai_api_version,
                    api_key=settings.azure_openai_api_key,
                )
        except Exception as e:  # pragma: no cover
            self._init_error = f"Azure OpenAI client init failed: {e}"
        return self._client, self._init_error

    def available(self) -> bool:
        client, _ = self._client_or_error()
        return client is not None

    def status(self) -> dict:
        client, err = self._client_or_error()
        return {"provider": "azure", "available": client is not None,
                "deployment": settings.azure_openai_deployment or None,
                "auth": "entra_id" if settings.azure_use_entra_id else "api_key",
                "detail": err}

    def extract(self, text: str, report_type_key: str) -> LLMResult:
        client, err = self._client_or_error()
        if client is None:
            return {"_error": err or "Azure OpenAI unavailable"}
        try:
            resp = client.chat.completions.create(
                model=settings.azure_openai_deployment,   # deployment name
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
