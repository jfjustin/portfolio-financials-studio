"""Carry-forward check: each quarter's roll-forward starts where the last one ended.

Capital-account statements are frequently year-to-date and can repeat stale
figures — a Q2 statement that still prints "Beginning balance as of January 1"
(Q1's beginning) and Jan–Jun cumulative activity. Recording those as-is gives a
Q2 row that doesn't follow on from Q1. Before facts are saved, every
roll-forward upload is checked against the entity's earlier quarters:

  1. Beginning = previous quarter's ending, whenever the document says otherwise.
  2. Year-to-date activity is de-cumulated (YTD − the year's earlier quarters).
  3. The quarter must reconcile (beginning + activity = ending); any residual is
     recorded in "Other" as a flagged reconciling plug.

Every adjusted value keeps the document's figure in `reported_value`, gets a
flag saying why it changed, and goes to human review. Confirmed facts are never
re-derived.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from . import db
from .extractors.base import (is_quarter_label, next_quarter, previous_quarter,
                              quarter_bounds, quarter_label)
from .models import ExtractionResult, FactStatus, FinancialFact, is_since_inception_label
from .report_types import REPORT_TYPES, Unit

RF = "investment_roll_forward"
# period activity — same set validate.py sums against (ending − beginning)
FLOW_KEYS = ("funding", "equity_income_loss", "return_of_capital", "dividends", "other",
             "investment_mtm", "sale_of_investment", "net_realized_gain",
             "accrued_interest", "note_conversion", "gain_loss_on_conversion",
             "acquisition_impact", "impairment")
DERIVED_FLAGS = ("carried_forward", "ytd_decumulated", "reconciling_plug",
                 "prior_quarter_missing", "derived_from_activity")
TOL = Decimal(1)


@dataclass
class Derivation:
    values: dict[str, Decimal] = field(default_factory=dict)   # metric -> recorded value
    flags: dict[str, list[str]] = field(default_factory=dict)  # metric -> derivation flags
    notes: dict[str, str] = field(default_factory=dict)        # metric -> why it changed
    warnings: list[str] = field(default_factory=list)

    def set(self, metric: str, value: Decimal, flag: str, note: str) -> None:
        self.values[metric] = value
        self.flag(metric, flag)
        self.notes[metric] = note

    def flag(self, metric: str, flag: str) -> None:
        if flag not in self.flags.setdefault(metric, []):
            self.flags[metric].append(flag)


def _fmt(v: Optional[Decimal]) -> str:
    return "—" if v is None else f"{v:,.0f}"


def derive(quarter: str, statement_start: Optional[date],
           reported: dict[str, Decimal],
           priors: dict[str, dict[str, Decimal]]) -> Derivation:
    """Work out the values to record for one quarter of one entity.

    `reported` is what the document says; `priors` holds the entity's other
    recorded quarters ({"2026-Q1": {metric: value}}).
    """
    d = Derivation()
    q_start, _ = quarter_bounds(quarter)
    prev = previous_quarter(quarter)
    prev_m = priors.get(prev, {})
    prev_end = prev_m.get("ending_balance")
    beg_rep = reported.get("beginning_balance")

    # Is the statement year-to-date? Either it says its period started before
    # this quarter did, or its beginning just repeats the prior quarter's
    # beginning instead of following on from that quarter's ending.
    ytd_start: Optional[date] = None
    if statement_start and statement_start < q_start:
        ytd_start = statement_start
    elif (beg_rep is not None and prev_m.get("beginning_balance") is not None
          and abs(beg_rep - prev_m["beginning_balance"]) <= TOL
          and (prev_end is None or abs(prev_end - beg_rep) > TOL)):
        ytd_start = quarter_bounds(prev)[0]

    covered: list[str] = []   # earlier quarters folded into a YTD statement
    if ytd_start:
        ql = quarter_label(ytd_start)
        while ql != quarter and len(covered) < 8:
            covered.append(ql)
            ql = next_quarter(ql)
    missing = [q for q in covered if q not in priors]

    # 1) beginning = previous quarter's ending
    beg = beg_rep
    if prev_end is not None:
        if beg_rep is None or abs(beg_rep - prev_end) > TOL:
            beg = prev_end
            d.set("beginning_balance", prev_end, "carried_forward",
                  f"carried forward from {prev} ending {_fmt(prev_end)}"
                  + (f"; document printed {_fmt(beg_rep)}" if beg_rep is not None else ""))
    elif beg_rep is None and not ytd_start and reported.get("ending_balance") is not None \
            and any(k in reported for k in FLOW_KEYS):
        # since-inception statements print the quarter's activity and the
        # closing balance but no opening one: opening = closing − activity
        acts = sum((reported[k] for k in FLOW_KEYS if k in reported), Decimal(0))
        beg = reported["ending_balance"] - acts
        d.set("beginning_balance", beg, "derived_from_activity",
              f"not printed — ending {_fmt(reported['ending_balance'])} less quarter "
              f"activity {_fmt(acts)}; {prev} not on file to carry forward")
    elif ytd_start and beg_rep is not None:
        d.flag("beginning_balance", "prior_quarter_missing")
        d.warnings.append(
            f"{prev} is not on file — {quarter} beginning taken from the document "
            f"(as of {ytd_start.isoformat()}); upload {prev} to roll it forward.")

    # 2) de-cumulate year-to-date activity
    decumulated = False
    if ytd_start:
        if missing:
            for k in FLOW_KEYS:
                if k in reported:
                    d.flag(k, "prior_quarter_missing")
            d.warnings.append(
                f"Statement is year-to-date from {ytd_start.isoformat()} but "
                f"{', '.join(missing)} not on file — activity is still cumulative.")
        else:
            for k in FLOW_KEYS + ("change",):
                if k in reported:
                    earlier = sum((priors[q].get(k, Decimal(0)) for q in covered), Decimal(0))
                    d.set(k, reported[k] - earlier, "ytd_decumulated",
                          f"year-to-date {_fmt(reported[k])} less {', '.join(covered)} "
                          f"{_fmt(earlier)}")
            decumulated = True

    # 3) reconcile beginning + activity = ending
    end = reported.get("ending_balance")
    derived_only = d.flags.get("beginning_balance") == ["derived_from_activity"]
    if (("beginning_balance" in d.values and not derived_only) or decumulated) \
            and beg is not None and end is not None:
        flows = {k: d.values.get(k, reported[k]) for k in FLOW_KEYS if k in reported}
        resid = end - beg - sum(flows.values(), Decimal(0))
        if abs(resid) > TOL:
            other = flows.get("other", Decimal(0)) + resid
            d.set("other", other, "reconciling_plug",
                  f"includes {_fmt(resid)} reconciling plug so {_fmt(beg)} + activity "
                  f"= ending {_fmt(end)}")
    return d


def _input_guards(facts: list[FinancialFact]) -> None:
    """Flag inputs that look like trash rather than period activity."""
    balances = {f.value for f in facts if f.value is not None and abs(f.value) >= 1000
                and f.metric_key not in FLOW_KEYS}
    for f in facts:
        if f.metric_key not in FLOW_KEYS or f.value is None:
            continue
        if is_since_inception_label(f.raw_label or ""):
            f.flags.append("since_inception_in_period_field")
            f.status = FactStatus.NEEDS_REVIEW
        elif abs(f.value) >= 1000 and f.value in balances:
            f.flags.append("duplicate_value_suspect")
            f.status = FactStatus.NEEDS_REVIEW


def apply_to_result(result: ExtractionResult) -> None:
    """Run the carry-forward check on a fresh extraction, before it is saved."""
    if (result.detected_report_type != RF or not is_quarter_label(result.period)
            or result.entity_name == "UNKNOWN"):
        return
    _input_guards(result.facts)
    priors = db.quarter_metrics(result.entity_name, RF)
    priors.pop(result.period, None)
    reported = {f.metric_key: f.value for f in result.facts
                if f.value is not None and f.status != FactStatus.REJECTED}
    start = date.fromisoformat(result.statement_start) if result.statement_start else None
    d = derive(result.period, start, reported, priors)

    by_key = {f.metric_key: f for f in result.facts}
    for mkey in set(d.values) | set(d.flags):
        f = by_key.get(mkey)
        if f is None and mkey in d.values:
            spec = REPORT_TYPES[RF].metric_by_key(mkey)
            f = FinancialFact(
                entity_name=result.entity_name, entity_kind=REPORT_TYPES[RF].entity_kind,
                report_type=RF, period=result.period, metric_key=mkey,
                metric_label=spec.label, unit=Unit.CURRENCY, currency=result.currency,
                provenance="carry_forward", confidence=0.9)
            result.facts.append(f)
            by_key[mkey] = f
        if f is None:
            continue
        if mkey in d.values:
            if f.value is not None and f.reported_value is None and "carry_forward" not in f.provenance:
                f.reported_value = f.value
            f.value = d.values[mkey]
            f.provenance = f"{f.provenance} | {d.notes[mkey]}"
        f.flags.extend(fl for fl in d.flags.get(mkey, []) if fl not in f.flags)
        f.status = FactStatus.NEEDS_REVIEW
    result.warnings.extend(d.warnings)


def rederive_following(entity: str, period: str) -> int:
    """Re-run the check on the quarters after `period` (e.g. Q1 arrived after Q2).

    Only unconfirmed facts change. Returns how many facts were updated.
    """
    if not is_quarter_label(period):
        return 0
    changed = 0
    q = period
    for _ in range(12):
        q = next_quarter(q)
        rows = [r for r in db.facts_for_quarter(entity, RF, q) if r["status"] != "rejected"]
        if not rows:
            break
        reported: dict[str, Decimal] = {}
        for r in rows:
            if r["reported_value"] is not None:
                reported[r["metric_key"]] = Decimal(r["reported_value"])
            elif r["value"] is not None and not (
                    not r["raw_value"]
                    and {"carried_forward", "derived_from_activity"} & set(r["flags"])):
                # values this check derived itself (nothing printed) aren't "reported"
                reported[r["metric_key"]] = Decimal(r["value"])
        priors = db.quarter_metrics(entity, RF)
        priors.pop(q, None)
        start = next((r["statement_start"] for r in rows if r["statement_start"]), None)
        d = derive(q, date.fromisoformat(start) if start else None, reported, priors)

        have = {r["metric_key"] for r in rows}
        for r in rows:
            if r["status"] == "confirmed":
                continue
            mk = r["metric_key"]
            new_val = d.values.get(mk, reported.get(mk))
            kept = [f for f in r["flags"] if f not in DERIVED_FLAGS]
            new_flags = kept + d.flags.get(mk, [])
            old_val = Decimal(r["value"]) if r["value"] is not None else None
            if new_val == old_val and new_flags == r["flags"]:
                continue
            base_prov = r["provenance"].split(" | ")[0]
            db.set_derived_fact(
                r["id"], value=new_val, flags=new_flags,
                reported_value=(reported.get(mk) if mk in d.values and r["raw_value"]
                                else None),
                status="needs_review" if d.flags.get(mk) else r["status"],
                provenance=f"{base_prov} | {d.notes[mk]}" if mk in d.notes else base_prov,
                note=f"re-derived after {period} was recorded")
            changed += 1
        for mk in set(d.values) - have:
            spec = REPORT_TYPES[RF].metric_by_key(mk)
            db.add_fact(rows[0]["submission_id"], rows[0], metric_key=mk,
                        metric_label=spec.label, value=d.values[mk],
                        flags=d.flags.get(mk, []),
                        provenance=f"carry_forward | {d.notes[mk]}",
                        note=f"derived after {period} was recorded")
            changed += 1
    return changed
