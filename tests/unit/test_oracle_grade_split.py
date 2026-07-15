"""ac_unverifiable oracle-kind grade split (story large-sleepful-needlefish, plan-v3).

The hard floor for ac_unverifiable is keyed on WHICH oracle defect the finding names:
missing_oracle / broken_oracle keep the 0.85 auto-high; underspecified_oracle (56% of the
calibration-3 floor-driven sample) scores below every blocking threshold and never floors.
The closed grade set is enforced at verification-parse time by the Pydantic Literal;
legacy plan-v2 sidecars are read as-is (ADR 0036 segmentation is the back-compat seam).

Proving command:
    .venv/bin/pytest tests/unit/test_oracle_grade_split.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pydantic
import pytest

from rebar.llm.review_kernel.verify import plan_review_verification_model

pytestmark = pytest.mark.unit


@pytest.fixture
def rebar_repo(tmp_path, monkeypatch):
    """A self-contained initialized rebar repo (this unit dir has no shared fixture)."""
    import subprocess

    import rebar

    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


ORACLE_GRADES = ("none", "underspecified_oracle", "broken_oracle", "missing_oracle")


# ── closed-set enforcement at verification-parse time ─────────────────────────────────────
def _validate(grade: str, *, strict: bool = False):
    model = plan_review_verification_model(strict=strict)
    return model.model_validate(
        {"verifications": [{"index": 0, "severity_attributes": {"ac_unverifiable": grade}}]}
    )


def test_every_oracle_grade_is_accepted() -> None:
    for grade in ORACLE_GRADES:
        v = _validate(grade)
        assert v.verifications[0].severity_attributes.ac_unverifiable == grade


def test_out_of_vocabulary_grade_is_rejected() -> None:
    # The failure path of the closed-set contract: the legacy ordinal ladder and arbitrary
    # strings are parse errors, in strict AND non-strict modes (Literal is unconditional).
    for bad in ("low", "medium", "high", "unverifiable", ""):
        for strict in (False, True):
            with pytest.raises(pydantic.ValidationError):
                _validate(bad, strict=strict)


# ── operator-attested enrich asymmetry ─────────────────────────────────────────────────────
_OA_DESC = (
    "A plan with an operational criterion.\n\n## Acceptance Criteria\n"
    "- [ ] [operator-attested] the fix is deployed to prod and the gate passes\n"
)


def _enriched_axis(grade: str) -> str:
    from rebar.llm.plan_review import workflow_ops

    finding = {
        "checklist_item": "[operator-attested] the fix is deployed to prod and the gate passes",
        "evidence": [],
    }
    verification = {"severity_attributes": {"ac_unverifiable": grade}}
    workflow_ops.enrich_operator_attested([finding], {0: verification}, _OA_DESC)
    return verification["severity_attributes"]["ac_unverifiable"]


def test_attestation_clears_missing_and_underspecified_but_never_broken() -> None:
    # A recorded attestation IS the oracle — but it cannot cure a factually wrong
    # stated command, so broken_oracle survives enrichment.
    assert _enriched_axis("missing_oracle") == "none"
    assert _enriched_axis("underspecified_oracle") == "none"
    assert _enriched_axis("broken_oracle") == "broken_oracle"


# ── sidecar persistence + legacy read-as-is ────────────────────────────────────────────────
def test_grade_persists_in_sidecar_payload() -> None:
    from rebar.llm.plan_review.sidecar import build_payload

    verdict = {
        "verdict": "BLOCK",
        "ticket_id": "t",
        "blocking": [
            {
                "id": "f1",
                "criteria": ["F1"],
                "finding": "x",
                "verification": {
                    "severity_attributes": {"ac_unverifiable": "underspecified_oracle"}
                },
            }
        ],
    }
    payload = build_payload(verdict, material="m")
    sa = payload["findings"][0]["verification"]["severity_attributes"]
    assert sa["ac_unverifiable"] == "underspecified_oracle"
    assert payload["impact_model_version"] == "plan-v3"


def test_legacy_plan_v2_sidecar_reads_as_is(rebar_repo: Path) -> None:
    # Back-compat is segmentation, not migration: a stored plan-v2 record with the legacy
    # ordinal grade round-trips unmodified through the sidecar readers.
    from rebar.llm.plan_review import sidecar

    tracker = Path(rebar_repo) / ".tickets-tracker" / "t-legacy"
    tracker.mkdir(parents=True, exist_ok=True)
    legacy = {
        "schema": "plan_review_result_v2",
        "ticket_id": "t-legacy",
        "verdict": "BLOCK",
        "impact_model_version": "plan-v2",
        "findings": [
            {
                "id": "f1",
                "verification": {"severity_attributes": {"ac_unverifiable": "low"}},
            }
        ],
    }
    (tracker / "100-REVIEW_RESULT.json").write_text(json.dumps({"data": legacy}))
    got = sidecar.latest_review_result("t-legacy", repo_root=str(rebar_repo))
    assert got is not None
    assert got["impact_model_version"] == "plan-v2"
    sa = got["findings"][0]["verification"]["severity_attributes"]
    assert sa["ac_unverifiable"] == "low"  # unmodified — never rewritten to the new vocabulary
