#!/usr/bin/env python3
"""Offline code-review threshold calibration over the code-v3 REVIEW_RESULT sidecar corpus.

Analog of docs/experiments/calibrate_plan_review_thresholds.py, adapted to the code-review
sidecar shape (schema code_review_result_v2):

  * Findings live in SEPARATE buckets (blocking/advisory/dropped/indeterminate/coaching),
    each finding also carries a per-finding `decision`, so we classify by BUCKET (the pool
    the finding actually landed in) and cross-check `decision`.
  * A "review" is one sidecar = one (change_id, revision). A material revision episode is two
    consecutive sidecars of the SAME change_id with a DIFFERENT revision (Gerrit patchset bump)
    -- the code-review analog of a plan material_fingerprint change.
  * There is no coverage.routing for code review, so the fire-rate denominator is a proxy:
    reviews producing >=1 finding for C over all reviews (documented caveat).

Signals:
  * Verifier-refutation (DENSE): validity distribution, P(dropped), P(indeterminate),
    per-binary-subquestion "no" rate (which dimension the verifier refutes).
  * Voluntary revision-response: criterion-load drop across revision episodes of a change.
  * Surviving-priority percentiles (blocking+advisory only) = where a block threshold would bite.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics
from typing import Any

# The 7 shared graded sub-questions + the code-review-specific ones seen in the corpus.
GRADED = (
    "is_verifiable",
    "evidence_entails_finding",
    "path_reachable",
    "impact_follows_necessarily",
    "no_viable_alternative_explanation",
    "no_existing_mitigation",
    "severity_claim_justified",
    "absence_confirmed_in_context",
    "cited_reference_accurate",
    "respects_artifact_altitude",
    "asserted_capability_confirmed",
)
SURFACED = ("blocking", "advisory")
POOLS = ("blocking", "advisory", "dropped", "indeterminate")
MIN_N = 25  # statistical-power floor for an auto-proposal


def load(tracker: str, version: str | None) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Bucket code-review sidecars by change_id (fallback ticket_id), newest-last. Segment to
    `version` (impact_model_version); a different/absent tag is skipped, never pooled."""
    by_change: dict[str, list[dict]] = collections.defaultdict(list)
    skipped = {"different_version": 0, "untagged": 0, "unparseable": 0, "wrong_schema": 0}
    for fp in glob.glob(os.path.join(tracker, "**", "*-REVIEW_RESULT.json"), recursive=True):
        try:
            ev = json.load(open(fp))
        except Exception:
            skipped["unparseable"] += 1
            continue
        d = ev.get("data") if isinstance(ev, dict) else None
        if not isinstance(d, dict) or str(d.get("schema", "")).startswith("code_review_result") is False:
            skipped["wrong_schema"] += 1
            continue
        if version is not None and d.get("impact_model_version") != version:
            skipped["different_version" if d.get("impact_model_version") else "untagged"] += 1
            continue
        key = d.get("change_id") or d.get("ticket_id") or os.path.basename(fp)
        by_change[key].append(
            {
                "ts": os.path.basename(fp).split("-")[0],
                "change_id": d.get("change_id"),
                "revision": d.get("revision"),
                "verdict": d.get("verdict"),
                "pools": {b: (d.get(b) or []) for b in POOLS},
            }
        )
    for rs in by_change.values():
        rs.sort(key=lambda r: r["ts"])
    return by_change, skipped


def _crits(f: dict) -> list[str]:
    return f.get("criteria") or ["<none>"]


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))
    return xs[i]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tracker", default=".tickets-tracker")
    ap.add_argument("--impact-model-version", default="code-v3")
    ap.add_argument("--emit", default=None, help="write a markdown report to this path")
    args = ap.parse_args()

    by_change, skipped = load(args.tracker, args.impact_model_version)
    revs = [r for rs in by_change.values() for r in rs]
    total_reviews = len(revs)
    n_findings = sum(len(r["pools"][b]) for r in revs for b in POOLS)
    hdr_lines = [
        f"[segmented to impact_model_version={args.impact_model_version}]",
        f"corpus: {total_reviews} sidecars / {len(by_change)} changes / {n_findings} pooled findings",
        f"skipped remainder: {sum(skipped.values())} sidecars ({skipped})",
    ]
    print("\n".join(hdr_lines) + "\n")

    # ---- per-criterion accumulators ----
    n = collections.Counter()  # findings tagged with C (across POOLS)
    n_fired = collections.Counter()  # reviews producing >=1 finding for C
    decisions = collections.defaultdict(collections.Counter)  # by bucket pool
    validities = collections.defaultdict(list)
    priorities_surv = collections.defaultdict(list)  # blocking/advisory only
    subq_no = collections.defaultdict(collections.Counter)
    subq_ans = collections.defaultdict(collections.Counter)

    for rev in revs:
        fired_this = set()
        for pool in POOLS:
            for f in rev["pools"][pool]:
                if not isinstance(f, dict):
                    continue
                for c in _crits(f):
                    n[c] += 1
                    fired_this.add(c)
                    decisions[c][pool] += 1
                    v = f.get("validity")
                    if f.get("tier") == "LLM" and v is not None:
                        validities[c].append(float(v))
                    if pool in SURFACED:
                        priorities_surv[c].append(float(f.get("priority") or 0.0))
                    binary = (f.get("verification") or {}).get("binary", {}) or {}
                    for q in GRADED:
                        a = binary.get(q)
                        if a in ("yes", "no", "insufficient"):
                            subq_ans[c][q] += 1
                            if a == "no":
                                subq_no[c][q] += 1
        for c in fired_this:
            n_fired[c] += 1

    # ---- revision-response (criterion-load-delta across revision episodes) ----
    load_before = collections.Counter()
    load_resolved = collections.Counter()
    eligible_eps = collections.Counter()
    for rs in by_change.values():
        for k in range(len(rs) - 1):
            a, b = rs[k], rs[k + 1]
            if a["revision"] == b["revision"] or not a["revision"] or not b["revision"]:
                continue  # same patchset (or unknown) -> not a revision episode
            la, lb = collections.Counter(), collections.Counter()
            for f in (a["pools"]["blocking"] + a["pools"]["advisory"]):
                if isinstance(f, dict):
                    for c in _crits(f):
                        la[c] += 1
            for f in (b["pools"]["blocking"] + b["pools"]["advisory"]):
                if isinstance(f, dict):
                    for c in _crits(f):
                        lb[c] += 1
            for c, before in la.items():
                eligible_eps[c] += 1
                load_before[c] += before
                load_resolved[c] += max(0, before - lb.get(c, 0))

    # ---- build rows ----
    rows = []
    for c in sorted(n, key=lambda k: -n[k]):
        vals = validities[c]
        mv = round(statistics.mean(vals), 3) if vals else None
        tot = n[c]
        p_drop = round(decisions[c]["dropped"] / tot, 3)
        p_indet = round(decisions[c]["indeterminate"] / tot, 3)
        p_block = round(decisions[c]["blocking"] / tot, 3)
        fire = round(n_fired[c] / total_reviews, 3) if total_reviews else None
        rr = round(load_resolved[c] / load_before[c], 3) if load_before[c] else None
        psurv = priorities_surv[c]
        worst_q, worst_rate = None, 0.0
        for q in GRADED:
            if subq_ans[c][q] >= 5:
                r = subq_no[c][q] / subq_ans[c][q]
                if r > worst_rate:
                    worst_q, worst_rate = q, r
        rows.append(
            dict(
                c=c, n=tot, surf=len(psurv), fire=fire, mv=mv,
                p_drop=p_drop, p_indet=p_indet, p_block=p_block, rr=rr,
                elig=eligible_eps[c],
                p75=round(pct(psurv, 75), 3), p90=round(pct(psurv, 90), 3),
                p95=round(pct(psurv, 95), 3), pmax=round(max(psurv), 3) if psurv else 0.0,
                worst_q=worst_q, worst_rate=round(worst_rate, 2),
            )
        )

    # ---- table ----
    hdr = (f"{'crit':<26}{'n':>5}{'surf':>5}{'fire':>6}{'mval':>6}{'drop':>6}{'indet':>6}"
           f"{'pblk':>6}{'rev_rr':>7}{'elig':>5}{'p75':>6}{'p90':>6}{'p95':>6}{'pmax':>6}  worst_subq(no-rate)")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['c']:<26}{r['n']:>5}{r['surf']:>5}"
              f"{(r['fire'] or 0):>6.2f}{(r['mv'] or 0):>6.2f}{r['p_drop']:>6.2f}{r['p_indet']:>6.2f}"
              f"{r['p_block']:>6.2f}{(r['rr'] or 0):>7.2f}{r['elig']:>5}"
              f"{r['p75']:>6.2f}{r['p90']:>6.2f}{r['p95']:>6.2f}{r['pmax']:>6.2f}  "
              f"{r['worst_q'] or '-'}({r['worst_rate']})")

    # ---- precision-first proposal ----
    # Refined from the plan-review rule for the code-review sidecar shape:
    #  * DET/attestation pseudo-criteria (validity==0 but findings land in the blocking pool) are
    #    deterministic gates whose posture is fixed by the detector/attestation, NOT an LLM priority
    #    threshold -> excluded (DET).
    #  * P(dropped) is a code-review-specific FP signal: the Pass-3 decider DROPS a finding it judges
    #    non-actionable. A criterion the decider drops heavily is FP-prone regardless of validity
    #    (docs/supply-chain/scope-intent). The plan-review rule lacked this guard.
    DROP_FP = 0.40  # >40% of findings dropped by the decider => FP-prone
    def classify(r: dict) -> tuple[str, str, float, str]:
        mv = r["mv"] or 0.0
        # DET / attestation gate (validity not meaningful; posture fixed elsewhere)
        if r["p_block"] > 0 and (r["mv"] is None or mv < 0.05):
            return "DET/ATTEST", "n/a", 0.0, f"deterministic/attestation gate (pblk={r['p_block']}, validity~0); not LLM-tunable"
        if r["n"] < MIN_N:
            return "LOW-DATA", "advisory", 0.95, f"n={r['n']} below floor; interactive review"
        if mv < 0.45 or r["p_indet"] > 0.20 or r["p_drop"] > DROP_FP:
            return "FP-PRONE", "advisory", 0.95, f"validity {mv}/indet {r['p_indet']}/drop {r['p_drop']} => keep advisory"
        if (r["rr"] or 0) >= 0.6 and mv >= 0.55 and r["p_indet"] <= 0.15:
            thr = max(0.5, min(0.95, round(r["p90"], 2)))
            return "BLOCK-ELIGIBLE", "blocking", thr, f"validity {mv}, drop {r['p_drop']}, rev_rr {r['rr']}; block priority>= {thr}"
        return "ADVISORY-KEEP", "advisory", 0.95, f"validity {mv}, drop {r['p_drop']}, rev_rr {r['rr']}; real but borderline => advisory"

    print("\n=== PROPOSAL (precision-first; n<%d => LOW-DATA/interactive) ===" % MIN_N)
    print(f"{'crit':<26}{'n':>5}  {'class':<15}{'posture':<10}{'thr':>6}  rationale")
    proposal = []
    for r in rows:
        cls, posture, thr, rat = classify(r)
        proposal.append((r, cls, posture, thr, rat))
        print(f"{r['c']:<26}{r['n']:>5}  {cls:<15}{posture:<10}{thr:>6.2f}  {rat}")

    if args.emit:
        _emit_report(args.emit, hdr_lines, rows, proposal, args.impact_model_version)
        print(f"\nreport -> {args.emit}")


def _emit_report(path: str, hdr_lines, rows, proposal, version) -> None:
    L = [f"# Code-review threshold calibration ({version})\n"]
    L += [f"{ln}\n" for ln in hdr_lines]
    L.append("\n## Per-criterion signals\n\n")
    L.append("| criterion | n | surf | fire | mval | drop | indet | pblk | rev_rr | elig | p75 | p90 | p95 | pmax | worst subq (no-rate) |\n")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for r in rows:
        L.append(f"| {r['c']} | {r['n']} | {r['surf']} | {r['fire']} | {r['mv']} | {r['p_drop']} | "
                 f"{r['p_indet']} | {r['p_block']} | {r['rr']} | {r['elig']} | {r['p75']} | {r['p90']} | "
                 f"{r['p95']} | {r['pmax']} | {r['worst_q']} ({r['worst_rate']}) |\n")
    L.append("\n## Precision-first proposal\n\n")
    L.append("| criterion | n | class | posture | threshold | rationale |\n|---|---|---|---|---|---|\n")
    for r, cls, posture, thr, rat in proposal:
        L.append(f"| {r['c']} | {r['n']} | {cls} | {posture} | {thr:.2f} | {rat} |\n")
    open(path, "w").write("".join(L))


if __name__ == "__main__":
    main()
