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

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

LLMProviderName = Literal["none", "azure", "openai", "ollama"]


def _default_data_dir() -> Path:
    """Where to store the SQLite DB and uploaded-file copies.

    Azure App Service (Linux, Python/Oryx) unpacks the deployed app into an
    EPHEMERAL path on every container start (observed: /tmp/<hash>/), so
    anchoring storage at BASE_DIR there silently loses all data on every
    restart, scale event, or redeploy. App Service mounts /home as persistent
    storage, so default there to /home/data when running on App Service
    (detected via WEBSITE_INSTANCE_ID, which Azure sets on every instance).
    Override explicitly with the DATA_DIR environment variable.
    """
    home = Path("/home")
    if os.environ.get("WEBSITE_INSTANCE_ID") and home.is_dir():
        return home / "data"
    return BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore",
                                      case_sensitive=False)

    # --- Storage ---------------------------------------------------------------
    # Single source of truth: uploads/processed/db_path all derive from data_dir
    # (see properties below) so one DATA_DIR override moves everything together.
    data_dir: Path = Field(default_factory=_default_data_dir)

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pipeline.db"

    # --- Database (SQLAlchemy URL) -------------------------------------------
    # Default is the local SQLite file above. For Azure SQL / PostgreSQL set e.g.:
    #   postgresql+psycopg://user:pass@host:5432/dbname
    #   mssql+pyodbc://user:pass@server.database.windows.net/db?driver=ODBC+Driver+18+for+SQL+Server
    # Leave blank to use the SQLite db_path.
    database_url: str = ""

    # --- Object storage for uploaded source documents -----------------------
    #   local  -> keep copies under data/uploads (default)
    #   azure  -> Azure Blob Storage container (confidential docs in the tenant)
    storage_backend: str = "local"               # local | azure
    azure_storage_account_url: str = ""          # https://<acct>.blob.core.windows.net
    azure_storage_container: str = "fund-documents"
    azure_storage_connection_string: str = ""    # optional; else uses Entra ID / managed identity

    # --- Authentication ------------------------------------------------------
    #   none      -> open (single local user; default for local/dev)
    #   password  -> shared-password gate (a private workspace behind one password)
    #   entra     -> Microsoft Entra ID SSO (OIDC auth-code flow via MSAL)
    auth_mode: str = "none"                       # none | password | entra
    session_secret: str = "change-me-in-production"
    session_ttl_hours: int = 12
    # password mode
    app_password: str = ""                        # the shared workspace password
    # entra mode
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    entra_redirect_path: str = "/auth/callback"

    # --- LLM fallback provider (used ONLY for non-standard layouts) -------------
    # Its output is ALWAYS routed to human review; the accuracy guarantee never
    # rests on the model. "none" = deterministic-only (no model calls at all).
    llm_provider: LLMProviderName = "none"
    # one model call per file must finish inside Azure App Service's ~230 s
    # request limit (uploads are sent one file per request)
    llm_timeout_s: int = 180
    # GPT-5-class / o-series models: how hard to think ("minimal", "low",
    # "medium", "high"); blank = the model's default. Ignored by models that
    # don't support it.
    llm_reasoning_effort: str = ""
    llm_temperature: float = 0.5
    llm_max_doc_chars: int = 24000  # document text budget sent to the model
    llm_max_image_pages: int = 4    # scanned pages sent as images (vision models)
    # Who "our" investor is in LP statements, so the model reads that investor's
    # column/row and not the fund total. Comma-separated; matched loosely.
    lp_names: str = "Lam Research Capital LLC, Lam Research"

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

    # Fiscal year-end month (1-12). Default 9 = September: FY2026 runs
    # Oct 2025-Sep 2026, so FY2026 Q1 = Oct-Dec 2025, Q2 = Jan-Mar 2026,
    # Q3 = Apr-Jun 2026, Q4 = Jul-Sep 2026. Set to 12 for a pure calendar
    # year. Every period shown in the UI/ledger/exports carries BOTH the
    # calendar label (e.g. "2026-Q1") and the fiscal label (e.g. "FY2026 Q2").
    fiscal_year_end_month: int = 9

    # --- Server ---
    host: str = "127.0.0.1"  # localhost only — never binds to a public interface
    port: int = 8713

    @property
    def llm_enabled(self) -> bool:
        """True when any real model provider is configured."""
        return self.llm_provider != "none"

    @property
    def sqlalchemy_url(self) -> str:
        """Effective SQLAlchemy URL: explicit DATABASE_URL, else the local SQLite file."""
        return self.database_url or f"sqlite:///{self.db_path}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.processed_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(
                    f"Cannot create data directory {d}. On Azure App Service, "
                    "ensure persistent storage is enabled "
                    "(WEBSITES_ENABLE_APP_SERVICE_STORAGE=true, the default), "
                    "or set DATA_DIR to a writable path.") from e


settings = Settings()
settings.ensure_dirs()
