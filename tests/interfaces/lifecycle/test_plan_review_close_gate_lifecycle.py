"""Held-out lifecycle wiring for the plan-review close gate."""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar._commands import gates, transition_close


def _make(repo: Path, ticket_type: str = "task") -> str:
    tid = rebar.create_ticket(ticket_type, "close plan gate", repo_root=str(repo))
    rebar.claim(tid, assignee="me", repo_root=str(repo))
    return tid


def test_plan_gate_failure_precedes_completion_and_leaves_status_unchanged(
    rebar_repo: Path, monkeypatch
) -> None:
    tid = _make(rebar_repo)
    monkeypatch.setattr(
        gates,
        "close_plan_review_gate_check",
        lambda *a, **k: {
            "ok": False,
            "verdict": "stale-pin-drift",
            "reason": "canonical prerequisite changed",
        },
    )
    monkeypatch.setattr(
        transition_close,
        "_completion_precheck",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("completion ran first")),
    )

    with pytest.raises(rebar.RebarError) as exc:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    assert exc.value.returncode == 1
    assert "plan-review close gate: stale-pin-drift" in exc.value.stderr
    assert "review-plan" in exc.value.stderr
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "in_progress"


def test_locked_recheck_detects_change_after_precheck(rebar_repo: Path, monkeypatch) -> None:
    tid = _make(rebar_repo)
    checks = 0

    def changes_between_checks(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal checks
        checks += 1
        if checks == 1:
            return {"ok": True, "verdict": "certified", "reason": "current"}
        return {"ok": False, "verdict": "stale-material", "reason": "plan changed"}

    monkeypatch.setattr(gates, "close_plan_review_gate_check", changes_between_checks)
    monkeypatch.setattr(transition_close, "_completion_precheck", lambda *a, **k: None)

    with pytest.raises(rebar.RebarError) as exc:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    assert checks == 2
    assert "stale-material" in exc.value.stderr
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "in_progress"


def test_force_close_bypasses_plan_gate_but_not_structural_children(
    rebar_repo: Path, monkeypatch
) -> None:
    parent = _make(rebar_repo, "story")
    child = rebar.create_ticket("task", "open child", parent=parent, repo_root=str(rebar_repo))
    monkeypatch.setattr(
        gates,
        "close_plan_review_gate_check",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("plan gate ran")),
    )

    with pytest.raises(rebar.RebarError, match="child"):
        rebar.transition(
            parent,
            "in_progress",
            "closed",
            force_close="approved",
            repo_root=str(rebar_repo),
        )

    assert rebar.show_ticket(parent, repo_root=str(rebar_repo))["status"] == "in_progress"
    assert rebar.show_ticket(child, repo_root=str(rebar_repo))["status"] == "open"


def test_force_close_bypasses_plan_gate(rebar_repo: Path, monkeypatch) -> None:
    tid = _make(rebar_repo)
    monkeypatch.setattr(
        gates,
        "close_plan_review_gate_check",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("plan gate ran")),
    )
    rebar.transition(
        tid,
        "in_progress",
        "closed",
        force_close="approved",
        repo_root=str(rebar_repo),
    )

    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "closed"


def test_idea_rejection_bypasses_plan_gate(rebar_repo: Path, monkeypatch) -> None:
    tid = rebar.idea("reject idea", repo_root=str(rebar_repo))
    monkeypatch.setattr(
        gates,
        "close_plan_review_gate_check",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("plan gate ran")),
    )

    rebar.transition(tid, "idea", "closed", repo_root=str(rebar_repo))

    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "closed"


def test_close_gate_uses_only_local_ticket_reads(rebar_repo: Path, monkeypatch) -> None:
    """Close validity must not run the read facade's fetch/reconverge policy."""
    from rebar import signing
    from rebar._engine_support import reads as ticket_reads
    from rebar.llm.plan_review import attest

    (rebar_repo / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_close = true\n", encoding="utf-8"
    )
    tid = _make(rebar_repo)
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    material = attest.current_material_fingerprint(tid, repo_root=str(rebar_repo))
    assert material is not None
    manifest = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": tid, "coverage": {"counts": {}}},
        material=material,
        regver=attest.registry_version(str(rebar_repo)),
        review_phase="planning",
    )
    verified = {
        "verified": True,
        "verdict": "certified",
        "manifest": manifest,
        "signed_at": 2,
        "head_sha": "irrelevant-for-close",
    }

    monkeypatch.setattr(
        ticket_reads,
        "_sync_disabled",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network-capable freshness read")),
    )

    unsigned = gates.close_plan_review_gate_check(tid, state, repo_root=str(rebar_repo))
    assert unsigned["verdict"] == "unsigned"

    monkeypatch.setattr(signing, "verify_signature", lambda *a, **k: verified)
    result = gates.close_plan_review_gate_check(tid, state, repo_root=str(rebar_repo))

    assert {key: result[key] for key in ("ok", "verdict", "reason")} == {
        "ok": True,
        "verdict": "certified",
        "reason": "certified plan-review attestation",
    }
    health = result["health"]
    assert health["pin_status"] == "current-no-relationships"
    assert health["related_material_status"] == "no-related-material"
    assert health["targets"] == []
    assert health["enforced"] is False
    assert health["enforcement_status"] == "disabled"
    assert health["advisory"] is False
    assert health["signed_phase"] == "planning"
    assert health["required_phase"] == "execution"
    assert health["phase_status"] == "compatible"
    assert health["effective_execution_floor"] is None
