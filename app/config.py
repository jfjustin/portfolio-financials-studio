"""Central configuration for Portfolio Financials Studio.

Everything defaults to a **deterministic, offline demo** — the app runs with no
model and no credentials. To use an LLM fallback for non-standard layouts, set
`LLM_PROVIDER` and the matching credentials.

    LLM_PROVIDER = none   -> deterministic only (default; great for demos)
                   azure  -> customer's Microsoft model (Azure OpenAI, in their tenant)
                   openai -> public OpenAI cloud
                   ollama -> local on-machine model

Override any value with an environment variable of the same name (case-insensitive),
or via a local `.env` file. Examples:

    LLM_PROVIDER=azure \
    AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com \
    AZURE_OPENAI_DEPLOYMENT=gpt-4o \
    AZURE_OPENAI_API_KEY=... \
    python -m app.main
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

LLMProviderName = Literal["none", "azure", "openai", "ollama"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore",
                                      case_sensitive=False)

    # --- Storage (all local files, no external DB) ---
    data_dir: Path = BASE_DIR / "data"
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    processed_dir: Path = BASE_DIR / "data" / "processed"
    db_path: Path = BASE_DIR / "data" / "pipeline.db"

    # --- LLM fallback provider (used ONLY for non-standard layouts) -------------
    # Its output is ALWAYS routed to human review; the accuracy guarantee never
    # rests on the model. "none" = deterministic-only (no model calls at all).
    llm_provider: LLMProviderName = "none"
    llm_timeout_s: int = 120
    llm_temperature: float = 0.0
    llm_max_doc_chars: int = 8000  # slice of the document sent to the model

    # Customer Microsoft model — Azure OpenAI (runs inside the customer's tenant)
    azure_openai_endpoint: str = ""        # https://<resource>.openai.azure.com
    azure_openai_deployment: str = ""      # the *deployment* name, not the base model
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_api_key: str = ""         # leave blank to use Entra ID (see below)
    azure_use_entra_id: bool = False       # True -> Microsoft Entra ID / managed identity

    # Public OpenAI cloud (optional)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = ""              # optional override for OpenAI-compatible gateways

    # Local Ollama (optional, on-machine)
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b-instruct"

    # --- Extraction behavior ---
    # Confidence at/above which a value is auto-accepted; below -> flagged for review.
    auto_accept_confidence: float = 0.95
    # Tolerance (absolute) for "subtotals must reconcile to totals" checks.
    reconcile_tolerance: float = 1.0

    # --- Server ---
    host: str = "127.0.0.1"  # localhost only — never binds to a public interface
    port: int = 8713

    @property
    def llm_enabled(self) -> bool:
        """True when any real model provider is configured."""
        return self.llm_provider != "none"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.processed_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
