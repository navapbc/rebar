#!/usr/bin/env python3
"""Offline code-review threshold calibration over a version-segmented REVIEW_RESULT sidecar corpus.

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

ROUTING-AWARE (fixes CORRECTIONS A / C / E recorded in
docs/experiments/code-review-threshold-calibration.md). Earlier revisions of this script read
NO routing index, so every re-run reproduced two known-wrong rows: `sec` was reported as a
DET/attestation gate (it is an LLM synonym of `security`), and `project.review-phase-boundaries`
was reported at the 0.95 unknown-criterion default rather than the 0.90 the project overlay
actually sets. Both are now read from the SAME production path the gate uses --
`registry.effective_routing` (packaged index MERGED with `.rebar/criteria_routing.json`) and
`registry.normalize_criteria` (the `sec`->`security` / `documentation`->`docs` synonym map) --
so the generator cannot drift from the gate again. A pure-JSON fallback keeps the script usable
in a checkout where `rebar` is not importable. Consequences:

  * criterion labels are NORMALIZED before accumulation, so synonyms are pooled with their
    canonical criterion instead of falling to the unknown-label default;
  * DET/attestation criteria are identified by their routing `exec == "DET"`, not by the old
    "validity ~ 0 and something blocked" heuristic that misfired on `sec`;
  * every row carries its CURRENT posture/threshold, so the proposal reads as a DELTA against
    what is committed rather than as a free-standing absolute.

`--dump-newly-blocking CRIT=THR` writes the findings that would become NEWLY blocking under a
proposed threshold (priority in [THR, current_threshold)) to JSON, for content adjudication --
the false-positive/nit rate of a proposed flip is a question about finding TEXT, which no
validity statistic answers.
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
UNROUTED_THRESHOLD = 0.95  # kernel default for a criterion with no routing entry


def _current_impact_model_version() -> str:
    """The impact-model version the gate stamps TODAY, read from the production constant. A
    hardcoded default is how this script silently kept analysing a retired cohort: the model
    moved to code-v5 while the default still said code-v3, and ADR 0036 forbids pooling the two,
    so the mismatch produced an empty-but-plausible segment rather than an error."""
    try:
        from rebar.llm.code_review.sidecar import IMPACT_MODEL_VERSION  # type: ignore

        return str(IMPACT_MODEL_VERSION)
    except Exception:
        pass
    try:
        import re as _re

        sidecar_py = os.path.join("src", "rebar", "llm", "code_review", "sidecar.py")
        with open(sidecar_py) as fh:
            src = fh.read()
        m = _re.search(r'^IMPACT_MODEL_VERSION\s*=\s*"([^"]+)"', src, _re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "code-v5"


def _load_routing(repo_root: str) -> dict[str, dict]:
    """The EFFECTIVE per-criterion routing (packaged index + the project overlay's `code_review`
    map), read through the production path so this script cannot drift from the gate. Falls back
    to reading the two JSON files directly when `rebar` is not importable."""
    try:
        from rebar.llm.code_review import registry  # type: ignore

        return dict(registry.effective_routing(repo_root))
    except Exception:
        pass
    out: dict[str, dict] = {}
    packaged = os.path.join("src", "rebar", "llm", "code_review", "criteria_routing.json")
    overlay = os.path.join(repo_root, ".rebar", "criteria_routing.json")
    for path, key in ((packaged, None), (overlay, "code_review")):
        try:
            with open(path) as fh:
                raw = json.load(fh)
        except Exception:
            continue
        block = raw.get(key, {}) if key else raw
        for k, v in (block or {}).items():
            if not k.startswith("_") and isinstance(v, dict):
                out.setdefault(k, {}).update(v)
    return out


def _synonyms() -> dict[str, str]:
    """The model-emitted criterion-label synonym map, from the production registry when
    importable (ticket d890-e711-156e-444b), else its committed literal value."""
    try:
        from rebar.llm.code_review import registry  # type: ignore

        return dict(registry.CRITERIA_SYNONYMS)
    except Exception:
        return {"sec": "security", "documentation": "docs"}


ROUTING: dict[str, dict] = {}
SYNONYMS: dict[str, str] = {}


def _posture_label(r: dict, *, short: bool = False) -> str:
    """The criterion's CURRENT posture as a display string. `UNROUTED` is deliberately distinct
    from `advisory` -- an unrouted criterion only behaves advisory by falling through the default,
    and conflating the two is what let a high-volume criterion sit unrouted unnoticed."""
    if not r["routed"]:
        return "UNROUTED"
    if r["cur_blocking"]:
        kind = "BLK" if short else "blocking"
    else:
        kind = "adv" if short else "advisory"
    return f"{kind}@{r['cur_thr']:.2f}"


def posture_of(criterion: str) -> tuple[float, bool, str]:
    """`(current_threshold, blocking_enabled, exec_mode)` for a criterion. An UNROUTED criterion
    reports the kernel's 0.95 unknown-label default and `exec` "-" -- which is itself a finding
    (an unrouted high-volume criterion is a routing bug, not a deliberate posture)."""
    e = ROUTING.get(criterion)
    if not e:
        return UNROUTED_THRESHOLD, False, "-"
    thr = e.get("block_threshold")
    return (
        float(thr) if thr is not None else UNROUTED_THRESHOLD,
        bool(e.get("blocking_enabled")),
        str(e.get("exec") or "-"),
    )


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
    """The finding's criterion labels, NORMALIZED through the synonym map before accumulation
    (CORRECTION A): an un-normalized `sec` pools separately from `security` and then falls to the
    unknown-label default, which is what produced the bogus DET/ATTEST row for `sec`."""
    raw = f.get("criteria") or ["<none>"]
    seen: list[str] = []
    for label in raw:
        canonical = label if label in ROUTING else SYNONYMS.get(label, label)
        if canonical not in seen:
            seen.append(canonical)
    return seen


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))
    return xs[i]


def _parse_block_impact_specs(specs: list[str]) -> list[tuple[str, float]]:
    """Parse ``CRIT=THR`` block-impact specs into ``(criterion, threshold)`` pairs."""
    out: list[tuple[str, float]] = []
    for spec in specs:
        crit, _, raw = spec.partition("=")
        if not crit or not raw:
            raise SystemExit(f"--block-impact expects CRIT=THR, got {spec!r}")
        try:
            out.append((crit, float(raw)))
        except ValueError:
            raise SystemExit(f"--block-impact threshold must be a number, got {raw!r}") from None
    return out


def block_impact(by_change: dict[str, list[dict]], criterion: str, thr: float) -> dict[str, Any]:
    """Retrospective block-impact of ``thr`` on ``criterion`` over the pooled corpus.

    Reproduces the columns of this document's "Block-impact of the proposed thresholds" table:
    how many SURVIVING (blocking+advisory) findings tagged with the criterion carry
    ``priority >= thr``, how many distinct changes that is, and the validity of that
    would-block set. Purely retrospective — no sidecar is rewritten."""
    surviving = would_block = low_validity = 0
    hits: set[str] = set()
    validities: list[float] = []
    for change_key, revs in by_change.items():
        for rev in revs:
            for pool in SURFACED:
                for f in rev["pools"][pool]:
                    if not isinstance(f, dict) or criterion not in _crits(f):
                        continue
                    surviving += 1
                    if float(f.get("priority") or 0.0) < thr:
                        continue
                    would_block += 1
                    hits.add(change_key)
                    v = f.get("validity")
                    if v is None:
                        continue
                    validities.append(float(v))
                    if float(v) < 0.5:
                        low_validity += 1
    total_changes = len(by_change)
    return {
        "criterion": criterion,
        "thr": thr,
        "surviving": surviving,
        "would_block": would_block,
        "of_surviving": round(100 * would_block / surviving, 1) if surviving else 0.0,
        "changes_hit": len(hits),
        "of_all_changes": round(100 * len(hits) / total_changes, 2) if total_changes else 0.0,
        "mean_validity": round(statistics.mean(validities), 2) if validities else None,
        "val_lt_half": low_validity,
    }


def newly_blocking(by_change: dict[str, list[dict]], criterion: str, thr: float) -> list[dict]:
    """The findings that would become NEWLY blocking for ``criterion`` at ``thr`` -- priority in
    ``[thr, current_threshold)``, i.e. exactly the set the proposed flip ADDS over what already
    blocks today. Returns the finding TEXT plus the severity attributes, because the
    false-positive / nit rate of a proposed threshold is a question about content that no
    validity statistic answers; `--dump-newly-blocking` writes these out for adjudication."""
    cur_thr, cur_blocking, _ = posture_of(criterion)
    ceiling = cur_thr if cur_blocking else float("inf")
    out: list[dict] = []
    for change_key, revs in by_change.items():
        for rev in revs:
            for pool in SURFACED:
                for f in rev["pools"][pool]:
                    if not isinstance(f, dict) or criterion not in _crits(f):
                        continue
                    pri = float(f.get("priority") or 0.0)
                    if not (thr <= pri < ceiling):
                        continue
                    attrs = (f.get("verification") or {}).get("severity_attributes", {}) or {}
                    out.append(
                        {
                            "change": change_key,
                            "criterion": criterion,
                            "pool": pool,
                            "priority": pri,
                            "validity": f.get("validity"),
                            "impact": f.get("impact"),
                            "location": f.get("location"),
                            "finding": f.get("finding"),
                            "suggested_fix": f.get("suggested_fix"),
                            "evidence": f.get("evidence"),
                            "severity_attributes": attrs,
                        }
                    )
    return sorted(out, key=lambda r: -r["priority"])


def print_block_impact(by_change: dict[str, list[dict]], specs: list[tuple[str, float]]) -> None:
    """Print the block-impact table for each ``(criterion, threshold)`` spec."""
    print(f"block-impact over {len(by_change)} changes\n")
    print("| criterion | thr | surviving | would-block | of surviving | changes hit "
          "| of all changes | mean validity | val<0.5 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for criterion, thr in specs:
        r = block_impact(by_change, criterion, thr)
        mv = "n/a" if r["mean_validity"] is None else f"{r['mean_validity']:.2f}"
        print(f"| {r['criterion']} | {r['thr']:.2f} | {r['surviving']} | {r['would_block']} "
              f"| {r['of_surviving']}% | {r['changes_hit']} | {r['of_all_changes']}% "
              f"| {mv} | {r['val_lt_half']} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tracker", default=".tickets-tracker")
    ap.add_argument(
        "--impact-model-version",
        default=_current_impact_model_version(),
        help="corpus segment to analyse; defaults to the version the gate CURRENTLY stamps "
        "(%(default)s), never a hardcoded historical one -- ADR 0036 forbids pooling versions",
    )
    ap.add_argument("--repo-root", default=".", help="root whose .rebar/ overlay is read")
    ap.add_argument("--emit", default=None, help="write a markdown report to this path")
    ap.add_argument(
        "--block-impact",
        action="append",
        default=[],
        metavar="CRIT=THR",
        help="print the retrospective block-impact table for CRIT at threshold THR "
        "(repeatable) instead of the full calibration",
    )
    ap.add_argument(
        "--dump-newly-blocking",
        action="append",
        default=[],
        metavar="CRIT=THR",
        help="write the findings that would become NEWLY blocking for CRIT at THR "
        "(priority in [THR, current threshold)) to JSON for content adjudication; "
        "repeatable, paired with --dump-to",
    )
    ap.add_argument("--dump-to", default="newly_blocking.json")
    args = ap.parse_args()

    global ROUTING, SYNONYMS
    ROUTING = _load_routing(args.repo_root)
    SYNONYMS = _synonyms()

    by_change, skipped = load(args.tracker, args.impact_model_version)
    if args.dump_newly_blocking:
        payload = {}
        for criterion, thr in _parse_block_impact_specs(args.dump_newly_blocking):
            found = newly_blocking(by_change, criterion, thr)
            payload[f"{criterion}@{thr}"] = found
            cur, blocking, _ = posture_of(criterion)
            now = f"blocking@{cur}" if blocking else (f"advisory@{cur}" if criterion in ROUTING else "UNROUTED")
            print(f"{criterion}: {now} -> blocking@{thr}  newly-blocking findings: {len(found)}")
        with open(args.dump_to, "w") as fh:
            json.dump(payload, fh, indent=1)
        print(f"\ndump -> {args.dump_to}")
        return
    if args.block_impact:
        print_block_impact(by_change, _parse_block_impact_specs(args.block_impact))
        return
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
        cur_thr, cur_blocking, exec_mode = posture_of(c)
        rows.append(
            dict(
                c=c, cur_thr=cur_thr, cur_blocking=cur_blocking, exec=exec_mode,
                routed=c in ROUTING,
                n=tot, surf=len(psurv), fire=fire, mv=mv,
                p_drop=p_drop, p_indet=p_indet, p_block=p_block, rr=rr,
                elig=eligible_eps[c],
                p75=round(pct(psurv, 75), 3), p90=round(pct(psurv, 90), 3),
                p95=round(pct(psurv, 95), 3), pmax=round(max(psurv), 3) if psurv else 0.0,
                worst_q=worst_q, worst_rate=round(worst_rate, 2),
            )
        )

    # ---- table ----
    hdr = (f"{'crit':<26}{'now':>10}{'n':>5}{'surf':>5}{'fire':>6}{'mval':>6}{'drop':>6}{'indet':>6}"
           f"{'pblk':>6}{'rev_rr':>7}{'elig':>5}{'p75':>6}{'p90':>6}{'p95':>6}{'pmax':>6}  worst_subq(no-rate)")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        now = _posture_label(r, short=True)
        print(f"{r['c']:<26}{now:>10}{r['n']:>5}{r['surf']:>5}"
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
        # DET / attestation gate: identified by its ROUTING `exec` (CORRECTION A), not by the old
        # "something blocked and validity ~ 0" heuristic -- that heuristic classified `sec`, an
        # ordinary LLM synonym of `security`, as a deterministic gate on every re-run. A criterion
        # with no routing entry can still be an attestation pseudo-criterion, so the heuristic is
        # kept as a fallback for the UNROUTED case only.
        if r["exec"] == "DET":
            return ("DET/ATTEST", "n/a", 0.0,
                    "deterministic detector (exec=DET, fail_mode fixed by routing); not LLM-tunable")
        if r["exec"] == "-" and r["p_block"] > 0 and (r["mv"] is None or mv < 0.05):
            return ("DET/ATTEST", "n/a", 0.0,
                    f"unrouted attestation gate (pblk={r['p_block']}, validity~0); not LLM-tunable")
        if r["n"] < MIN_N:
            return "LOW-DATA", "advisory", 0.95, f"n={r['n']} below floor; interactive review"
        if mv < 0.45 or r["p_indet"] > 0.20 or r["p_drop"] > DROP_FP:
            return "FP-PRONE", "advisory", 0.95, f"validity {mv}/indet {r['p_indet']}/drop {r['p_drop']} => keep advisory"
        if (r["rr"] or 0) >= 0.6 and mv >= 0.55 and r["p_indet"] <= 0.15:
            thr = max(0.5, min(0.95, round(r["p90"], 2)))
            return "BLOCK-ELIGIBLE", "blocking", thr, f"validity {mv}, drop {r['p_drop']}, rev_rr {r['rr']}; block priority>= {thr}"
        return "ADVISORY-KEEP", "advisory", 0.95, f"validity {mv}, drop {r['p_drop']}, rev_rr {r['rr']}; real but borderline => advisory"

    print("\n=== PROPOSAL (precision-first; n<%d => LOW-DATA/interactive) ===" % MIN_N)
    print("A proposal is a DELTA against what is committed; `change` is empty when the proposal")
    print("matches the routing already in force. UNROUTED marks a criterion with no routing entry")
    print(f"at all -- it silently takes the {UNROUTED_THRESHOLD:.2f} unknown-label default,")
    print("which is a routing bug when the criterion carries real volume.")
    print("the criterion carries real volume, not a deliberate advisory posture.\n")
    print(f"{'crit':<26}{'now':>10}{'n':>5}  {'class':<15}{'posture':<10}{'thr':>6}  {'change':<22}rationale")
    proposal = []
    for r in rows:
        cls, posture, thr, rat = classify(r)
        now = _posture_label(r, short=True)
        if cls == "DET/ATTEST":
            change = ""
        elif not r["routed"] and posture == "blocking":
            change = f"ROUTE -> blk@{thr:.2f}"
        elif posture == "blocking" and not r["cur_blocking"]:
            change = f"PROMOTE -> blk@{thr:.2f}"
        elif posture == "advisory" and r["cur_blocking"]:
            change = "DEMOTE -> advisory"
        elif posture == "blocking" and abs(thr - r["cur_thr"]) > 1e-9:
            change = f"RETUNE {r['cur_thr']:.2f} -> {thr:.2f}"
        else:
            change = ""
        proposal.append((r, cls, posture, thr, rat, change))
        print(f"{r['c']:<26}{now:>10}{r['n']:>5}  {cls:<15}{posture:<10}{thr:>6.2f}  {change:<22}{rat}")

    if args.emit:
        _emit_report(args.emit, hdr_lines, rows, proposal, args.impact_model_version)
        print(f"\nreport -> {args.emit}")


def _emit_report(path: str, hdr_lines, rows, proposal, version) -> None:
    L = [f"# Code-review threshold calibration ({version})\n"]
    L += [f"{ln}\n" for ln in hdr_lines]
    L.append("\n## Per-criterion signals\n\n")
    L.append("| criterion | in force | n | surf | fire | mval | drop | indet | pblk | rev_rr | elig | p75 | p90 | p95 | pmax | worst subq (no-rate) |\n")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for r in rows:
        now = _posture_label(r)
        L.append(f"| {r['c']} | {now} | {r['n']} | {r['surf']} | {r['fire']} | {r['mv']} | {r['p_drop']} | "
                 f"{r['p_indet']} | {r['p_block']} | {r['rr']} | {r['elig']} | {r['p75']} | {r['p90']} | "
                 f"{r['p95']} | {r['pmax']} | {r['worst_q']} ({r['worst_rate']}) |\n")
    L.append("\n## Precision-first proposal\n\n")
    L.append("| criterion | in force | n | class | posture | threshold | change | rationale |\n"
             "|---|---|---|---|---|---|---|---|\n")
    for r, cls, posture, thr, rat, change in proposal:
        now = _posture_label(r)
        L.append(f"| {r['c']} | {now} | {r['n']} | {cls} | {posture} | {thr:.2f} "
                 f"| {change or '-'} | {rat} |\n")
    open(path, "w").write("".join(L))


if __name__ == "__main__":
    main()
