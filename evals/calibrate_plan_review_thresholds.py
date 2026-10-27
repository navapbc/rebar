#!/usr/bin/env python3
"""Offline plan-review threshold calibration over the REVIEW_RESULT sidecar corpus.

Reads every .tickets-tracker/*/*-REVIEW_RESULT.json sidecar and derives, per criterion,
the two reliability signals + a proposed (default_posture, block_threshold).

Signals (per docs/plan-review-gate.md + the approved methodology):
  * Verifier-refutation (DENSE): validity distribution, P(dropped), P(indeterminate),
    and the per-binary-subquestion "no" rate (which dimension the verifier refutes).
  * Voluntary revision-response: a material_fingerprint change between consecutive
    reviews of a ticket = a genuine revision; measured at CRITERION-LOAD-DELTA
    granularity (count drop), NOT exact finding-id survival (confounded by finder
    text-churn).
  * Fire-rate denominator from coverage.routing (which criteria ran per review).
"""

from __future__ import annotations
import json, os, glob, collections, statistics
from typing import Any

GRADED = (
    "is_verifiable",
    "evidence_entails_finding",
    "path_reachable",
    "impact_follows_necessarily",
    "no_viable_alternative_explanation",
    "no_existing_mitigation",
    "severity_claim_justified",
)
DET_CRITERIA = {"P1", "P5", "P6", "P7", "P8", "P9", "P2", "P3", "P4"}  # always "run"
MIN_N = 25  # statistical-power floor for an auto-proposal; below this -> Step 5 interactive


def load(
    impact_model_version: str | None = None,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Bucket REVIEW_RESULT sidecars by ticket. When ``impact_model_version`` is given, SEGMENT the
    corpus to that formula version: a sidecar tagged with a DIFFERENT version is skipped, and a
    sidecar with NO ``impact_model_version`` tag is treated as "unknown" and ALSO skipped — never
    silently pooled across versions (story raptorial-galloping-dragon; mirrors the missing-`cohort`
    discipline). With ``None`` (the default) behaviour is unchanged: all sidecars are pooled.

    Returns ``(by_ticket, skipped)`` where ``skipped`` counts the excluded remainder by reason
    (``different_version`` / ``untagged`` / ``unparseable``), so a segmented run can report what
    it did NOT analyze (calibration-3 requirement — a segment size without its complement is
    unauditable)."""
    by_ticket: dict[str, list[dict]] = collections.defaultdict(list)
    skipped = {"different_version": 0, "untagged": 0, "unparseable": 0}
    for fp in glob.glob(".tickets-tracker/*/*-REVIEW_RESULT.json"):
        try:
            d = json.load(open(fp))
        except Exception:
            skipped["unparseable"] += 1
            continue
        data = d.get("data", {})
        if (
            impact_model_version is not None
            and data.get("impact_model_version") != impact_model_version
        ):
            # segment by version; a missing tag (None) never equals a requested version
            tagged = data.get("impact_model_version") is not None
            skipped["different_version" if tagged else "untagged"] += 1
            continue
        by_ticket[data.get("ticket_id")].append(
            {
                "ts": os.path.basename(fp).split("-")[0],
                "matfp": data.get("material_fingerprint"),
                "model": data.get("model"),
                "type": data.get("ticket_type"),
                "routing": (data.get("coverage", {}) or {}).get("routing", {}) or {},
                "findings": data.get("findings", []),
            }
        )
    for rs in by_ticket.values():
        rs.sort(key=lambda r: r["ts"])
    return by_ticket, skipped


def ran_criteria(rev: dict) -> set[str]:
    r = rev["routing"]
    s = set(r.get("agent_tier", []) or []) | set(r.get("single_turn", []) or [])
    return s | DET_CRITERIA  # DET floor runs every review


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--impact-model-version",
        default=None,
        help="Segment the corpus to one impact-model formula version (e.g. plan-v2); sidecars of a "
        "different version OR with no version tag are skipped (never pooled). Default: pool all.",
    )
    args = ap.parse_args()
    by_ticket, skipped = load(impact_model_version=args.impact_model_version)
    if args.impact_model_version:
        print(f"[segmented to impact_model_version={args.impact_model_version}]")
    revs = [r for rs in by_ticket.values() for r in rs]
    print(
        f"corpus: {len(revs)} sidecars / {len(by_ticket)} tickets / "
        f"{sum(len(r['findings']) for r in revs)} findings"
    )
    print(
        f"skipped remainder: {sum(skipped.values())} sidecars "
        f"(different_version={skipped['different_version']}, untagged={skipped['untagged']}, "
        f"unparseable={skipped['unparseable']})\n"
    )

    # ---- per-criterion accumulators ----
    n = collections.Counter()  # findings tagged with C
    n_ran = collections.Counter()  # reviews where C ran
    n_fired = collections.Counter()  # reviews producing >=1 finding for C
    decisions = collections.defaultdict(collections.Counter)
    validities: dict[str, list[float]] = collections.defaultdict(list)
    priorities_surv: dict[str, list[float]] = collections.defaultdict(list)  # advisory/block only
    subq_no = collections.defaultdict(collections.Counter)  # "no" answers per sub-question
    subq_ans = collections.defaultdict(collections.Counter)  # answerable per sub-question
    models = collections.defaultdict(collections.Counter)

    for rev in revs:
        ran = ran_criteria(rev)
        for c in ran:
            n_ran[c] += 1
        fired_this = collections.Counter()
        for f in rev["findings"]:
            crits = f.get("criteria", []) or ["<none>"]
            dec = f.get("decision")
            v = f.get("validity")
            for c in crits:
                n[c] += 1
                fired_this[c] += 1
                decisions[c][dec] += 1
                models[c][rev["model"]] += 1
                if f.get("tier") == "LLM" and v is not None:
                    validities[c].append(v)
                if dec in ("advisory", "block"):
                    priorities_surv[c].append(float(f.get("priority") or 0.0))
                ver = f.get("verification") or {}
                binary = ver.get("binary", {}) or {}
                for q in GRADED:
                    a = binary.get(q)
                    if a in ("yes", "no", "insufficient"):
                        subq_ans[c][q] += 1
                        if a == "no":
                            subq_no[c][q] += 1
        for c in fired_this:
            n_fired[c] += 1

    # ---- revision-response (criterion-load-delta over material-change episodes) ----
    load_before = collections.Counter()  # sum of finding-load pre-revision (eligible)
    load_resolved = collections.Counter()  # sum of load DROP after revision (clamped >=0)
    eligible_eps = collections.Counter()  # episodes where C had >=1 finding pre-revision
    for rs in by_ticket.values():
        for k in range(len(rs) - 1):
            a, b = rs[k], rs[k + 1]
            if a["matfp"] == b["matfp"]:
                continue  # no material revision between these rounds
            la = collections.Counter()
            lb = collections.Counter()
            for f in a["findings"]:
                for c in f.get("criteria", []) or ["<none>"]:
                    la[c] += 1
            for f in b["findings"]:
                for c in f.get("criteria", []) or ["<none>"]:
                    lb[c] += 1
            for c, before in la.items():
                eligible_eps[c] += 1
                load_before[c] += before
                load_resolved[c] += max(0, before - lb.get(c, 0))

    # ---- emit table + proposal ----
    def pct(xs, p):
        if not xs:
            return 0.0
        xs = sorted(xs)
        i = min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))
        return xs[i]

    rows = []
    for c in sorted(n, key=lambda k: -n[k]):
        vals = validities[c]
        mv = round(statistics.mean(vals), 3) if vals else None
        p_drop = round(decisions[c]["dropped"] / n[c], 3)
        p_indet = round(decisions[c]["indeterminate"] / n[c], 3)
        fire = round(n_fired[c] / n_ran[c], 3) if n_ran[c] else None
        rr = round(load_resolved[c] / load_before[c], 3) if load_before[c] else None
        psurv = priorities_surv[c]
        p75 = round(pct(psurv, 75), 3)
        p90 = round(pct(psurv, 90), 3)
        pmax = round(max(psurv), 3) if psurv else 0.0
        # worst (highest "no"-rate) sub-question
        worst_q, worst_rate = None, 0.0
        for q in GRADED:
            if subq_ans[c][q] >= 5:
                r = subq_no[c][q] / subq_ans[c][q]
                if r > worst_rate:
                    worst_q, worst_rate = q, r
        rows.append(
            dict(
                c=c,
                n=n[c],
                n_ran=n_ran[c],
                fire=fire,
                mv=mv,
                p_drop=p_drop,
                p_indet=p_indet,
                rr=rr,
                elig=eligible_eps[c],
                p75=p75,
                p90=p90,
                pmax=pmax,
                worst_q=worst_q,
                worst_rate=round(worst_rate, 2),
                models=dict(models[c]),
            )
        )

    hdr = (
        f"{'crit':<7}{'n':>5}{'ran':>5}{'fire':>6}{'mval':>6}{'drop':>6}{'indet':>6}"
        f"{'rev_rr':>7}{'elig':>5}{'p75':>6}{'p90':>6}{'pmax':>6}  worst_subq(no-rate)"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['c']:<7}{r['n']:>5}{r['n_ran']:>5}"
            f"{(r['fire'] if r['fire'] is not None else 0):>6.2f}"
            f"{(r['mv'] if r['mv'] is not None else 0):>6.2f}"
            f"{r['p_drop']:>6.2f}{r['p_indet']:>6.2f}"
            f"{(r['rr'] if r['rr'] is not None else 0):>7.2f}{r['elig']:>5}"
            f"{r['p75']:>6.2f}{r['p90']:>6.2f}{r['pmax']:>6.2f}  "
            f"{r['worst_q'] or '-'}({r['worst_rate']})"
        )

    # ---- automated proposal (precision-first); below MIN_N -> defer to Step 5 ----
    print("\n=== PROPOSAL (precision-first; n<%d => DEFER to interactive Step 5) ===" % MIN_N)
    print(f"{'crit':<7}{'n':>5}  {'class':<14}{'posture':<10}{'threshold':>10}  rationale")
    for r in rows:
        c = r["c"]
        if c.startswith("P"):  # DET floor — posture is fixed by det_floor.py, not this knob
            continue
        mv = r["mv"] or 0.0
        if r["n"] < MIN_N:
            cls, posture, thr = "LOW-DATA", "advisory", 0.95
            rat = f"n={r['n']} below floor; bring to user (Step 5)"
        elif mv < 0.45 or r["p_indet"] > 0.20:
            cls, posture, thr = "FP-PRONE", "advisory", 0.95
            rat = f"low validity {mv}/high indet {r['p_indet']} => keep non-blocking"
        elif (r["rr"] or 0) >= 0.6 and mv >= 0.55 and r["p_indet"] <= 0.15:
            # strong on both signals -> enable blocking on the upper tail of priority
            thr = max(0.5, min(0.95, round(r["p90"], 2)))
            cls, posture = "BLOCK-ELIGIBLE", "blocking"
            rat = f"validity {mv}, rev_rr {r['rr']}; block top-decile priority>= {thr}"
        else:
            cls, posture, thr = "ADVISORY-KEEP", "advisory", 0.95
            rat = f"validity {mv}, rev_rr {r['rr']}; real but borderline => advisory"
        print(f"{c:<7}{r['n']:>5}  {cls:<14}{posture:<10}{thr:>10.2f}  {rat}")

    # model mix note
    allmod = collections.Counter()
    for r in rows:
        for m, k in r["models"].items():
            allmod[m] += k
    print("\nmodel mix across findings:", dict(allmod))


if __name__ == "__main__":
    main()
