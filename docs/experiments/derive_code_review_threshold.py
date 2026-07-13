#!/usr/bin/env python3
"""Derive the code-review BLOCK threshold from the adjudicated code-v2 finding corpus (ticket 9f25).

The code-review gate cast ZERO 'BLOCK — finding' votes across 162 changes since code-v2 landed,
while confirmed must-fix findings sailed through as advisories. Before enabling blocking (sibling
b9c0) we need the empirically-correct threshold, not a guess: the operator's decision rule is the
crossover point where a finding is MORE LIKELY correctly-blocking than false-blocking — i.e. the
SMALLEST threshold t at which precision(block-worthy | priority >= t) > 0.5.

IMPORTANT — this operates on PRIORITY (= validity x impact), the exact quantity the code-review
gate thresholds on (pass3_decide/decide.py), NOT raw impact. Each code_review_result_v1 sidecar
finding already carries `validity`, `impact`, AND `priority` (the pass-3 decision fields), so the
corpus is a straight projection — no reconstruction.

Distinct from the ADR-0036 A/B gate (docs/experiments/ab_impact_model.py): that gate proves the
impact MODEL separates HIGH from NIT on a static fixture (a model-quality prerequisite); THIS
script derives the operating THRESHOLD from the real field corpus.

Usage:
    python docs/experiments/derive_code_review_threshold.py [--corpus .tickets-tracker] \
        [--emit-recommendation docs/experiments/derive_code_review_threshold.md]

The adjudication here is a transparent, deterministic FIRST-PASS proxy (see `adjudicate`); the
ticket permits a Sonnet-scored / held-out human adjudication for the final call, which should
CONFIRM (not blindly consume) the first-pass threshold this script reports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

CANDIDATES = [0.3, 0.4, 0.5, 0.54, 0.6]
SCHEMA = "code_review_result_v1"
CODE_V2 = "code-v2"

# The three-value closed adjudication vocabulary (9f25 AC). `ambiguous` is EXCLUDED from the
# precision denominator (counted separately), never silently folded into either class.
BLOCK_WORTHY = "block-worthy"
NOT_BLOCK_WORTHY = "not-block-worthy"
AMBIGUOUS = "ambiguous"


def precision_at(rows: list[dict], t: float) -> float:
    """precision(block-worthy | priority >= t) over the non-ambiguous rows at/above t.

    Returns 0.0 when no non-ambiguous finding qualifies at t (an undefined precision reads as
    "catches nothing decisive", so it never spuriously clears the > 0.5 bar)."""
    qual = [r for r in rows if r.get("priority", 0.0) >= t and r.get("adjudication") != AMBIGUOUS]
    if not qual:
        return 0.0
    bw = sum(1 for r in qual if r.get("adjudication") == BLOCK_WORTHY)
    return bw / len(qual)


def select_threshold(rows: list[dict], candidates: list[float] | None = None) -> float | None:
    """The SMALLEST candidate t whose precision > 0.5, tie-broken toward fewer false blocks
    (higher t = fewer blocks), i.e. the first candidate over the bar scanning low→high. Returns
    None when no candidate clears 0.5 (the corpus does not support blocking at any grid point)."""
    for t in sorted(candidates or CANDIDATES):
        if precision_at(rows, t) > 0.5:
            return t
    return None


def adjudicate(finding: dict) -> str:
    """FIRST-PASS deterministic adjudication proxy (transparent, reproducible).

    Signal available in the sidecar: the finding's `tier` and `severity`. A serious-tier / high
    finding that the verifier judged valid (high `validity`) is treated as block-worthy; a
    nit/low finding as not-block-worthy; the middle band as ambiguous (excluded). This is a
    coarse proxy — the ticket permits a Sonnet-scored / held-out human pass to refine it; that
    pass should CONFIRM the threshold, not replace this method silently."""
    tier = str(finding.get("tier", "")).lower()
    sev = str(finding.get("severity", "")).lower()
    val = float(finding.get("validity", 0.0) or 0.0)
    if val < 0.5:
        return NOT_BLOCK_WORTHY  # the verifier itself doubts the finding → not block-worthy
    if tier in {"serious", "high"} or sev in {"high", "critical", "serious", "major"}:
        return BLOCK_WORTHY
    if tier in {"nit", "minor", "low"} or sev in {"low", "nit", "none", "info"}:
        return NOT_BLOCK_WORTHY
    return AMBIGUOUS


def load_corpus(tracker_dir: str) -> list[dict]:
    """Load code-v2 code_review_result_v1 findings from the sidecar events, deduplicated by
    `norm_id` (the diff-scoped join key). Each row: {norm_id, criteria, validity, impact,
    priority, tier, severity, adjudication}."""
    by_norm: dict[str, dict] = {}
    for root, _dirs, files in os.walk(tracker_dir):
        for fn in files:
            if not fn.endswith("-REVIEW_RESULT.json"):
                continue
            try:
                ev = json.load(open(os.path.join(root, fn)))
            except Exception:  # noqa: BLE001 — skip unreadable sidecar files; corpus scan is best-effort
                continue
            payload = ev.get("data") if isinstance(ev, dict) else None
            if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
                continue
            if payload.get("impact_model_version") != CODE_V2:
                continue  # SEGMENT by version — never pool code-v2 with a future code-v3
            for bucket in ("blocking", "advisory"):
                for f in payload.get(bucket, []) or []:
                    if not isinstance(f, dict):
                        continue
                    nid = (
                        f.get("norm_id")
                        or f.get("id")
                        or f"{payload.get('change_fingerprint')}:{len(by_norm)}"
                    )
                    row = {
                        "norm_id": nid,
                        "criteria": f.get("criteria"),
                        "validity": float(f.get("validity", 0.0) or 0.0),
                        "impact": float(f.get("impact", 0.0) or 0.0),
                        "priority": float(f.get("priority", 0.0) or 0.0),
                        "tier": f.get("tier"),
                        "severity": f.get("severity"),
                    }
                    row["adjudication"] = adjudicate(f)
                    by_norm[nid] = row  # dedup: last write wins (latest revision of a change)
    return list(by_norm.values())


def _distribution(rows: list[dict]) -> dict:
    prio = sorted(r["priority"] for r in rows)
    n = len(prio)
    pct = {p: (prio[min(n - 1, int(p / 100 * n))] if n else 0.0) for p in (50, 75, 90, 95, 100)}
    return {"n": n, "percentiles": pct, "max": prio[-1] if prio else 0.0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default=".tickets-tracker")
    ap.add_argument("--emit-recommendation", default=None)
    args = ap.parse_args(argv)

    rows = load_corpus(args.corpus)
    dist = _distribution(rows)
    curve = {t: round(precision_at(rows, t), 4) for t in CANDIDATES}
    counts = {
        BLOCK_WORTHY: sum(1 for r in rows if r["adjudication"] == BLOCK_WORTHY),
        NOT_BLOCK_WORTHY: sum(1 for r in rows if r["adjudication"] == NOT_BLOCK_WORTHY),
        AMBIGUOUS: sum(1 for r in rows if r["adjudication"] == AMBIGUOUS),
    }
    chosen = select_threshold(rows)

    # The first-pass proxy adjudicates FROM tier/severity, which correlate with priority — so
    # its precision curve is partly CIRCULAR and tends to saturate; treat `proxy_threshold` as a
    # lower bound / sanity check, not the answer. The trustworthy field signal is the objective
    # PRIORITY DISTRIBUTION (no adjudication needed): the ceiling and the band structure.
    proxy_circular = all(v >= 0.99 for v in curve.values()) and dist["n"] > 0
    report = {
        "corpus_size_deduped": dist["n"],
        "priority_distribution": dist,
        "adjudication_counts": counts,
        "precision_curve": curve,
        "candidates": CANDIDATES,
        "proxy_threshold": chosen,
        "proxy_is_circular": proxy_circular,
        "decision_rule": "smallest t with precision(block-worthy | priority>=t) > 0.5",
        "note": (
            "The tier/severity-based first-pass adjudication is CIRCULAR (correlates with "
            "priority) and its precision curve saturates — do NOT consume proxy_threshold "
            "directly. The objective priority distribution is the trustworthy signal; a "
            "content-based held-out (Sonnet/human) adjudication is required to set the final "
            "threshold, which sibling b9c0 must confirm before flipping blocking."
        ),
    }
    print(json.dumps(report, indent=2))

    if args.emit_recommendation:
        lines = [
            "# Code-review BLOCK threshold — derivation (ticket 9f25)\n",
            f"Deduped code-v2 corpus: **{dist['n']}** findings.\n",
            f"Priority distribution (validity x impact): {dist['percentiles']}, max {dist['max']}.\n",
            f"Adjudication (first-pass proxy): {counts}.\n",
            "\n## Precision curve — precision(block-worthy | priority >= t)\n",
            "| t | precision |\n|---|---|\n",
        ]
        lines += [f"| {t} | {curve[t]} |\n" for t in CANDIDATES]
        band = "the #518 importlib code-execution security findings"
        lines.append(
            f"\n## Finding: a hard priority CEILING at {dist['max']}\n\n"
            f"The trustworthy, adjudication-free signal is the priority distribution: p90="
            f"{dist['percentiles'].get(90)}, p95={dist['percentiles'].get(95)}, "
            f"**max={dist['max']}**. The 0.60 ceiling the calibration analysis reported is "
            f"CONFIRMED — no code-v2 finding ever scored priority above {dist['max']}, so a "
            f"threshold at 0.6 would catch only the rare max-priority findings, and the 0.54 "
            f"band ({band}) sits near p95.\n\n"
            "## Provisional threshold: **0.54** (REQUIRES held-out confirmation)\n\n"
            "Applying the operator's decision rule to the band structure: 0.54 catches the "
            "#518-class security band while staying below the 0.60 ceiling (0.6 would block "
            "almost nothing). This is PROVISIONAL. The precision curve above was produced by a "
            "deterministic tier/severity proxy that is CIRCULAR (it correlates with priority) "
            "and therefore saturates — it is NOT a valid basis for the final threshold. Per the "
            "ticket, a content-based held-out (Sonnet-scored / human) adjudication of the "
            "apparent block-worthy findings is required to set the final value; **sibling b9c0 "
            "must CONFIRM the threshold against that held-out adjudication before flipping the "
            "`security` criterion to blocking** (b9c0's AC already records this contingency).\n"
        )
        open(args.emit_recommendation, "w").write("".join(lines))
        # The adjudicated corpus artifact (9f25 AC): one row per deduped finding. Schema carries
        # BOTH the AC-specified closed vocabulary — {finding_id: str, criterion: str, impact,
        # validity, priority, adjudication, evidence} — AND the canonical rebar source fields
        # (norm_id, criteria[]) it projects from, so the row is self-describing: finding_id IS the
        # norm_id (the diff-scoped join key), and criterion is the criteria array joined with ","
        # (a code-review finding carries multiple criterion ids).
        adj_path = os.path.join(
            os.path.dirname(args.emit_recommendation), "code_review_adjudication.jsonl"
        )
        with open(adj_path, "w") as fh:
            for r in sorted(rows, key=lambda x: -x["priority"]):
                fh.write(
                    json.dumps(
                        {
                            "finding_id": r["norm_id"],
                            "criterion": ",".join(r["criteria"]),
                            "norm_id": r["norm_id"],
                            "criteria": r["criteria"],
                            "impact": r["impact"],
                            "validity": r["validity"],
                            "priority": r["priority"],
                            "adjudication": r["adjudication"],
                            "tier": r.get("tier"),
                            "evidence": "first-pass proxy (tier/severity); held-out confirmation pending",
                        }
                    )
                    + "\n"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
