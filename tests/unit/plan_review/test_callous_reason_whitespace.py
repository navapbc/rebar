"""No-file-impact reason whitespace follows the material normalization contract."""

from __future__ import annotations

import hashlib
import json

import pytest

from rebar.llm.plan_review import attest, relation_snapshot
from rebar.llm.plan_review.attest import compute_validity
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import material_fingerprint

pytestmark = pytest.mark.unit

_DESCRIPTION = "## Approach\nDo the thing.  \n\n## Acceptance Criteria\n- [x] the thing is done\n"
_CANONICAL_DESCRIPTION = (
    "## Approach\nDo the thing.\n\n## Acceptance Criteria\n- [ ] the thing is done"
)
_CHECKBOX_ONLY_DESCRIPTION = _DESCRIPTION.replace("- [x]", "- [ ]")


def _ctx(reason: str, ticket_id: str = "t-callous") -> PlanContext:
    return PlanContext(
        ticket_id=ticket_id,
        ticket_type="bug",
        title="reason whitespace fixture",
        description=_DESCRIPTION,
        state={
            "ticket_id": ticket_id,
            "file_impact": [],
            "file_impact_scope": "none",
            "no_file_impact_reason": reason,
        },
    )


def _state(reason: str, ticket_id: str = "t-callous") -> dict:
    ctx = _ctx(reason, ticket_id)
    return {**ctx.state, "status": "in_progress", "description": ctx.description}


def _wire_state(monkeypatch: pytest.MonkeyPatch, state: dict) -> None:
    import rebar._reads as _reads

    monkeypatch.setattr(_reads, "show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr(relation_snapshot, "live_material_children", lambda tid, repo_root=None: [])


def _plan_attestation(signed_material: str) -> dict:
    return {
        "manifest": ["plan-review: PASS", f"material: {signed_material}", "regver: rv0"],
        "head_sha": "headA",
        "signed_at": 100,
    }


def _historical_hash(ticket_id: str, description: str, reason: str) -> str:
    """Hash an historical basis independently of the production canonicalizer."""
    basis = {
        "ticket_id": ticket_id,
        "description": description,
        "file_impact": [],
        "children": [],
        "file_impact_scope": {"kind": "none", "reason": reason},
    }
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@pytest.mark.parametrize("variant", ["docs only ", "docs only\n", "\ndocs only"])
def test_boundary_reason_whitespace_keeps_current_fingerprint(variant: str) -> None:
    assert material_fingerprint(_ctx(variant)) == material_fingerprint(_ctx("docs only"))


def test_reason_whitespace_only_edit_leaves_plan_review_certified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")

    signed_state = _state("docs only")
    _wire_state(monkeypatch, signed_state)
    signed = attest.current_material_fingerprint("t-callous")
    assert signed is not None

    edited_state = _state("docs only ")
    _wire_state(monkeypatch, edited_state)
    result = compute_validity(_plan_attestation(signed), edited_state, "plan-review")

    assert result["valid"] is True, result
    assert result["verdict"] == "certified", result


@pytest.mark.parametrize("meaningful", ["docs  only", "runtime only"])
def test_meaningful_reason_edit_still_reads_stale_material(
    monkeypatch: pytest.MonkeyPatch, meaningful: str
) -> None:
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")

    signed_state = _state("docs only")
    _wire_state(monkeypatch, signed_state)
    signed = attest.current_material_fingerprint("t-callous")
    assert signed is not None

    edited_state = _state(meaningful)
    _wire_state(monkeypatch, edited_state)
    result = compute_validity(_plan_attestation(signed), edited_state, "plan-review")

    assert result["valid"] is False, result
    assert result["verdict"] == "stale-material", result


@pytest.mark.parametrize(
    "historical_description",
    [_DESCRIPTION, _CHECKBOX_ONLY_DESCRIPTION, _CANONICAL_DESCRIPTION],
    ids=["pre-330c", "post-330c-pre-2be7", "post-2be7-pre-reason-fix"],
)
def test_raw_reason_historical_generations_are_grandfathered(
    monkeypatch: pytest.MonkeyPatch, historical_description: str
) -> None:
    """All three old generations used a raw reason and must remain byte-reproducible."""
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")

    reason = "docs only "
    state = _state(reason)
    _wire_state(monkeypatch, state)
    signed = _historical_hash("t-callous", historical_description, reason)

    result = compute_validity(_plan_attestation(signed), state, "plan-review")

    assert result["valid"] is True, result
    assert result["verdict"] == "certified", result


def test_unrelated_valid_fingerprint_remains_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    state = _state("docs only ")
    _wire_state(monkeypatch, state)

    result = compute_validity(_plan_attestation("0" * 16), state, "plan-review")

    assert result["valid"] is False, result
    assert result["verdict"] == "stale-material", result
