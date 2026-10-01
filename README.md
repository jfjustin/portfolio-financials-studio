# Portfolio Financials Studio

Extract, validate, and review confidential **fund / JV / direct-investment**
financials from **PDF and Excel** reports — replacing manual data entry into
spreadsheets — with a **pluggable model backend** and a strict human-in-the-loop
review step.

Runs **deterministic-only by default**: no model, no credentials, nothing leaves
the machine. When you need help with non-standard layouts, plug in a model
provider — including a **customer's own Microsoft model (Azure OpenAI)** running
inside their Azure tenant, so data never goes to a public vendor.

```
 upload ─▶ extract ─▶ classify + map ─▶ validate ─▶ human review ─▶ trusted data ─▶ dashboard / CSV
          (PDF/Excel)  (entity + metric)  (reconcile)  (confirm/edit)   (audited)
```

This project combines two lineages: the rigor of a deterministic, audit-trailed
extraction pipeline, and the domain model of an investment-intelligence app
(entity classification, capital-account roll-forwards, workbook field mapping).

## Why the numbers are trustworthy

No automated extractor — model or otherwise — can *guarantee* correct figures on
its own. Trust is built in four layers:

1. **Deterministic parsing first.** Standard sheets and native PDFs are parsed by
   exact rules (openpyxl / pdfplumber), never guessed. In multi-sheet workbooks
   the **primary statement sheet** is detected so values aren't pulled off a
   look-alike summary tab.
2. **Validation / reconciliation.** Arithmetic checks run automatically:
   Assets = Liabilities + Net Assets; Income − Expenses = Net Income;
   Equity income = Ownership % × JV Net Income; Fair Value − Cost = Unrealized;
   MOIC = FV / Cost; **capital-account roll-forward** (Beginning + Activity =
   Ending); ownership in 0–100; currency consistency. Anything that fails is
   downgraded to *needs review*.
3. **Human-in-the-loop.** Every low-confidence, flagged, or **model-suggested**
   value is surfaced for a person to confirm or correct. Only `auto`
   (high-confidence + passed checks) and `confirmed` values feed the dashboard
   and export.
4. **Full audit trail.** Every value and every change (old → new, when, note) is
   recorded append-only in SQLite. Money is stored as exact decimals, never floats.

The LLM is used **only as a fallback** for non-standard layouts, and its output is
**always** routed to human review — the guarantee never rests on the model.

## Model providers (pluggable)

Select with the `LLM_PROVIDER` environment variable:

| `LLM_PROVIDER` | Model runs | Data boundary | Use when |
|---|---|---|---|
| `none` *(default)* | — | nothing leaves the machine | demos, fully deterministic runs |
| `azure` | **customer's Azure tenant** | inside their Azure subscription | enterprise / confidential data |
| `openai` | public OpenAI cloud | leaves to OpenAI | quick trials |
| `ollama` | local machine | nothing leaves the machine | fully offline with a model |

### Customer Microsoft model (Azure OpenAI)

Connects to an Azure OpenAI **deployment** in the customer's own subscription, so
prompts/data stay within their Azure boundary and are not shared with OpenAI.

```bash
export LLM_PROVIDER=azure
export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
export AZURE_OPENAI_DEPLOYMENT=your-deployment-name        # deployment, not base model
export AZURE_OPENAI_API_VERSION=2024-10-21
export AZURE_OPENAI_API_KEY=...          # OR keyless: AZURE_USE_ENTRA_ID=true
pip install "openai>=1.40" "azure-identity>=1.17"
./run.sh
```

Keyless (recommended for enterprise): set `AZURE_USE_ENTRA_ID=true` and
authenticate with `azure-identity` (`DefaultAzureCredential`: managed identity,
`az login`, or environment credentials) — no API key stored anywhere.

### Deterministic-only (demo)

```bash
./run.sh            # LLM_PROVIDER defaults to none
```

## Authentication (private workspace)

Set `AUTH_MODE`:

| `AUTH_MODE` | Gate | Use when |
|---|---|---|
| `none` *(default)* | open | local / single user |
| `password` | one shared workspace password (`APP_PASSWORD`) | quick private deployments, demos |
| `entra` | **Microsoft Entra ID SSO** (OIDC auth-code flow via MSAL) | enterprise |

```bash
# shared-password workspace
AUTH_MODE=password APP_PASSWORD='choose-one' SESSION_SECRET=$(openssl rand -hex 32) ./run.sh

# Microsoft Entra ID SSO
AUTH_MODE=entra ENTRA_TENANT_ID=... ENTRA_CLIENT_ID=... ENTRA_CLIENT_SECRET=... ./run.sh
# register the redirect URI <your-host>/auth/callback in the Entra app registration
```

Sessions are signed HttpOnly cookies. Static assets, the login page, and the
health check are the only unauthenticated routes.

## Deploy to Azure (Microsoft-environment target)

For a Microsoft shop (Entra ID + Fabric + Azure), the coherent combo is the
**Azure OpenAI model** + the app on **Azure Container Apps**, both inside the
tenant, reached over the corporate VPN:

- Container image: `Dockerfile` (multi-stage; injects `$PORT`).
- One-shot reference deploy: [`deploy/azure-container-apps.sh`](deploy/azure-container-apps.sh)
  — builds the image, creates a Container App with a **system-assigned managed
  identity**, and grants it `Cognitive Services OpenAI User` + `Storage Blob
  Data Contributor` (keyless; no secrets stored).
- Storage: `DATABASE_URL` → **Azure SQL / PostgreSQL**; `STORAGE_BACKEND=azure`
  → **Blob** for source docs. Both support Entra ID / managed identity.
- Network: deploy with **internal ingress** + a **Private Endpoint** on Azure
  OpenAI, so model traffic stays on the Azure backbone and the app is only
  reachable on the VPN.
- **Microsoft Fabric**: export trusted facts (`/api/export.csv` or the
  `fact`-level tables) into OneLake / a Lakehouse and surface them in Power BI.

```bash
docker build -t portfolio-financials-studio .
docker run -p 8000:8000 -e LLM_PROVIDER=none portfolio-financials-studio   # local container
# full Azure deploy:
bash deploy/azure-container-apps.sh        # edit the names/SKUs at the top first
```

## Report types supported

- **Fund Financials** — assets, liabilities, NAV, income, expenses, contributions, distributions…
- **Joint Venture** — ownership %, JV assets/liabilities/equity, revenue, net income, carrying value
- **JV Equity Income / (Loss)** — equity-method share of JV earnings (auto-recomputed & checked)
- **Direct Investment** — cost, fair value, unrealized/realized, ownership, MOIC, IRR
- **Investment Roll-Forward** — capital-account statements (Beginning → Funding →
  Equity Income/(Loss) → Return of Capital → … → Ending), reconciled end-to-end

Add or edit metrics/synonyms in `app/report_types.py` — the rest of the pipeline adapts.

## Setup

```bash
cd portfolio-financials-studio
pip install -r requirements.txt          # core deps; LLM SDKs are optional
python samples/make_samples.py           # optional: generate demo files
./run.sh                                  # http://127.0.0.1:8713
```

Open **http://127.0.0.1:8713**, drag PDFs/Excel onto the upload area, then click a
submission to review, confirm, and lock in the values.

## What's in the box

```
app/
  report_types.py     canonical report types, metrics, synonym maps
  models.py           data models + robust number/currency parsing + label→metric mapping
  extractors/
    base.py           period normalization, entity guessing, document classification
    excel.py          deterministic .xlsx/.xlsm/.xls extraction
    pdf_native.py     deterministic native-PDF extraction + scanned-file detection
    registry.py       routing + assembly + primary-sheet selection + auto/needs-review policy
  llm/                pluggable model layer (never auto-accepted)
    base.py           provider interface + shared prompt/JSON handling + NoOp (deterministic)
    azure_openai.py   customer Microsoft model (Azure OpenAI)
    openai_cloud.py   public OpenAI cloud
    ollama.py         local, on-machine model
  validate.py         reconciliation / sanity checks
  pipeline.py         orchestrator (ingest → extract → validate → store)
  db.py               SQLAlchemy Core storage + append-only audit (SQLite / Azure SQL / Postgres)
  storage.py          source-document storage (local folder or Azure Blob)
  auth.py             AuthMiddleware + password gate / Entra ID SSO (optional)
  main.py             FastAPI app + JSON API + auth routes
templates/            index.html (dashboard) + login.html (private-workspace gate)
static/               dashboard UI (upload, review, offline SVG charts)
Dockerfile, deploy/   container image + Azure Container Apps deploy script
samples/              demo report generator
```

## Data & privacy

- All data in `data/` (SQLite DB + copies of uploaded source files). Back this
  folder up; it contains confidential financials.
- Server binds to `127.0.0.1` only. No telemetry, no external fonts/CDNs/scripts.
- In `azure`/`openai` modes, only the extracted text slice needed for a fallback
  is sent to the configured endpoint, and only for non-standard layouts.

## Known limitations / roadmap

- **One value per metric per statement.** When several source lines collapse into
  one canonical metric (e.g. multiple expense lines → "Other"), only one is kept
  and the roll-forward reconciliation surfaces the gap for review. Line-level
  aggregation is a planned enhancement.
- **OCR for scanned PDFs** — currently detected and flagged, not processed. Add a
  local OCR step (Tesseract/PaddleOCR) in `extractors/pdf_native.py`.
- Multi-user auth (single local user today); period-over-period trend charts.
