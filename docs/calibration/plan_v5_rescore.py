"""Zero-LLM re-score of the recorded plan-review corpus under the plan-v5 kind sets.

Evidence gate for story fixable-angular-caribou (ADR 0054: replay the persisted
REVIEW_RESULT sidecars through the SHIPPED compose functions; no LLM, no eval suite).

Each recorded finding carries `verification.severity_attributes` with the PRE-plan-v5
ORDINAL grades (`low|medium|high`) on `undecomposed` / `dod_uncertifiable`. Those rows are
read as RAW dicts -- never through the new `Literal` model, which rejects the old labels.
Every graded row is mapped to its nearest plan-v5 KIND by the documented rules below, then
the finding is re-scored and its decision compared against the recorded one.

The decision boundary is `priority >= block_threshold` where `priority = validity x impact`
(decide.pass3_decide), so "loses its block" means the re-scored priority falls below the
threshold the finding was actually judged against.
"""

from __future__ import annotations

import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from rebar.llm.evals.plan_replay.legacy_v4_baseline import (
    ADVISORY_KIND,
    DOD_RULES,
    FLOOR_KINDS,
    RULES,
    UNDECOMPOSED_RULES,
    classify,
    legacy_impact_plan,
)
from rebar.llm.review_kernel import decide

__all__ = [
    "ADVISORY_KIND",
    "DOD_RULES",
    "FLOOR_KINDS",
    "RULES",
    "UNDECOMPOSED_RULES",
    "classify",
    "legacy_impact_plan",
]


def load_corpus(root: str) -> list[dict]:
    rows: list[dict] = []
    for path in glob.glob(os.path.join(root, ".tickets-tracker", "*", "*REVIEW_RESULT.json")):
        try:
            data = json.load(open(path)).get("data") or {}
        except Exception:  # noqa: BLE001 - a malformed sidecar is skipped, never fatal
            continue
        if not str(data.get("schema") or "").startswith("plan_review"):
            continue
        for f in data.get("findings") or []:
            attrs = (f.get("verification") or {}).get("severity_attributes")
            bt = f.get("block_threshold")
            if not isinstance(attrs, dict) or not isinstance(bt, (int, float)):
                continue
            rows.append(
                {
                    "attrs": attrs,
                    "bt": float(bt),
                    "validity": float(f.get("validity") or 0.0),
                    "decision": f.get("decision"),
                    "criteria": f.get("criteria"),
                    "text": (f.get("finding") or "") + " " + " ".join(f.get("evidence") or []),
                }
            )
    return rows


def rescore(attrs: dict, kinds: dict[str, str]) -> float:
    """impact_plan as it will behave in plan-v5, computed from the SHIPPED constants."""
    graded = ("ac_unverifiable", "divergent_implementation", "undecomposed", "dod_uncertifiable")
    contribs = [
        decide._SEV01.get(attrs.get(a), 0.0) for a in decide._PLAN_SEVERITY_AXES if a not in graded
    ]
    contribs.append(decide.ORACLE_GRADE01.get(attrs.get("ac_unverifiable"), 0.0))
    contribs.append(decide.DIVERGENCE_GRADE01.get(attrs.get("divergent_implementation"), 0.0))
    for axis in ("undecomposed", "dod_uncertifiable"):
        contribs.append(decide._PLAN_GRADE_MAPS[axis].get(kinds.get(axis, "none"), 0.0))
    sev = max(contribs) if contribs else 0.0
    mult = 0.8 if attrs.get("silent_vs_self_revealing") == "self_revealing" else 1.0
    if kinds.get("dod_uncertifiable", "none") != "none":
        mult = 1.0
    amplified = min(1.0, sev * mult)
    override = (
        attrs.get("ac_unverifiable") in decide._ORACLE_FLOOR_GRADES
        or attrs.get("divergent_implementation") in decide._DIVERGENCE_FLOOR_GRADES
        or any(
            kinds.get(a, "none") in FLOOR_KINDS[a] for a in ("undecomposed", "dod_uncertifiable")
        )
    )
    result = max(amplified, decide._PLAN_HARD_OVERRIDE_FLOOR) if override else amplified
    return round(result, 4)


def main() -> int:
    # The residual (a graded row no rule matches) is scored BOTH ways so the lost-block count
    # is reported as a bound, not a point estimate resting on an unstated default.
    #   --residual=advisory (default) -> LOWER bound on retained blocks / UPPER bound on losses
    #   --residual=floor              -> the opposite bound
    residual_mode = "advisory"
    # The corpus is the ticket store's REVIEW_RESULT sidecars, which live in the checkout that
    # holds `.tickets-tracker/` — not necessarily this one (a fresh worktree has no tracker).
    root_override = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--residual="):
            residual_mode = arg.split("=", 1)[1]
        elif arg.startswith("--root="):
            root_override = arg.split("=", 1)[1]
    if residual_mode not in ("advisory", "floor"):
        print("usage: plan_v5_rescore.py [--residual=advisory|floor] [--root=<checkout>]")
        return 2
    root = os.path.abspath(root_override or os.path.join(os.path.dirname(__file__), "..", ".."))
    rows = load_corpus(root)
    if not rows:
        print(f"no corpus under {root}/.tickets-tracker — pass --root=<checkout with the store>")
        return 1
    print(f"residual mode: {residual_mode}")
    print(f"corpus: {len(rows)} plan findings carrying severity_attributes")

    unmatched: collections.Counter = collections.Counter()
    lost: list[dict] = []
    gained = 0
    old_at_bar = new_at_bar = 0
    kind_counts: collections.Counter = collections.Counter()

    for row in rows:
        attrs, bt, val = row["attrs"], row["bt"], row["validity"]
        kinds: dict[str, str] = {}
        residual = False
        for axis in ("undecomposed", "dod_uncertifiable"):
            grade = attrs.get(axis)
            if grade in (None, "none"):
                kinds[axis] = "none"
                continue
            kind = classify(axis, row["text"])
            if kind is None:
                unmatched[axis] += 1
                residual = True
                kind = ADVISORY_KIND[axis] if residual_mode == "advisory" else FLOOR_KINDS[axis][0]
            kinds[axis] = kind
            kind_counts[(axis, kind)] += 1

        old_block = round(legacy_impact_plan(attrs) * val, 4) >= bt
        new_block = round(rescore(attrs, kinds) * val, 4) >= bt
        old_at_bar += old_block
        new_at_bar += new_block
        if old_block and not new_block:
            lost.append({**row, "kinds": kinds, "residual": residual})
        if new_block and not old_block:
            gained += 1

    print(f"at/above block threshold: old={old_at_bar}  new={new_at_bar}")
    print(f"lost={len(lost)}  gained={gained}")
    print(f"unmatched-by-rule (scored as {residual_mode}): {dict(unmatched)}")
    print("kind distribution:")
    for (axis, kind), n in sorted(kind_counts.items()):
        print(f"  {axis:20s} {kind:30s} {n}")
    print("\nLOST BLOCKS (each must be a non-gap finding, or the R1 bar says ESCALATE):")
    for item in lost:
        axes = {a: k for a, k in item["kinds"].items() if k != "none"}
        print(f"  - {axes} bt={item['bt']} decision={item['decision']} crit={item['criteria']}")
        print(f"    {' '.join(item['text'].split())[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
