#!/usr/bin/env bash
# Launch the Portfolio Financials Studio dashboard locally.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8713}"
HOST="${HOST:-127.0.0.1}"
PROVIDER="${LLM_PROVIDER:-none}"

echo "Portfolio Financials Studio — starting on http://${HOST}:${PORT}"
echo "  LLM provider: ${PROVIDER}  (set LLM_PROVIDER=azure|openai|ollama|none)"
if [ "${PROVIDER}" = "none" ]; then
  echo "  Deterministic-only mode: no model, no credentials, nothing leaves this machine."
fi
exec python3 -m uvicorn app.main:app --host "${HOST}" --port "${PORT}"
