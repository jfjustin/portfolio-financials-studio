# Demo recording tools

Scripts used to produce the GIFs in [`docs/demo/`](../../docs/demo/). They drive
a real browser (Playwright) through the full workflow and screenshot key
states, then stitch the screenshots into a captioned, optimized GIF.

**Not included:** the actual source report files used in the original
recording (real, if partially redacted, fund/company filings) — kept out of
this public repo on purpose. Point the scripts at your own reports, or at the
synthetic fixtures in [`samples/`](../../samples/) (see `samples/make_samples.py`).

## Setup

```bash
pip install playwright Pillow
python3 -m playwright install chromium
```

## Record the deterministic variant (local, no model)

```bash
cd portfolio-financials-studio
rm -f data/pipeline.db                      # start from a clean slate
AUTH_MODE=none LLM_PROVIDER=none ./run.sh &  # or uvicorn directly

# edit the FILES list at the top of record_workflow.py to point at your
# own sample reports first
python3 tools/demo/record_workflow.py deterministic http://127.0.0.1:8713 /tmp/frames_det
python3 tools/demo/make_gif.py /tmp/frames_det docs/demo/deterministic_demo.gif deterministic
```

## Record the Azure OpenAI variant (against a live deployment)

Requires a deployment already configured with `LLM_PROVIDER=azure` and a real
Azure OpenAI endpoint/deployment (see README → *Model providers*). Two options:

- **From a clean/empty deployment:** use `record_workflow.py azure <url> <out_dir>`
  (same script, same FILES list) — it uploads fresh and captures the full
  upload → LLM-fallback → review arc.
- **Against a deployment that already has data loaded** (e.g. from a prior
  run): use `record_azure_variant.py <url> <out_dir>` instead — it skips the
  upload step and opens the first fund/company with an `llm_suggested` fact it
  finds, so you don't hit duplicate-file detection on a second run.

```bash
python3 tools/demo/record_azure_variant.py https://your-app.azurewebsites.net /tmp/frames_az
python3 tools/demo/make_gif.py /tmp/frames_az docs/demo/azure_demo.gif azure
```

## Wiping a live Azure deployment's DB before recording

See **GUIDE.md → "Managing the database"** for the exact commands (Kudu VFS
delete + restart). Recording against a populated deployment is also fine —
`record_azure_variant.py` is built for exactly that case.

## Customizing captions / which frames are included

Edit the `DETERMINISTIC_PLAN` / `AZURE_PLAN` lists in `make_gif.py` — each
entry is `(frame_label, hold_ms, caption)`. A label with no matching
screenshot is silently skipped, so it's safe to add entries speculatively.
