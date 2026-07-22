"""Happy-path contract for the local plan-review close gate."""

from __future__ import annotations

from rebar._commands import gates
from rebar.llm.plan_review.pin_health import PlanValidityProfile


def test_close_gate_uses_certified_plan_review_with_close_profile(monkeypatch) -> None:
    ticket_state = {
        "ticket_id": "1111-2222-3333-4444",
        "ticket_type": "story",
        "status": "in_progress",
    }
    verified = {
        "verified": True,
        "verdict": "certified",
        "manifest": ["plan-review: PASS"],
    }
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(gates, "gate_enabled", lambda *a, **k: True)

    from rebar import signing
    from rebar.llm.plan_review import attest

    def fake_verify(ticket_id, *, kind, repo_root=None):  # type: ignore[no-untyped-def]
        calls.append(("verify", ticket_id, kind, repo_root))
        return verified

    def fake_validity(attestation, state, kind, *, repo_root=None, profile=None):  # type: ignore[no-untyped-def]
        calls.append(("validity", attestation, state, kind, repo_root, profile))
        return {"valid": True, "verdict": "certified", "reason": "current"}

    monkeypatch.setattr(signing, "verify_signature", fake_verify)
    monkeypatch.setattr(attest, "compute_validity", fake_validity)

    result = gates.close_plan_review_gate_check(
        ticket_state["ticket_id"], ticket_state, repo_root="/repo"
    )

    assert result == {"ok": True, "verdict": "certified", "reason": "current"}
    assert calls == [
        ("verify", ticket_state["ticket_id"], "plan-review", "/repo"),
        (
            "validity",
            verified,
            ticket_state,
            "plan-review",
            "/repo",
            PlanValidityProfile.CLOSE,
        ),
    ]


def test_close_gate_disabled_performs_no_plan_review_reads(monkeypatch) -> None:
    monkeypatch.setattr(gates, "gate_enabled", lambda *a, **k: False)

    from rebar import signing

    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("signature read")),
    )

    result = gates.close_plan_review_gate_check(
        "1111-2222-3333-4444",
        {"ticket_id": "1111-2222-3333-4444", "ticket_type": "story"},
        repo_root="/repo",
    )

    assert result["ok"] is True
    assert result["verdict"] == "disabled"
