"""BLOCK-loop remediation eligibility (story a850): a ticket iterating under consecutive
BLOCK verdicts never mints a signature, so eligibility must reach the floor path through the
SIDECAR baseline — review → BLOCK → plan edit → re-review is eligible on round 2, with no
signature anywhere. Offline end-to-end: a DET-floor BLOCK (missing Acceptance Criteria fails
P1 unconditionally) with a schema-aware fake runner, so no model/network."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar.llm.plan_review import sidecar
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.interface


class _GateFake(FakeRunner):
    """Shape-valid offline runner for the plan-review passes (mirrors the lifecycle gate
    tests' fake): empty finder output, canned verifications, empty coach notes."""

    name = "fake"

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _f

        schema = req.output_schema
        if req.mode == "text":
            return {"text": "[fake summary]", "runner": self.name, "model": None, "trace_id": None}
        if schema == "plan_review_verification":
            idxs = [int(x) for x in re.findall(r"finding index (\d+)", req.instructions or "")]
            payload = {"verifications": [{"index": i} for i in idxs]}
        elif schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


# No "## Acceptance Criteria" block → the DET floor's P1 readiness-shape check BLOCKS
# unconditionally, so every round is a BLOCK and no attestation is ever signed.
_BLOCKING_DESC = (
    "A long-enough plan body that still fails the deterministic readiness floor because it "
    "carries no Acceptance Criteria checklist at all, exercising the BLOCK-loop regime the "
    "sidecar-baseline fallback exists for.\n\n## What\nchange a thing\n## Why\nbecause\n"
)


def test_block_loop_reaches_remediation_eligibility_without_signature(rebar_repo: Path) -> None:
    repo = str(rebar_repo)
    # the local-mode SHA fallback reads the committed HEAD — give the repo one (real repos have it)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    tid = rebar.create_ticket("task", "block loop", description=_BLOCKING_DESC, repo_root=repo)

    # Round 1: BLOCK; the sidecar must carry the a850 baseline stamps.
    v1 = rebar.llm.review_plan(tid, runner=_GateFake(), repo_root=repo)
    assert v1["verdict"] == "BLOCK"
    s1 = sidecar.latest_review_result(tid, repo_root=repo)
    assert s1 is not None
    assert s1.get("material_fingerprint")
    assert s1.get("verified_at_sha")  # git-HEAD fallback in local mode
    assert s1.get("regver")

    # Remediate the plan (material changes) — but it still BLOCKs (no AC block yet),
    # so no signature exists anywhere in the loop.
    rebar.edit_ticket(
        tid,
        description=_BLOCKING_DESC + "\nAn edited paragraph — the remediation attempt.\n",
        repo_root=repo,
    )

    # Round 2: eligibility must be TRUE via the sidecar baseline (AC4's oracle).
    v2 = rebar.llm.review_plan(tid, runner=_GateFake(), repo_root=repo)
    assert v2["verdict"] == "BLOCK"
    rem = (v2.get("coverage") or {}).get("remediation") or {}
    assert rem.get("eligible") is True
    assert rem.get("baseline") == "sidecar"
    s2 = sidecar.latest_review_result(tid, repo_root=repo)
    rem2 = (s2.get("coverage") or {}).get("remediation") or {}
    assert rem2.get("eligible") is True
