"""Stitch a sequence of PNG frames into an optimized, captioned animated GIF."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FRAMES_DIR = Path(sys.argv[1])
OUT_PATH = Path(sys.argv[2])
VARIANT = sys.argv[3] if len(sys.argv) > 3 else "deterministic"

DETERMINISTIC_PLAN = [
    ("landing_empty",         1100, "Start: empty dashboard, deterministic mode (no model)"),
    ("upload_dropzone",       900,  "Upload fund & company reports (PDF / XLSX / CSV)"),
    ("upload_results",        2400, "Deterministic extraction — per-file reconciliation checks"),
    ("submissions_table",     1300, "Every submission tracked, nothing silently trusted"),
    ("review_sectioned",      2200, "Review: sectioned by Assets / Income / Distributions"),
    ("modification_editing",  1300, "Human edits a value in place"),
    ("modification_saved",    1300, "Edit saved — audit trail records old -> new"),
    ("human_confirm_one",     1300, "Reviewer confirms a flagged item"),
    ("human_confirm_all",     1500, "Confirm all & lock in"),
    ("performance_strip",     1800, "Portfolio performance: TVPI / DPI / IRR"),
    ("direct_investments",    1800, "Direct Investments — cost / FMV / MOIC"),
    ("operating_metrics",     1800, "Direct Investments — operating metrics"),
    ("fund_investments",      1800, "Fund Investments — funding, distributions, QoQ"),
    ("value_over_time",       1800, "Portfolio value over time"),
    ("investment_portfolio",  1800, "Investment portfolio + quarterly roll-forward"),
    ("grand_ledger_filtered", 1800, "Grand ledger — every fact, every company, filterable"),
    ("export_links",          2000, "Export: trusted CSV, Fabric Parquet, schema"),
    ("final_overview",        1400, "Full workflow, start to finish — no model required"),
]

AZURE_PLAN = [
    ("landing_populated",     1300, "Same tool — now connected to the customer's Azure OpenAI deployment"),
    ("submissions_table",     1300, "7 reports ingested, deterministic extraction runs first"),
    ("review_sectioned",      2000, "A required field is genuinely missing — deterministic alone can't fill it"),
    ("review_llm_suggestion", 2600, "Azure OpenAI suggests 3 values — flagged llm_suggested, capped at 50% confidence"),
    ("modification_editing",  1500, "Human overwrites the AI's suggestion directly"),
    ("modification_saved",    1300, "Correction saved — audit trail records the AI value -> human value"),
    ("human_confirm_llm_value", 1500, "Reviewer confirms the (now human-verified) field"),
    ("human_confirm_all",     1500, "Confirm all & lock in"),
    ("performance_strip",     2200, "TVPI / DPI / NAV now compute — the AI-filled gap unblocked the math"),
    ("direct_investments",    1700, "Direct Investments — cost / FMV / MOIC"),
    ("operating_metrics",     1700, "Direct Investments — operating metrics"),
    ("fund_investments",      2000, "Fund Investments — Fund 11's AI-assisted funding now flows through"),
    ("value_over_time",       1700, "Portfolio value over time"),
    ("investment_portfolio",  1700, "Investment portfolio + quarterly roll-forward"),
    ("grand_ledger_filtered", 1700, "Grand ledger — every fact, every company, filterable"),
    ("export_links",          1900, "Export: trusted CSV, Fabric Parquet, schema"),
    ("final_overview",        1500, "AI only as a reviewed fallback — the guarantee never rests on the model"),
]

PLAN = AZURE_PLAN if VARIANT == "azure" else DETERMINISTIC_PLAN

def find_frame(label):
    matches = sorted(FRAMES_DIR.glob(f"*_{label}.png"))
    return matches[0] if matches else None

def load_font(size):
    for name in ("/System/Library/Fonts/Helvetica.ttc",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()

CAPTION_H = 54
FONT = load_font(22)

frames = []
durations = []
for label, dur, caption in PLAN:
    fp = find_frame(label)
    if fp is None:
        continue
    img = Image.open(fp).convert("RGB")
    # downscale for a reasonable gif size
    scale = 760 / img.width
    img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    canvas = Image.new("RGB", (img.width, img.height + CAPTION_H), (18, 26, 43))
    canvas.paste(img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((16, img.height + 15), caption, fill=(255, 255, 255), font=FONT)
    frames.append(canvas)
    durations.append(dur)

if not frames:
    print("NO FRAMES FOUND"); sys.exit(1)

# pad all frames to the same size (max dims) so GIF doesn't distort
max_w = max(f.width for f in frames)
max_h = max(f.height for f in frames)
padded = []
for f in frames:
    c = Image.new("RGB", (max_w, max_h), (18, 26, 43))
    c.paste(f, (0, 0))
    padded.append(c.convert("P", palette=Image.ADAPTIVE, colors=200))

padded[0].save(
    OUT_PATH, save_all=True, append_images=padded[1:], duration=durations,
    loop=0, optimize=True, disposal=2,
)
print(f"wrote {OUT_PATH} ({len(padded)} frames, {OUT_PATH.stat().st_size/1024/1024:.2f} MB)")
