"""Bot-alert recovery closes use an attested disposition, never force."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import close_precheck, composer, gates
from rebar._commands._seam import CommandError

pytestmark = pytest.mark.unit

_ALLOWED_BOT_ALERTS = (
    "dependency-advisory-canary",
    "heartbeat-canary",
    "binding-drift-canary",
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@example.test"),
        ("git", "config", "user.name", "Tester"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=root, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(root))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    monkeypatch.chdir(root)
    rebar.init_repo(repo_root=str(root))
    return root


def _bug(repo: Path, *, detected_by: str | None = None) -> str:
    created = composer.create_core(
        "bug",
        "bot alert",
        repo_root=str(repo),
        creation_channel="cli",
        detected_by=detected_by,
    )
    return str(created["id"])


def _arm_completion_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gates,
        "gate_enabled",
        lambda _root, name, **_k: name == "require_completion_verification_for_close",
    )
    monkeypatch.setattr(
        close_precheck,
        "_verify_with_duration_metrics",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("completion verifier ran")),
    )


@pytest.mark.parametrize("detected_by", _ALLOWED_BOT_ALERTS)
def test_env_integration_disposition_closes_supported_bot_alerts_without_verifier(
    repo: Path, monkeypatch: pytest.MonkeyPatch, detected_by: str
) -> None:
    _arm_completion_gate(monkeypatch)
    tid = _bug(repo, detected_by=detected_by)

    result, expectation = close_precheck._completion_precheck(
        tid,
        "bug",
        str(repo),
        str(repo),
        reason="Fixed: observed recovery.",
        force_close="",
        close_class="env_integration",
    )

    assert expectation == "disposition"
    assert result is not None
    assert result["disposition"] == "env_integration"
    assert result["detected_by"] == detected_by
    assert result["close_reason"] == "Fixed: observed recovery."


@pytest.mark.parametrize("detected_by", [None, "ci", "fuzzer", "human-audit"])
def test_env_integration_disposition_refuses_non_bot_alerts(
    repo: Path, monkeypatch: pytest.MonkeyPatch, detected_by: str | None
) -> None:
    _arm_completion_gate(monkeypatch)
    tid = _bug(repo, detected_by=detected_by)

    with pytest.raises(CommandError) as exc:
        close_precheck._completion_precheck(
            tid,
            "bug",
            str(repo),
            str(repo),
            reason="Fixed: observed recovery.",
            force_close="",
            close_class="env_integration",
        )

    assert "requires bot-alert detected_by provenance" in exc.value.message
