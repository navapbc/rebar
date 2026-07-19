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
# Every production plan-review manifest carries a regver stamp; compute_validity now treats a
# stamp that no longer matches the current (overlay-aware) registry_version — or a MISSING stamp —
# as stale-regver (story 08af). These unscoped tests carry a matching stamp to reach the head/
# material checks under test.
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


def test_plan_review_unscoped_unrelated_head_drift_stays_valid(monkeypatch) -> None:
    """Bug 5e40: an UNSCOPED (no dep map), previously-certified plan whose MATERIAL
    fingerprint is unchanged must NOT be invalidated when HEAD advances by an unrelated
    commit. Freshness for an unscoped plan is anchored on the bound material fingerprint,
    not a bare whole-HEAD SHA equality — so unrelated head drift no longer forces a
    stale-head escalation to a non-deterministic full re-review."""
    _fp(monkeypatch, "pm")  # current material == signed "pm" (unchanged)
    rv = _regver(monkeypatch)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headB")  # drifted
    att = {
        "manifest": ["plan-review: PASS", "material: pm", rv],
        "head_sha": "headA",  # signed at headA; HEAD moved to headB (unrelated)
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is True and res["verdict"] == "certified"


def test_plan_review_unscoped_material_change_still_invalidates(monkeypatch) -> None:
    """Guard against over-loosening: when the plan's MATERIAL actually changed, an
    unscoped attestation MUST still invalidate (stale-material). Head is held MATCHING
    here so the material change is the sole trigger — this guard is independent of the
    fix and must stay GREEN both ways (before and after)."""
    _fp(monkeypatch, "pm-NEW")  # current material != signed "pm"
    rv = _regver(monkeypatch)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")  # matches
    att = {
        "manifest": ["plan-review: PASS", "material: pm", rv],
        "head_sha": "headA",
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is False and res["verdict"] == "stale-material"


def test_plan_review_unscoped_head_drift_stale_when_no_material_bound(monkeypatch) -> None:
    """Fail-closed fallback: a material-LESS unscoped attestation has nothing to anchor
    freshness on, so the conservative whole-HEAD check still fires on head drift."""
    _fp(monkeypatch, None)
    rv = _regver(monkeypatch)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headB")
    att = {
        "manifest": ["plan-review: PASS", rv],  # no material line bound
        "head_sha": "headA",
        "signed_at": 100,
    }
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is False and res["verdict"] == "stale-head"


def test_plan_review_stale_when_regver_changed(monkeypatch) -> None:
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
    assert res["valid"] is False and res["verdict"] == "stale-regver"


def test_plan_review_stale_when_regver_missing(monkeypatch) -> None:
    _fp(monkeypatch, "pm")
    _regver(monkeypatch)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {"manifest": ["plan-review: PASS", "material: pm"], "head_sha": "headA", "signed_at": 100}
    state = {"ticket_id": "t", "status": "in_progress"}
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is False and res["verdict"] == "stale-regver"


def test_plan_review_invalid_when_reopened(monkeypatch) -> None:
    _fp(monkeypatch, "pm")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {"manifest": ["plan-review: PASS", "material: pm"], "head_sha": "headA", "signed_at": 100}
    state = {"ticket_id": "t", "status": "in_progress", "last_reopened_at": 150}
    assert compute_validity(att, state, "plan-review")["valid"] is False


def test_none_attestation_is_invalid() -> None:
    assert compute_validity(None, {"status": "closed"}, "completion-verifier")["valid"] is False
