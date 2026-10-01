# Portfolio Financials Studio — production container image.
# Multi-stage: install deps into a venv, then copy into a slim runtime.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /app

# System deps: the msodbcsql18 driver is only needed for Azure SQL (mssql+pyodbc).
# PostgreSQL (postgresql+psycopg) and SQLite need no extra system packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Core + cloud extras (Azure OpenAI/Blob/Entra, Postgres driver). Installed here
# so the image is deploy-ready; unused providers simply stay idle.
RUN pip install -r requirements.txt \
    && pip install "psycopg[binary]>=3.1" "azure-storage-blob>=12.19"

COPY . .

# Drop privileges
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Container Apps / App Service inject $PORT; default 8000 for plain `docker run`.
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
