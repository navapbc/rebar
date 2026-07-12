"""Happy-path oracle for 3183 (epic gnu-whale-ichor): opt-in authenticated-authorship
enforcement.

The ONLY 3183 test the implementation sees. Pins the merge-gate on the happy path:
with `identity.require_authenticated` ON, `rebar verify-authorship` FLAGS an unsigned
event and exits non-zero (the real control); with the config OFF it is advisory
(exit 0). The write-gate, replay-never-rejects, and snapshot round-trip are held out.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

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
    env = {**os.environ, "REBAR_ROOT": str(store)}
    env["REBAR_IDENTITY_REQUIRE_AUTHENTICATED"] = "1" if required else "0"
    return subprocess.run(
        ["rebar", "verify-authorship", "--all"],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
    )


def test_verify_authorship_flags_unsigned_when_required(store: Path) -> None:
    """With require_authenticated ON, the merge-gate flags an unsigned event and
    fails (non-zero exit)."""
    res = _verify_authorship(store, required=True)
    assert res.returncode != 0, res.stdout + res.stderr
    assert "unsigned" in (res.stdout + res.stderr).lower()


def test_verify_authorship_advisory_when_off(store: Path) -> None:
    """With require_authenticated OFF, verify-authorship is advisory (exit 0)."""
    res = _verify_authorship(store, required=False)
    assert res.returncode == 0, res.stdout + res.stderr
