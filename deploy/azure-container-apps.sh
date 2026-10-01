#!/usr/bin/env bash
# Deploy Portfolio Financials Studio to Azure Container Apps, wired to the
# customer's Azure OpenAI with a keyless managed identity. Reference script —
# review and adjust names/SKUs for your subscription before running.
#
# Prereqs: az CLI logged in (az login), an existing Azure OpenAI resource with a
# chat deployment, and (recommended) an Azure SQL / PostgreSQL + Storage account.
set -euo pipefail

# ---- edit these ----
RG=rg-portfolio-financials
LOCATION=eastus
ENVIRONMENT=pfs-env
APP=portfolio-financials-studio
ACR=pfsregistry$RANDOM                      # must be globally unique
AOAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com
AOAI_DEPLOYMENT=gpt-4o
DATABASE_URL="mssql+pyodbc://...@YOUR-SERVER.database.windows.net/db?driver=ODBC+Driver+18+for+SQL+Server"
STORAGE_ACCOUNT_URL=https://YOURACCT.blob.core.windows.net
APP_SESSION_SECRET=$(openssl rand -hex 32)
# --------------------

az group create -n "$RG" -l "$LOCATION"
az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled false
az acr build -r "$ACR" -t "$APP:latest" .                 # build image from this repo

az containerapp env create -n "$ENVIRONMENT" -g "$RG" -l "$LOCATION"

# Create the app with a system-assigned managed identity (keyless auth).
az containerapp create \
  -n "$APP" -g "$RG" --environment "$ENVIRONMENT" \
  --image "$ACR.azurecr.io/$APP:latest" \
  --registry-server "$ACR.azurecr.io" --registry-identity system \
  --system-assigned \
  --ingress internal --target-port 8000 \
  --min-replicas 1 --max-replicas 3 \
  --env-vars \
    LLM_PROVIDER=azure \
    AZURE_USE_ENTRA_ID=true \
    AZURE_OPENAI_ENDPOINT="$AOAI_ENDPOINT" \
    AZURE_OPENAI_DEPLOYMENT="$AOAI_DEPLOYMENT" \
    STORAGE_BACKEND=azure \
    AZURE_STORAGE_ACCOUNT_URL="$STORAGE_ACCOUNT_URL" \
    DATABASE_URL="$DATABASE_URL" \
    AUTH_MODE=entra \
    SESSION_SECRET="$APP_SESSION_SECRET"

# Grant the app's managed identity access to Azure OpenAI and Blob Storage.
PRINCIPAL=$(az containerapp show -n "$APP" -g "$RG" --query identity.principalId -o tsv)
SUB=$(az account show --query id -o tsv)
az role assignment create --assignee "$PRINCIPAL" \
  --role "Cognitive Services OpenAI User" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG"
az role assignment create --assignee "$PRINCIPAL" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG"

echo "Deployed. Ingress is INTERNAL — reach it over the corporate VPN / VNet."
echo "Add a Private Endpoint to the Azure OpenAI resource to keep model traffic off the public internet."
