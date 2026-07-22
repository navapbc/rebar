"""Plan validity owns progressive-refresh eligibility."""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import attest


def test_candidate_delegates_full_ticket_state_to_drift_refresh_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = getattr(attest, "PlanValidityProfile", None)
    assert profile is not None, "PlanValidityProfile API is absent"
    signature = {
        "verified": True,
        "manifest": [
            "plan-review: PASS",
            "regver: registry",
            "dep old-digest src/x.py",
        ],
        "key_id": "key",
    }
    state = {
        "ticket_id": "1111-2222-3333-4444",
        "status": "open",
        "last_reopened_at": 123,
    }
    monkeypatch.setattr("rebar.signing.verify_signature", lambda *a, **k: signature)
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *a, **k: state)
    seen = {}

    def valid(attestation, ticket_state, kind, *, repo_root=None, profile=None):
        seen.update(
            attestation=attestation,
            ticket_state=ticket_state,
            kind=kind,
            repo_root=repo_root,
            profile=profile,
        )
        return {"valid": True, "reason": "ok", "verdict": "certified"}

    monkeypatch.setattr(attest, "compute_validity", valid)
    monkeypatch.setattr(attest, "_rehash", lambda *a, **k: {"src/x.py": "new-digest"})
    candidate = attest.drift_refresh_candidate("1111-2222-3333-4444", repo_root="/repo")
    assert candidate == {
        "manifest": signature["manifest"],
        "deps": {"src/x.py": "old-digest"},
        "key_id": "key",
    }
    assert seen == {
        "attestation": signature,
        "ticket_state": state,
        "kind": "plan-review",
        "repo_root": "/repo",
        "profile": profile.DRIFT_REFRESH,
    }


def test_reopened_or_pin_invalid_attestation_is_not_refreshable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = getattr(attest, "PlanValidityProfile", None)
    assert profile is not None
    monkeypatch.setattr(
        "rebar.signing.verify_signature",
        lambda *a, **k: {
            "verified": True,
            "manifest": ["plan-review: PASS", "dep old src/x.py"],
        },
    )
    monkeypatch.setattr(
        "rebar._reads.show_ticket",
        lambda *a, **k: {"ticket_id": "1111-2222-3333-4444", "last_reopened_at": 5},
    )
    monkeypatch.setattr(
        attest,
        "compute_validity",
        lambda *a, **k: {"valid": False, "reason": "stale", "verdict": "stale-reopened"},
    )
    monkeypatch.setattr(
        attest,
        "_rehash",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probe must not run")),
    )
    assert attest.drift_refresh_candidate("1111-2222-3333-4444", repo_root="/repo") is None
