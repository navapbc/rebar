"""Unit tests for compute_validity — the per-kind validity-on-read dispatcher (story 929e,
epic dark-acme-lumen). These exercise the branches directly with constructed records/state
(the end-to-end paths are covered by test_attested_signing + test_plan_review_gate)."""

from __future__ import annotations

from rebar.llm.plan_review import attest
from rebar.llm.plan_review.attest import compute_validity


def _fp(monkeypatch, value):
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda tid, repo_root=None: value)


# ── completion-verifier ─────────────────────────────────────────────────────────
def test_completion_valid_when_closed_unreopened_material_matches(monkeypatch) -> None:
    _fp(monkeypatch, "m1")
    att = {"manifest": ["completion-verifier: PASS", "material: m1"], "signed_at": 100}
    state = {"ticket_id": "t", "status": "closed"}
    assert compute_validity(att, state, "completion-verifier")["valid"] is True


def test_completion_invalid_when_not_closed(monkeypatch) -> None:
    _fp(monkeypatch, "m1")
    att = {"manifest": ["completion-verifier: PASS", "material: m1"], "signed_at": 100}
    state = {"ticket_id": "t", "status": "open"}
    res = compute_validity(att, state, "completion-verifier")
    assert res["valid"] is False and res["verdict"] == "not-closed"


def test_completion_invalid_when_reopened_after_signing(monkeypatch) -> None:
    _fp(monkeypatch, "m1")
    att = {"manifest": ["completion-verifier: PASS", "material: m1"], "signed_at": 100}
    # Re-closed (status closed) but last reopen is AFTER signing → stale.
    state = {"ticket_id": "t", "status": "closed", "last_reopened_at": 200}
    res = compute_validity(att, state, "completion-verifier")
    assert res["valid"] is False and res["verdict"] == "stale-reopened"


def test_completion_invalid_when_material_changed(monkeypatch) -> None:
    _fp(monkeypatch, "m2")  # current != signed "m1"
    att = {"manifest": ["completion-verifier: PASS", "material: m1"], "signed_at": 100}
    state = {"ticket_id": "t", "status": "closed"}
    res = compute_validity(att, state, "completion-verifier")
    assert res["valid"] is False and res["verdict"] == "stale-material"


def test_completion_valid_when_reclosed_after_reopen(monkeypatch) -> None:
    _fp(monkeypatch, "m1")
    # Re-signed AFTER the reopen (signed_at > last_reopened_at) → valid again.
    att = {"manifest": ["completion-verifier: PASS", "material: m1"], "signed_at": 300}
    state = {"ticket_id": "t", "status": "closed", "last_reopened_at": 200}
    assert compute_validity(att, state, "completion-verifier")["valid"] is True


# ── plan-review (unscoped: no dep map → whole-HEAD freshness) ───────────────────
# Every production plan-review manifest carries a regver stamp. Since ADR 0053 a stamp that no
# longer matches the current (overlay-aware) registry_version — or a MISSING stamp — is
# GRANDFATHERED: reported as non-blocking ``registry_drift``, never invalidating. These unscoped
# tests carry a matching stamp so the head/material checks under test are the only variable.
def _regver(monkeypatch, value="rv0") -> str:
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: value)
    return f"regver: {value}"


def test_plan_review_valid_when_head_and_material_match(monkeypatch) -> None:
    _fp(monkeypatch, "pm")
    rv = _regver(monkeypatch)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {
        "manifest": ["plan-review: PASS", "material: pm", rv],
        "head_sha": "headA",
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    assert compute_validity(att, state, "plan-review")["valid"] is True


def test_plan_review_invalid_on_head_drift(monkeypatch) -> None:
    _fp(monkeypatch, "pm")
    rv = _regver(monkeypatch)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headB")
    att = {
        "manifest": ["plan-review: PASS", "material: pm", rv],
        "head_sha": "headA",
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is False and res["verdict"] == "stale-head"


def test_plan_review_grandfathered_when_regver_changed(monkeypatch) -> None:
    """ADR 0053: a rotated criteria-registry stamp is GRANDFATHERED — the attestation stays
    valid (nothing about the plan or the code moved) and the drift is reported instead."""
    _fp(monkeypatch, "pm")
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv-NEW")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {
        "manifest": ["plan-review: PASS", "material: pm", "regver: rv-OLD"],
        "head_sha": "headA",
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is True and res["verdict"] == "certified"
    assert res["registry_drift"] == {"signed": "rv-OLD", "current": "rv-NEW"}


def test_plan_review_grandfathered_when_regver_missing(monkeypatch) -> None:
    """An older manifest with no ``regver:`` line is grandfathered too, and reports a
    ``None`` signed stamp so the drift is still visible."""
    _fp(monkeypatch, "pm")
    _regver(monkeypatch)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {"manifest": ["plan-review: PASS", "material: pm"], "head_sha": "headA", "signed_at": 100}
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is True and res["verdict"] == "certified"
    assert res["registry_drift"] == {"signed": None, "current": "rv0"}


def test_regver_drift_does_not_mask_other_staleness(monkeypatch) -> None:
    """Only the REGISTRY dimension was grandfathered: an attestation that is both
    regver-drifted AND head-drifted still fails on the head axis, carrying the drift report."""
    _fp(monkeypatch, "pm")
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv-NEW")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headB")
    att = {
        "manifest": ["plan-review: PASS", "material: pm", "regver: rv-OLD"],
        "head_sha": "headA",
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is False and res["verdict"] == "stale-head"
    assert res["registry_drift"] == {"signed": "rv-OLD", "current": "rv-NEW"}


def test_no_registry_drift_key_when_regver_matches(monkeypatch) -> None:
    """A matching stamp reports nothing — ``registry_drift`` is absent, not a null."""
    _fp(monkeypatch, "pm")
    rv = _regver(monkeypatch)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {
        "manifest": ["plan-review: PASS", "material: pm", rv],
        "head_sha": "headA",
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is True and "registry_drift" not in res


def test_plan_review_invalid_when_reopened(monkeypatch) -> None:
    _fp(monkeypatch, "pm")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {"manifest": ["plan-review: PASS", "material: pm"], "head_sha": "headA", "signed_at": 100}
    state = {"ticket_id": "t", "status": "in_progress", "last_reopened_at": 150}
    assert compute_validity(att, state, "plan-review")["valid"] is False


def test_none_attestation_is_invalid() -> None:
    assert compute_validity(None, {"status": "closed"}, "completion-verifier")["valid"] is False
