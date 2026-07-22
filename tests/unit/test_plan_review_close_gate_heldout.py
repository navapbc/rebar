"""Held-out policy and failure contracts for the plan-review close gate."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rebar._commands import gates


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        ("unsigned", "no certified plan-review attestation"),
        ("stale-reopened", "review predates reopen"),
        ("stale-regver", "criteria registry changed"),
        ("stale-material", "ticket material changed"),
        ("unverifiable-material", "material unavailable"),
        ("stale-pin-drift", "prerequisite drifted"),
        ("stale-pin-missing", "prerequisite missing"),
        ("malformed-pin", "pin metadata malformed"),
        ("incompatible-phase", "execution phase mismatch"),
        ("malformed-phase", "phase metadata malformed"),
    ],
)
def test_close_gate_preserves_validity_failure_vocabulary(
    monkeypatch, verdict: str, reason: str
) -> None:
    monkeypatch.setattr(gates, "gate_enabled", lambda *a, **k: True)

    from rebar import signing
    from rebar.llm.plan_review import attest

    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda *a, **k: {"verified": verdict != "unsigned", "verdict": "certified"},
    )
    monkeypatch.setattr(
        attest,
        "compute_validity",
        lambda *a, **k: {"valid": False, "verdict": verdict, "reason": reason},
    )

    result = gates.close_plan_review_gate_check(
        "1111-2222-3333-4444",
        {"ticket_id": "1111-2222-3333-4444", "ticket_type": "task"},
        repo_root="/repo",
    )
    assert result == {"ok": False, "verdict": verdict, "reason": reason}


def test_close_gate_unexpected_error_fails_closed_without_raising(monkeypatch, caplog) -> None:
    monkeypatch.setattr(gates, "gate_enabled", lambda *a, **k: True)

    from rebar import signing

    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("secret parser detail")),
    )

    with caplog.at_level(logging.WARNING):
        result = gates.close_plan_review_gate_check(
            "1111-2222-3333-4444",
            {"ticket_id": "1111-2222-3333-4444", "ticket_type": "epic"},
            repo_root="/repo",
        )

    assert result["ok"] is False
    assert result["verdict"] == "unavailable"
    assert "secret parser detail" not in result["reason"]
    assert any(
        getattr(record, "event", None) == "plan_review_close_gate_unavailable"
        or "plan_review_close_gate_unavailable" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("ticket_type", ["bug", "session_log", "code_review", "identity"])
def test_close_gate_exempt_types_perform_no_signature_read(monkeypatch, ticket_type: str) -> None:
    monkeypatch.setattr(gates, "gate_enabled", lambda *a, **k: True)

    from rebar import signing

    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("signature read")),
    )
    result = gates.close_plan_review_gate_check(
        "1111-2222-3333-4444",
        {"ticket_id": "1111-2222-3333-4444", "ticket_type": ticket_type},
        repo_root="/repo",
    )
    assert result["ok"] is True
    assert result["verdict"] == "exempt"


def test_close_gate_repo_root_none_resolves_enabled_checkout(monkeypatch, tmp_path: Path) -> None:
    """CLI-style callers pass ``None``; config must resolve from cwd, not ``"None"``."""
    from rebar import config, signing
    from rebar.llm.plan_review import attest

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_close = true\n", encoding="utf-8"
    )
    monkeypatch.chdir(repo)
    config.reset_config_cache()
    calls: list[str] = []
    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda *a, **k: calls.append("signature") or {"verified": True, "verdict": "certified"},
    )
    monkeypatch.setattr(
        attest,
        "compute_validity",
        lambda *a, **k: {"valid": True, "verdict": "certified", "reason": "current"},
    )

    result = gates.close_plan_review_gate_check(
        "1111-2222-3333-4444",
        {"ticket_id": "1111-2222-3333-4444", "ticket_type": "story"},
        repo_root=None,
    )

    assert result["ok"] is True
    assert result["verdict"] == "certified"
    assert calls == ["signature"]
