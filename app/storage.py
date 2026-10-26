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


def load_source(source_key: str | None, filename: str, file_hash: str) -> Path | None:
    """A local path to the stored copy of an uploaded file (for re-reading it),
    or None. Tries the recorded key first, then the stored copies whose name and
    content hash match — older submissions didn't record their key."""
    import hashlib
    import tempfile

    def _sha(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()

    if source_key and not source_key.startswith(("http", "azure")):
        p = Path(source_key)
        if p.exists():
            return p
    if settings.storage_backend == "azure":
        try:
            container = _blob_container_client()
            names = ([source_key.rsplit("/", 1)[-1]] if source_key else []) + sorted(
                (b.name for b in container.list_blobs()
                 if b.name.endswith(f"__{Path(filename).name}")), reverse=True)
            for name in names:
                data = container.download_blob(name).readall()
                if _sha(data) == file_hash:
                    tmp = Path(tempfile.mkdtemp()) / Path(filename).name
                    tmp.write_bytes(data)
                    return tmp
        except Exception:
            return None
        return None
    for p in sorted(settings.upload_dir.glob(f"*__{Path(filename).name}"), reverse=True):
        try:
            if _sha(p.read_bytes()) == file_hash:
                return p
        except OSError:
            continue
    return None
