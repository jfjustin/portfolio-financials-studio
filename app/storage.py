"""Object storage for uploaded source documents.

Keeps an immutable copy of every ingested file so each extracted value can be
traced back to its source. Two backends, chosen by `settings.storage_backend`:

  local  -> copy under data/uploads (default; nothing leaves the machine)
  azure  -> Azure Blob Storage container in the customer's tenant

The Azure backend authenticates with a connection string if provided, otherwise
with Microsoft Entra ID via `azure-identity` (DefaultAzureCredential / managed
identity) — keyless, the enterprise-preferred path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import settings


def _stamp(name: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}__{Path(name).name}"


def store_source(raw_bytes: bytes, filename: str) -> str:
    """Persist a copy of an uploaded file. Returns a storage key/URI (best-effort).

    Never raises on failure — keeping the source copy must not block an otherwise
    successful extraction.
    """
    key = _stamp(filename)
    if settings.storage_backend == "azure":
        try:
            return _store_azure(raw_bytes, key)
        except Exception:
            return ""  # extraction already succeeded; copy is best-effort
    # local
    try:
        settings.ensure_dirs()
        dest = settings.upload_dir / key
        dest.write_bytes(raw_bytes)
        return str(dest)
    except Exception:
        return ""


def _blob_container_client():
    from azure.storage.blob import BlobServiceClient
    if settings.azure_storage_connection_string:
        svc = BlobServiceClient.from_connection_string(
            settings.azure_storage_connection_string)
    else:
        from azure.identity import DefaultAzureCredential
        svc = BlobServiceClient(
            account_url=settings.azure_storage_account_url,
            credential=DefaultAzureCredential())
    container = svc.get_container_client(settings.azure_storage_container)
    try:
        container.create_container()
    except Exception:
        pass  # already exists
    return container


def _store_azure(raw_bytes: bytes, key: str) -> str:
    container = _blob_container_client()
    container.upload_blob(name=key, data=raw_bytes, overwrite=True)
    base = (settings.azure_storage_account_url.rstrip("/")
            if settings.azure_storage_account_url else "azure")
    return f"{base}/{settings.azure_storage_container}/{key}"
