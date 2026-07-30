"""Retyping a ticket must invalidate a reused plan-review verdict (ticket 6e4f).

Type selects which criteria a review applies at all — `orchestrator.py:364`/`:642`
and `workflow_ops.py:167`/`:183` all branch on it — so a PASS earned as a `bug`,
which is exempt from several criteria, must not be replayed for a `task`, which is
not. Both reuse paths gated only on the material fingerprint, which does not move
when the type changes.

Kept apart from `test_plan_review_reuse.py`: those fixtures are tuned to the
DET-floor short-circuit and were red on main within the last day (change 1044).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rebar.llm.plan_review import reuse


def _ctx(ticket_type: str) -> SimpleNamespace:
    return SimpleNamespace(ticket_id="t-1", ticket_type=ticket_type)


def _block_sidecar(ticket_type: Any) -> dict[str, Any]:
    """A stored BLOCK verdict whose every OTHER precondition for reuse holds."""
    payload: dict[str, Any] = {
        "verdict": "BLOCK",
        "material_fingerprint": "fp-unchanged",
        "verified_at_sha": "sha-unchanged",
        "findings": [{"decision": "block", "finding": "x", "criteria": ["E2"]}],
        "coaching": [],
    }
    if ticket_type is not None:
        payload["ticket_type"] = ticket_type
    return payload


@pytest.fixture
def _block_reuse_ready(monkeypatch: pytest.MonkeyPatch):
    """Pin every non-type precondition of verdict_reuse to 'unchanged'."""
    monkeypatch.setattr(
        reuse.attest, "current_material_fingerprint", lambda *_a, **_k: "fp-unchanged"
    )
    monkeypatch.setattr(reuse.sidecar, "review_code_sha", lambda *_a, **_k: "sha-unchanged")


# ---------------------------------------------------------------------------
# the predicate itself
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_type_changed_only_when_both_sides_are_known() -> None:
    """The predicate fails OPEN: a missing side is never reported as a change."""
    assert reuse._type_changed("bug", _ctx("task")) is True
    assert reuse._type_changed("task", _ctx("task")) is False
    # fail-open cases — the operator chose this over invalidating every attestation
    assert reuse._type_changed(None, _ctx("task")) is False
    assert reuse._type_changed("", _ctx("task")) is False
    assert reuse._type_changed("bug", _ctx("")) is False


# ---------------------------------------------------------------------------
# verdict_reuse (the BLOCK path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_verdict_reuse_declines_when_the_type_changed(
    monkeypatch: pytest.MonkeyPatch, _block_reuse_ready
) -> None:
    """A BLOCK stored for a `bug` is not replayed once the ticket is a `task`."""
    monkeypatch.setattr(
        reuse.sidecar, "latest_review_result", lambda *_a, **_k: _block_sidecar("bug")
    )
    assert reuse.verdict_reuse("t-1", _ctx("task"), repo_root=None) is None


@pytest.mark.unit
def test_verdict_reuse_still_reuses_when_the_type_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, _block_reuse_ready
) -> None:
    """The guard must not cost a legitimate reuse — no spurious re-reviews."""
    monkeypatch.setattr(
        reuse.sidecar, "latest_review_result", lambda *_a, **_k: _block_sidecar("task")
    )
    out = reuse.verdict_reuse("t-1", _ctx("task"), repo_root=None)
    assert out is not None and out["verdict"] == "BLOCK", out


@pytest.mark.unit
@pytest.mark.parametrize("stored", [None, ""], ids=["absent", "empty"])
def test_verdict_reuse_fails_open_on_a_sidecar_without_a_type(
    monkeypatch: pytest.MonkeyPatch, _block_reuse_ready, stored
) -> None:
    """A sidecar predating this check keeps reusing — the whole point of fail-open.

    Invalidating these would force a fresh review of every already-certified ticket,
    which is exactly the mass invalidation the operator declined.
    """
    monkeypatch.setattr(
        reuse.sidecar, "latest_review_result", lambda *_a, **_k: _block_sidecar(stored)
    )
    out = reuse.verdict_reuse("t-1", _ctx("task"), repo_root=None)
    assert out is not None and out["verdict"] == "BLOCK", out


# ---------------------------------------------------------------------------
# idempotent_reuse (the PASS path)
# ---------------------------------------------------------------------------


@pytest.fixture
def _pass_reuse_ready(monkeypatch: pytest.MonkeyPatch):
    """Pin every non-type precondition of idempotent_reuse to 'reuse is safe'."""
    monkeypatch.setattr(reuse, "claim_gate_check", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        reuse.attest, "current_material_fingerprint", lambda *_a, **_k: "fp-unchanged"
    )
    import rebar.signing as _signing

    monkeypatch.setattr(
        _signing, "verify_signature", lambda *_a, **_k: {"key_id": "k", "head_sha": "h"}
    )


@pytest.mark.unit
def test_idempotent_reuse_declines_when_the_type_changed(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    """A PASS earned under the `bug` exemptions is not replayed for a `task`."""
    monkeypatch.setattr(
        reuse.sidecar, "latest_review_result", lambda *_a, **_k: {"ticket_type": "bug"}
    )
    assert reuse.idempotent_reuse("t-1", _ctx("task"), repo_root=None) is None


@pytest.mark.unit
def test_idempotent_reuse_still_reuses_when_the_type_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    monkeypatch.setattr(
        reuse.sidecar, "latest_review_result", lambda *_a, **_k: {"ticket_type": "task"}
    )
    out = reuse.idempotent_reuse("t-1", _ctx("task"), repo_root=None)
    assert out is not None and out["verdict"] == "PASS", out


@pytest.mark.unit
def test_idempotent_reuse_fails_open_when_the_sidecar_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    """A sidecar read error must not cost a valid reuse — fail open, not closed."""

    def _boom(*_a, **_k):
        raise OSError("sidecar unreadable")

    monkeypatch.setattr(reuse.sidecar, "latest_review_result", _boom)
    out = reuse.idempotent_reuse("t-1", _ctx("task"), repo_root=None)
    assert out is not None and out["verdict"] == "PASS", out
