#!/usr/bin/env python3
"""Aggregate the plan-review `REVIEW_RESULT` sidecar corpus into an impact/severity
distribution report — the repeatable form of the ad-hoc analysis that showed the
verifier's IMPACT axis already discriminates (it is NOT saturated at "critical").

The `REVIEW_RESULT` sidecar (`src/rebar/llm/plan_review/sidecar.py`) persists, per
finding, the computed `impact`/`severity`/`validity`/`priority` plus the raw
`verification.severity_attributes` — on every review, PASS or BLOCK. Those events live
on the `tickets` branch (reducer-ignored, retained), so this is a pure offline read:
it never touches the gate, the model, or the network.

Use it to (a) confirm/refute "everything comes back critical", (b) root-cause severity
inflation by drilling into the raw attribute distributions, and (c) calibrate the
Pass-3 rising-floor threshold against the real distribution rather than a guess.

Usage:
    python scripts/plan_review_impact_distribution.py [TICKETS_ROOT] [--json]

TICKETS_ROOT defaults to `.tickets-tracker` (the git-ignored worktree). `--json` emits
the machine-readable aggregate instead of the text report.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics
import sys
from typing import Any

_ATTR_KEYS = ("prod_impact", "debt_impact", "blast_radius", "likelihood", "reversibility")


def _findings(event: dict[str, Any]) -> list[dict[str, Any]]:
    """The per-finding records out of one REVIEW_RESULT event (payload under `data`)."""
    data = event.get("data") if isinstance(event, dict) else None
    payload = data if isinstance(data, dict) and "findings" in data else event
    out = payload.get("findings") if isinstance(payload, dict) else None
    return out if isinstance(out, list) else []


def aggregate(tickets_root: str) -> dict[str, Any]:
    """Walk every `*REVIEW_RESULT.json` under `tickets_root` and aggregate the
    severity/impact distributions, overall and split by tier (LLM vs DET)."""
    files = sorted(
        glob.glob(os.path.join(tickets_root, "**", "*REVIEW_RESULT.json"), recursive=True)
    )
    sev = collections.Counter()
    tier_sev: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    decision = collections.Counter()
    attrs = {k: collections.Counter() for k in _ATTR_KEYS}
    impacts: list[float] = []
    n_findings = n_llm = n_with_attrs = 0
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, ValueError):
            continue
        for f in _findings(event):
            n_findings += 1
            tier = f.get("tier")
            decision[f.get("decision")] += 1
            sev[f.get("severity")] += 1
            tier_sev[tier][f.get("severity")] += 1
            if isinstance(f.get("impact"), int | float):
                impacts.append(float(f["impact"]))
            if tier == "LLM":
                n_llm += 1
            sa = (f.get("verification") or {}).get("severity_attributes") or {}
            if sa:
                n_with_attrs += 1
                for k in _ATTR_KEYS:
                    if sa.get(k) is not None:
                        attrs[k][sa[k]] += 1
    impact_stats = {}
    if impacts:
        impact_stats = {
            "n": len(impacts),
            "mean": round(statistics.mean(impacts), 4),
            "median": round(statistics.median(impacts), 4),
            "min": min(impacts),
            "max": max(impacts),
            "pct_critical_ge_0.75": round(100 * sum(v >= 0.75 for v in impacts) / len(impacts), 1),
            # Impact PERCENTILES — the reproducible inputs the rising-floor `novelty_priority_floor`
            # default is derived from (child cc5b): the floor is set to ~the p40 impact band (the
            # "below major" cut). Printed so the chosen scalar is auditable, not asserted.
            "percentiles": _percentiles(impacts, (20, 40, 50, 60, 80)),
        }
    return {
        "events": len(files),
        "findings": n_findings,
        "llm_findings": n_llm,
        "findings_with_severity_attributes": n_with_attrs,
        "severity": dict(sev),
        "severity_by_tier": {t: dict(c) for t, c in tier_sev.items()},
        "decision": dict(decision),
        "impact": impact_stats,
        "severity_attributes": {k: dict(c) for k, c in attrs.items()},
    }


def _percentiles(values: list[float], pcts: tuple[int, ...]) -> dict[str, float]:
    """Linear-interpolated percentiles of ``values`` at the given percent points, keyed
    ``pNN``. Used to derive the rising-floor scalar reproducibly (child cc5b)."""
    s = sorted(values)
    out: dict[str, float] = {}
    for p in pcts:
        if len(s) == 1:
            out[f"p{p}"] = round(s[0], 4)
            continue
        rank = (p / 100) * (len(s) - 1)
        lo = int(rank)
        frac = rank - lo
        hi = min(lo + 1, len(s) - 1)
        out[f"p{p}"] = round(s[lo] + (s[hi] - s[lo]) * frac, 4)
    return out


def _pct(counter: dict[str, int]) -> str:
    total = sum(counter.values()) or 1
    return ", ".join(
        f"{lvl}={cnt} ({100 * cnt / total:.0f}%)"
        for lvl, cnt in sorted(counter.items(), key=lambda kv: -kv[1])
    )


def render_text(agg: dict[str, Any]) -> str:
    lines = [
        f"REVIEW_RESULT corpus: {agg['events']} events, {agg['findings']} findings "
        f"({agg['llm_findings']} LLM, {agg['findings_with_severity_attributes']} with attrs)",
        "",
        f"severity (all): {_pct(agg['severity'])}",
    ]
    for tier, c in agg["severity_by_tier"].items():
        lines.append(f"severity [{tier}]: {_pct(c)}")
    if agg["impact"]:
        i = agg["impact"]
        lines += [
            "",
            f"impact: n={i['n']} mean={i['mean']} median={i['median']} "
            f"min={i['min']:.2f} max={i['max']:.2f} critical(>=0.75)={i['pct_critical_ge_0.75']}%",
        ]
        if i.get("percentiles"):
            pcts = ", ".join(f"{k}={v}" for k, v in i["percentiles"].items())
            lines.append(f"impact percentiles: {pcts}  (rising-floor default ~= p40)")
    lines += ["", f"decision: {_pct(agg['decision'])}", "", "severity_attributes (LLM):"]
    for k, c in agg["severity_attributes"].items():
        lines.append(f"  {k}: {_pct(c)}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "tickets_root",
        nargs="?",
        default=".tickets-tracker",
        help="store worktree (default .tickets-tracker)",
    )
    ap.add_argument(
        "--json", action="store_true", help="emit the JSON aggregate instead of the text report"
    )
    args = ap.parse_args(argv)
    agg = aggregate(args.tickets_root)
    if not agg["events"]:
        sys.stderr.write(f"no REVIEW_RESULT sidecar events found under {args.tickets_root!r}\n")
        return 1
    print(json.dumps(agg, indent=2) if args.json else render_text(agg))  # noqa: T201 — CLI report
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
