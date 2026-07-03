"""Unit tests for delivered_now — the plan-review completion-awareness predicate (story 457a).

``delivered_now(child, siblings)`` keys on VERIFIED delivery (a valid ``completion-verifier``
attestation on read), never bare ``closed`` status. These exercise both branches directly with
constructed ticket-state dicts, monkeypatching the same two seams ``test_compute_validity``
does (``rebar.verify_signature`` — the per-child attestation read reused from
``completion.child_closure_findings`` — and ``attest.current_material_fingerprint``)."""

from __future__ import annotations

import rebar
from rebar.llm.plan_review import attest
from rebar.llm.plan_review.attest import delivered_now


def _patch_sigs(monkeypatch, sigs: dict[str, dict]) -> None:
    """Route ``rebar.verify_signature(tid, kind=…)`` to a per-ticket fake record. An id absent
    from ``sigs`` returns an ``unsigned`` verdict (no completion attestation → not attested)."""

    def fake(tid, *, kind=None, repo_root=None):
        return sigs.get(tid, {"verdict": "unsigned"})

    monkeypatch.setattr(rebar, "verify_signature", fake)


def _material(monkeypatch, value: str = "m1") -> None:
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda tid, repo_root=None: value)


def _certified(material: str = "m1", signed_at: int = 100) -> dict:
    """A certified completion-verifier signature record (shape compute_validity consumes)."""
    return {
        "verdict": "certified",
        "manifest": ["completion-verifier: PASS", f"material: {material}"],
        "signed_at": signed_at,
    }


def _child(status: str = "closed", *, parent: str = "epic", tid: str = "c1", **extra) -> dict:
    return {"ticket_id": tid, "status": status, "parent_id": parent, **extra}


# ── Branch (A): delivered-and-attested ────────────────────────────────────────────
def test_attested_closed_is_delivered(monkeypatch) -> None:
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {"c1": _certified("m1")})
    assert delivered_now(_child("closed"), []) is True


def test_force_closed_unsigned_is_not_delivered(monkeypatch) -> None:
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {})  # no attestation → verify_signature returns 'unsigned'
    assert delivered_now(_child("closed"), []) is False


def test_drift_stale_attestation_is_not_delivered(monkeypatch) -> None:
    # Certified signed against "m1" but the current material fingerprint is "m2" → compute_validity
    # returns valid=False (stale-material) → not delivered even though closed + certified.
    _material(monkeypatch, "m2")
    _patch_sigs(monkeypatch, {"c1": _certified("m1")})
    assert delivered_now(_child("closed"), []) is False


def test_in_progress_child_is_not_delivered(monkeypatch) -> None:
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {"c1": _certified("m1")})  # attested, but status gates it out
    assert delivered_now(_child("in_progress"), []) is False


def test_open_child_is_not_delivered(monkeypatch) -> None:
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {"c1": _certified("m1")})
    assert delivered_now(_child("open"), []) is False


# ── Reopen semantics: a PARENT reopen must not un-deliver a closed+attested child ──
def test_parent_reopen_keeps_attested_child_delivered(monkeypatch) -> None:
    # The child's OWN state carries no last_reopened_at (only the parent was reopened), so
    # compute_validity — which keys on each ticket's own last_reopened_at — still validates it.
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {"c1": _certified("m1", signed_at=100)})
    child = _child("closed")  # no last_reopened_at ⇒ parent reopen does not reach it
    assert delivered_now(child, []) is True


def test_child_own_reopen_after_signing_undelivers(monkeypatch) -> None:
    # Sanity companion: the child's OWN reopen (after signing) DOES invalidate it (stale-reopened).
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {"c1": _certified("m1", signed_at=100)})
    child = _child("closed", last_reopened_at=200)
    assert delivered_now(child, []) is False


# ── Branch (B): superseded-by-live-in-epic-sibling ────────────────────────────────
def _superseder(status: str, *, parent: str = "epic", tid: str = "a1", target: str = "c1") -> dict:
    """A sibling A carrying an ``A -supersedes-> child`` link (dep stored on the source)."""
    return {
        "ticket_id": tid,
        "status": status,
        "parent_id": parent,
        "deps": [{"relation": "supersedes", "target_id": target, "link_uuid": "u1"}],
    }


def test_superseded_by_live_sibling_is_delivered(monkeypatch) -> None:
    # Child is force-closed (not attested), but a LIVE (in_progress) in-epic sibling supersedes it.
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {})  # neither child nor A is attested
    child = _child("closed")
    a = _superseder("in_progress")
    assert delivered_now(child, [a]) is True


def test_superseded_by_open_sibling_is_delivered(monkeypatch) -> None:
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {})
    child = _child("open")  # not delivered on its own
    a = _superseder("open")
    assert delivered_now(child, [a]) is True


def test_superseded_by_closed_attested_sibling_is_delivered(monkeypatch) -> None:
    # A is closed but carries a VALID completion attestation → branch (A) on A → live vehicle.
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {"a1": _certified("m1")})
    child = _child("open")
    a = _superseder("closed")
    assert delivered_now(child, [a]) is True


def test_superseded_by_non_sibling_is_not_delivered(monkeypatch) -> None:
    # A supersedes child and is live, but sits under a DIFFERENT parent → not an in-epic sibling.
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {})
    child = _child("closed", parent="epic")
    a = _superseder("in_progress", parent="other-epic")
    assert delivered_now(child, [a]) is False


def test_superseded_by_dead_force_closed_sibling_is_not_delivered(monkeypatch) -> None:
    # A supersedes child and is a sibling, but A is force-closed (closed, no valid attestation) —
    # an abandoned vehicle, not a live one → child is not delivered via the supersede branch.
    _material(monkeypatch, "m1")
    _patch_sigs(monkeypatch, {})  # A ('a1') is unsigned → dead
    child = _child("closed")
    a = _superseder("closed")
    assert delivered_now(child, [a]) is False
