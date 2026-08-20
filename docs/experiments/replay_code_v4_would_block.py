#!/usr/bin/env python3
"""Offline v3→v4 would-block replay for the code-review impact model (ticket 7f9f — code-v4).

Re-scores the stored **code-v3 REVIEW_RESULT sidecar corpus** under the NEW ``impact_code``
(code-v4) WITHOUT rewriting any sidecar — this is pure offline analysis, exactly the "replay =
offline analysis, not a migration" contract AC1 makes. For every finding the verifier produced
a ``severity_attributes`` set for, it recomputes ``priority = validity × impact_code(attrs)``
and re-applies each criterion's PACKAGED ``(block_threshold, blocking_enabled)`` posture (with
``tests`` now blocking@0.54), then reports how many findings/changes WOULD block under v4 vs.
the stored v3 decision. On the recorded corpus (1881 sidecars / 1243 changes) this reproduces
the ticket's accepted set: **v3 61 findings / 42 changes → v4 163 findings / 99 changes**, zero
demotions.

The corpus is NOT committed (it lives in the tracker's sidecar store, like the inputs to
``calibrate_code_review_thresholds.py``); point ``--tracker`` at a checkout that has it to
reproduce 163/99. With no corpus the script runs a **deterministic, corpus-free self-check**
(the default) that pins the two invariants a reviewer can verify anywhere — no model, no
network, no CI provider required (portability):

  1. a contract-contradicting fire case (``forbids_contract_allowed_state`` serious, prod
     medium, silent) BLOCKS under v4 + ``tests``@0.54; and
  2. a debt-only + hard_to_reverse finding stays ADVISORY (the reversibility floor is
     consequence-lane-gated, so debt never floors) — one of the 4 corpus findings AC7 pins.

Run (self-check, always available):  python docs/experiments/replay_code_v4_would_block.py
Run (full corpus replay):            python docs/experiments/replay_code_v4_would_block.py \
                                         --tracker /path/to/checkout/.tickets-tracker
Exit 0 iff the self-check invariants hold (or, in --tracker mode, iff the replay completes).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any

from rebar.llm.code_review.registry import threshold_for
from rebar.llm.review_kernel.decide import impact_code, pass3_decide

# The packaged v4 blocking set (routing) — the criteria whose posture can BLOCK. Kept here only
# for the human-readable summary; the actual gate uses the packaged threshold_for per finding.
_BLOCKING_SET = (
    "secret-detection",
    "high-critical-security",
    "security",
    "api-compat",
    "deletion-impact",
    "regression",
    "error-handling",
    "tests",
)


def _would_block(finding: dict[str, Any]) -> bool:
    """Re-decide ONE stored finding under v4 impact_code + the packaged code-review posture."""
    verification = finding.get("verification") or {
        "binary": finding.get("binary", {}),
        "severity_attributes": finding.get("severity_attributes", {}),
    }
    criteria = finding.get("criteria") or finding.get("criteria_ids") or []
    block_threshold, blocking_enabled = threshold_for(list(criteria))
    decision = pass3_decide(
        verification,
        block_threshold=block_threshold,
        blocking_enabled=blocking_enabled,
        impact_fn=impact_code,
    )
    return decision["decision"] == "block"


def _iter_findings(tracker: str):
    """Yield (change_id, finding) over every code-v3 REVIEW_RESULT sidecar under ``tracker``."""
    pattern = os.path.join(tracker, "**", "*-REVIEW_RESULT.json")
    for fp in glob.glob(pattern, recursive=True):
        try:
            doc = json.loads(open(fp, encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        if doc.get("impact_model_version") not in (None, "code-v3"):
            continue
        change_id = doc.get("change_id") or doc.get("ticket_id") or fp
        for bucket in ("blocking", "advisory", "dropped", "indeterminate", "coaching"):
            for finding in doc.get(bucket, []) or []:
                yield change_id, finding


def _replay_corpus(tracker: str) -> int:
    v3_block_findings = v4_block_findings = 0
    v3_changes: set[str] = set()
    v4_changes: set[str] = set()
    total = 0
    for change_id, finding in _iter_findings(tracker):
        total += 1
        if finding.get("decision") == "block":
            v3_block_findings += 1
            v3_changes.add(change_id)
        if _would_block(finding):
            v4_block_findings += 1
            v4_changes.add(change_id)
    print(f"corpus: {total} findings under {tracker}")
    print(f"  v3 (stored)   : {v3_block_findings} findings / {len(v3_changes)} changes block")
    print(f"  v4 (impact_code): {v4_block_findings} findings / {len(v4_changes)} changes block")
    print(f"  blocking set  : {', '.join(_BLOCKING_SET)}")
    return 0


# ── the deterministic, corpus-free self-check ────────────────────────────────────

_FIRE_CASE = {
    "criteria": ["tests"],
    "verification": {
        "binary": {
            "is_verifiable": "yes",
            "evidence_entails_finding": "yes",
            "path_reachable": "yes",
            "impact_follows_necessarily": "yes",
            "no_viable_alternative_explanation": "yes",
            "no_existing_mitigation": "yes",
            "severity_claim_justified": "yes",
        },
        "severity_attributes": {
            "forbids_contract_allowed_state": True,
            "prod_impact": "medium",
            "trigger_likelihood": "sometimes",
            "silent_failure": True,
        },
    },
}

_DEBT_ONLY_HARD_TO_REVERSE = {
    "criteria": ["tests"],
    "verification": {
        "binary": {
            "is_verifiable": "yes",
            "evidence_entails_finding": "yes",
            "path_reachable": "yes",
            "impact_follows_necessarily": "yes",
            "no_viable_alternative_explanation": "yes",
            "no_existing_mitigation": "yes",
            "severity_claim_justified": "yes",
        },
        "severity_attributes": {
            "implicit_coupling": True,
            "dead_code": True,
            "hard_to_reverse_surface": True,
            "prod_impact": "none",
        },
    },
}


def _self_check() -> int:
    fire_priority = impact_code(_FIRE_CASE["verification"]["severity_attributes"])
    debt_priority = impact_code(_DEBT_ONLY_HARD_TO_REVERSE["verification"]["severity_attributes"])
    fire_blocks = _would_block(_FIRE_CASE)
    debt_blocks = _would_block(_DEBT_ONLY_HARD_TO_REVERSE)
    print("self-check (corpus-free, deterministic):")
    print(f"  contract-contradiction fire case : impact={fire_priority:.4f} block={fire_blocks}")
    print(f"  debt-only + hard_to_reverse      : impact={debt_priority:.4f} block={debt_blocks}")
    ok = True
    if not fire_blocks:
        print("  FAIL: fire case must BLOCK under v4 + tests@0.54")
        ok = False
    if debt_blocks:
        print("  FAIL: debt-only + hard_to_reverse must stay ADVISORY (floor is consequence-gated)")
        ok = False
    if debt_priority >= 0.54:
        print(f"  FAIL: debt-only score {debt_priority:.4f} reached the tests block threshold")
        ok = False
    print("  PASS" if ok else "  FAILED")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tracker",
        default=None,
        help="Path to a .tickets-tracker holding the code-v3 sidecar corpus (reproduces 163/99). "
        "Omit to run the deterministic corpus-free self-check.",
    )
    args = ap.parse_args()
    if args.tracker:
        sys.exit(_replay_corpus(args.tracker))
    sys.exit(_self_check())


if __name__ == "__main__":
    main()
