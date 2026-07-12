"""Happy-path oracle for AC7 (bff8, epic gnu-whale-ichor): the `rebar verify-identity`
enforcement merge-gate.

The ONLY AC7 test the implementation sees: `rebar verify-identity --all` runs the gate, and
`--require-authenticated` forces enforcement (non-zero exit on an unsigned event) even when
config leaves it off. The alias, `--since` grandfathering, `--format json` report, and the
CI workflow file are held out.
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
    rebar.create_ticket("task", "an unsigned task", repo_root=str(repo))
    return repo


def _run(store: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "REBAR_ROOT": str(store)}
    # config leaves require_authenticated OFF; the flag must force it on.
    env["REBAR_IDENTITY_REQUIRE_AUTHENTICATED"] = "0"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["rebar", *args], cwd=store, env=env, capture_output=True, text=True)


def test_verify_identity_dispatches(store: Path) -> None:
    """`rebar verify-identity --all` runs the gate; advisory (exit 0) with enforcement off."""
    res = _run(store, "verify-identity", "--all")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "verif" in (res.stdout + res.stderr).lower()


def test_require_authenticated_flag_forces_enforcement(store: Path) -> None:
    """`--require-authenticated` forces the gate on even when config leaves it off:
    an unsigned in-scope event then fails the gate (non-zero exit)."""
    res = _run(store, "verify-identity", "--all", "--require-authenticated")
    assert res.returncode != 0, res.stdout + res.stderr
    assert "unsigned" in (res.stdout + res.stderr).lower()
