"""Provider demo — run the same non-standard document through each LLM backend.

Shows, side by side, what `none | azure | openai | ollama` do with a layout the
deterministic extractors can't map (a GP-letter narrative, no statement table).

    python samples/provider_demo.py                 # all four, live (unconfigured ones report status)
    python samples/provider_demo.py --mock          # all four, offline, canned model output
    python samples/provider_demo.py -p ollama       # one provider, live

`--mock` swaps only the network transport (fake OpenAI client / fake requests.post);
prompt building, JSON parsing and normalization are the real code paths.

Every returned value also gets a grounding check: does the number literally
appear in the source text? Ungrounded values are the hallucination signal — they
are still only suggestions (confidence 0.5, needs_review), never auto-accepted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.llm import get_provider  # noqa: E402
from app.llm.base import build_prompt  # noqa: E402

PROVIDERS = ["none", "azure", "openai", "ollama"]
REPORT_TYPE = "fund_financials"

# A layout the deterministic parser can't map: numbers live in prose, labels are
# paraphrased ("holdings aggregated" instead of "Total Assets").
DOC = """Harborview Growth Partners III, L.P. — Letter to Limited Partners
For the quarter ended December 31, 2025. All amounts in USD.

Dear Partners,
At quarter end the Partnership's holdings aggregated $148,250,000, against
obligations of $12,400,000, leaving partners' capital of $135,850,000.
During the quarter we called $9,000,000 from partners and returned
$4,200,000 in distributions. The management fee for the period was $612,500.
Net increase in net assets from operations was $6,340,000, driven primarily by
a change in unrealized appreciation of $5,100,000.
"""

# Canned model reply used by --mock. Includes one deliberately ungrounded value
# (realized_gain) so the grounding check has something to catch.
MOCK_REPLY = {
    "entity_name": "Harborview Growth Partners III, L.P.",
    "period": "December 31, 2025",
    "currency": "USD",
    "total_assets": {"value": "148,250,000", "source_label": "holdings aggregated"},
    "total_liabilities": {"value": "12,400,000", "source_label": "obligations"},
    "nav": {"value": "135,850,000", "source_label": "partners' capital"},
    "capital_called": {"value": "9,000,000", "source_label": "called"},
    "distributions": {"value": "4,200,000", "source_label": "distributions"},
    "management_fees": {"value": "612,500", "source_label": "management fee"},
    "net_income": {"value": "6,340,000", "source_label": "net increase in net assets"},
    "unrealized_gain": {"value": "5,100,000", "source_label": "change in unrealized appreciation"},
    "realized_gain": {"value": "1,240,000", "source_label": "realized gain"},  # not in DOC
}


# ---------------------------------------------------------------------------
def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s)


def grounded(value: str, text: str) -> bool:
    d = _digits(value)
    return bool(d) and d in {_digits(m) for m in re.findall(r"[\d][\d,\.]*", text)}


def reset_provider(name: str) -> None:
    settings.llm_provider = name
    get_provider.cache_clear()


def fake_openai_client():
    reply = json.dumps(MOCK_REPLY)
    create = lambda **kw: SimpleNamespace(  # noqa: E731
        choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def fake_ollama_post(url, json=None, timeout=None):  # noqa: A002
    import json as _j
    return SimpleNamespace(raise_for_status=lambda: None,
                           json=lambda: {"response": "<think>scan</think>" + _j.dumps(MOCK_REPLY)})


def run_one(name: str, use_mock: bool) -> dict:
    reset_provider(name)
    prov = get_provider()
    patches = []
    if use_mock and name in ("azure", "openai"):
        prov._client, prov._init_error = fake_openai_client(), None
    if use_mock and name == "ollama":
        patches = [mock.patch("app.llm.ollama.requests.post", fake_ollama_post),
                   mock.patch.object(type(prov), "available", lambda self: True)]
    for p in patches:
        p.start()
    try:
        status = prov.status()
        t0 = time.perf_counter()
        result = prov.extract(DOC, REPORT_TYPE)
        ms = (time.perf_counter() - t0) * 1000
    finally:
        for p in patches:
            p.stop()
    return {"name": name, "status": status, "result": result, "ms": ms}


def boundary(name: str) -> str:
    return {"none": "nothing leaves machine",
            "azure": "customer Azure tenant",
            "openai": "public OpenAI cloud",
            "ollama": "nothing leaves machine"}[name]


def report(r: dict) -> None:
    name, res = r["name"], r["result"]
    bar = "=" * 72
    print(f"\n{bar}\nPROVIDER: {name:<8}  boundary: {boundary(name)}  "
          f"latency: {r['ms']:.0f} ms\n{bar}")
    st = {k: v for k, v in r["status"].items() if v is not None}
    print("status  :", st)
    if res.get("_error"):
        print("result  : UNAVAILABLE —", res["_error"])
        print("routing : pipeline logs warning; required metrics stay missing -> needs_review")
        return
    fields = res.get("fields", {})
    if not fields:
        print("result  : no suggestions (deterministic-only)")
        print("routing : required metrics missing -> whole submission needs_review")
        return
    print(f"entity  : {res.get('entity_name')}  | period: {res.get('period')}  "
          f"| ccy: {res.get('currency')}")
    print(f"{'metric':<18}{'value':>14}  {'grounded':<9} label")
    for k, v in fields.items():
        g = "yes" if grounded(v["value"], DOC) else "NO  <-"
        print(f"{k:<18}{v['value']:>14}  {g:<9} {v['source_label']}")
    n_bad = sum(not grounded(v["value"], DOC) for v in fields.values())
    print(f"routing : {len(fields)} values -> needs_review (conf 0.5, flag llm_suggested); "
          f"{n_bad} ungrounded -> reject in review")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--provider", choices=PROVIDERS)
    ap.add_argument("--mock", action="store_true", help="offline canned transport")
    ap.add_argument("--show-prompt", action="store_true")
    a = ap.parse_args()

    if a.show_prompt:
        print(build_prompt(DOC, REPORT_TYPE))
        return
    print(f"Document: GP letter, non-standard layout ({len(DOC)} chars)  "
          f"mode: {'MOCK transport' if a.mock else 'LIVE'}")
    for name in ([a.provider] if a.provider else PROVIDERS):
        report(run_one(name, a.mock))


if __name__ == "__main__":
    main()
