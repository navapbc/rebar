"""Happy-path authenticated-authorship gate tests.

With enforcement on, verify-identity flags unsigned events; with it off,
the gate is advisory. Write-gate, replay, and snapshot cases are held out."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    # a normal (unsigned) ticket exists in the store
    rebar.create_ticket("task", "unsigned work", repo_root=str(repo))
    return repo


def _verify_authorship(store: Path, *, required: bool) -> subprocess.CompletedProcess:
    env = subprocess_env({"REBAR_ROOT": str(store)})
    env["REBAR_IDENTITY_REQUIRE_AUTHENTICATED"] = "1" if required else "0"
    return subprocess.run(
        ["rebar", "verify-identity", "--all"],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_verify_authorship_flags_unsigned_when_required(store: Path) -> None:
    """With require_authenticated ON, the merge-gate flags an unsigned event and
    fails (non-zero exit)."""
    res = _verify_authorship(store, required=True)
    assert res.returncode != 0, res.stdout + res.stderr
    assert "unsigned" in (res.stdout + res.stderr).lower()


def test_verify_authorship_advisory_when_off(store: Path) -> None:
    """With require_authenticated OFF, verify-identity is advisory (exit 0)."""
    res = _verify_authorship(store, required=False)
    assert res.returncode == 0, res.stdout + res.stderr
