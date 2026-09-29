"""Pluggable LLM fallback layer.

The pipeline is deterministic-first. When a *required* metric can't be mapped by
the deterministic extractors, it may ask an LLM provider for suggestions. Every
value a provider returns is treated as a *suggestion* — flagged `needs_review`,
never auto-accepted — so the trust guarantee rests on deterministic parsing plus
human confirmation, not on any model.

Providers are interchangeable and selected by `settings.llm_provider`:

    none    -> NoOpProvider          (deterministic only; the default / demo)
    azure   -> AzureOpenAIProvider   (customer's Microsoft model, their tenant)
    openai  -> OpenAIProvider        (public OpenAI cloud)
    ollama  -> OllamaProvider        (local, on-machine)
"""
from __future__ import annotations

from functools import lru_cache

from ..config import settings
from .base import LLMProvider, LLMResult


@lru_cache(maxsize=1)
def get_provider() -> LLMProvider:
    """Return the configured provider (memoized)."""
    name = settings.llm_provider
    if name == "azure":
        from .azure_openai import AzureOpenAIProvider
        return AzureOpenAIProvider()
    if name == "openai":
        from .openai_cloud import OpenAIProvider
        return OpenAIProvider()
    if name == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider()
    from .base import NoOpProvider
    return NoOpProvider()


def extract_with_llm(text: str, report_type_key: str) -> dict:
    """Convenience wrapper used by the extraction registry.

    Returns {"fields": {key: {value, source_label}}, "entity_name", "period",
    "currency"} or {} when disabled/unavailable. Never raises.
    """
    return get_provider().extract(text, report_type_key)


__all__ = ["LLMProvider", "LLMResult", "get_provider", "extract_with_llm"]
