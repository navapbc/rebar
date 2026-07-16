"""Unit tests for the release-source authorization guards (story fd84, Finding 1).

`scripts/release_guards.py` extracts the three release-time guards into small, testable
units so their NEGATIVE paths are codebase-verifiable without a live publish:

  * version-lockstep — the dispatch `version` input must equal `pyproject.toml`'s
    `[project].version`, `server.json`'s top-level `version`, and every
    `server.json.packages[].version`.
  * ancestry — the dispatched HEAD must be reachable from `origin/main`
    (`git merge-base --is-ancestor`), accepting a non-tip ancestor and rejecting a
    sibling commit.
  * env-preflight — the `pypi` GitHub environment must have required reviewers AND a
    `main`-only deployment-branch policy; a cleared protection fails the release closed.

These exercise the pure helpers directly (plus a real temp git repo for ancestry) — no
GitHub Actions run needed. The structural wiring of these guards into `release.yml` is
covered by `tests/unit/test_release_workflow_pins.py`.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

import rebar

ROOT = Path(rebar.__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "release_guards.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_guards", _SCRIPT)
    assert spec and spec.loader, f"cannot load {_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rg = _load()


# ── version-lockstep ──────────────────────────────────────────────────────────
_PYPROJECT = '[project]\nname = "nava-rebar"\nversion = "{v}"\n'


def _server(top: str, pkgs: list[str]) -> dict:
    return {"version": top, "packages": [{"version": p} for p in pkgs]}


def test_version_lockstep_happy_all_equal() -> None:
    fails = rg.check_version_lockstep(
        "0.7.2", _PYPROJECT.format(v="0.7.2"), _server("0.7.2", ["0.7.2"])
    )
    assert fails == [], f"expected lockstep to pass, got {fails}"


# --- HELD OUT (edge cases below) ---
def test_version_lockstep_pyproject_mismatch_fails() -> None:
    fails = rg.check_version_lockstep(
        "0.7.2", _PYPROJECT.format(v="0.7.1"), _server("0.7.2", ["0.7.2"])
    )
    assert fails, "a pyproject version != input must fail lockstep"
    assert any("pyproject" in f.lower() for f in fails)


def test_version_lockstep_server_top_mismatch_fails() -> None:
    fails = rg.check_version_lockstep(
        "0.7.2", _PYPROJECT.format(v="0.7.2"), _server("0.7.1", ["0.7.2"])
    )
    assert fails, "a server.json top-level version != input must fail lockstep"


def test_version_lockstep_package_mismatch_fails() -> None:
    fails = rg.check_version_lockstep(
        "0.7.2", _PYPROJECT.format(v="0.7.2"), _server("0.7.2", ["0.7.2", "0.7.1"])
    )
    assert fails, "any packages[].version != input must fail lockstep"


def test_version_lockstep_input_differs_from_all_fails() -> None:
    fails = rg.check_version_lockstep(
        "9.9.9", _PYPROJECT.format(v="0.7.2"), _server("0.7.2", ["0.7.2"])
    )
    assert fails, "an input version matching nothing must fail lockstep"


def test_version_lockstep_missing_pyproject_version_fails() -> None:
    fails = rg.check_version_lockstep(
        "0.7.2", '[project]\nname = "nava-rebar"\n', _server("0.7.2", ["0.7.2"])
    )
    assert fails, "an absent pyproject version must fail (not silently pass)"


def test_version_lockstep_no_packages_fails() -> None:
    # A server.json with no packages array is malformed for lockstep — must not pass blindly.
    fails = rg.check_version_lockstep(
        "0.7.2", _PYPROJECT.format(v="0.7.2"), {"version": "0.7.2", "packages": []}
    )
    assert fails, "server.json with zero packages must fail lockstep"


# ── ancestry ──────────────────────────────────────────────────────────────────
def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("a")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "A")
    sha_a = _git(r, "rev-parse", "HEAD")
    (r / "f").write_text("b")
    _git(r, "commit", "-qam", "B")
    sha_b = _git(r, "rev-parse", "HEAD")
    (r / "f").write_text("c")
    _git(r, "commit", "-qam", "C")
    sha_c = _git(r, "rev-parse", "HEAD")
    # A sibling commit forked off A, never merged into main.
    _git(r, "checkout", "-q", "-b", "side", sha_a)
    (r / "g").write_text("d")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "D")
    sha_d = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "main")
    return {"dir": r, "A": sha_a, "B": sha_b, "C": sha_c, "D": sha_d}


def test_ancestry_accepts_tip(repo) -> None:
    assert rg.is_ancestor(repo["C"], "main", cwd=repo["dir"]) is True


# --- HELD OUT (edge cases below) ---
def test_ancestry_accepts_non_tip_ancestor(repo) -> None:
    # A non-tip ancestor (A, B) is still reachable from main — must be accepted.
    assert rg.is_ancestor(repo["A"], "main", cwd=repo["dir"]) is True
    assert rg.is_ancestor(repo["B"], "main", cwd=repo["dir"]) is True


def test_ancestry_rejects_sibling(repo) -> None:
    # D forked off A and was never merged — NOT an ancestor of main.
    assert rg.is_ancestor(repo["D"], "main", cwd=repo["dir"]) is False


# ── env-preflight ─────────────────────────────────────────────────────────────
def _env_ok() -> dict:
    return {
        "protection_rules": [
            {"id": 1, "type": "required_reviewers", "reviewers": [{"type": "User"}]},
            {"id": 2, "type": "branch_policy"},
        ],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    }


def _policies_main() -> dict:
    return {"total_count": 1, "branch_policies": [{"name": "main"}]}


def test_env_preflight_happy() -> None:
    assert rg.check_env_protection(_env_ok(), _policies_main()) == []


# --- HELD OUT (edge cases below) ---
def test_env_preflight_no_required_reviewers_fails() -> None:
    env = _env_ok()
    env["protection_rules"] = [{"id": 2, "type": "branch_policy"}]
    fails = rg.check_env_protection(env, _policies_main())
    assert any("review" in f.lower() for f in fails), fails


def test_env_preflight_empty_reviewers_fails() -> None:
    env = _env_ok()
    env["protection_rules"][0]["reviewers"] = []
    fails = rg.check_env_protection(env, _policies_main())
    assert any("review" in f.lower() for f in fails), fails


def test_env_preflight_branch_policy_null_fails() -> None:
    # A null deployment_branch_policy means ALL branches may deploy — the exact drift
    # the preflight exists to catch.
    env = _env_ok()
    env["deployment_branch_policy"] = None
    fails = rg.check_env_protection(env, None)
    assert any("branch" in f.lower() for f in fails), fails


def test_env_preflight_branch_policy_not_main_only_fails() -> None:
    fails = rg.check_env_protection(_env_ok(), {"branch_policies": [{"name": "dev"}]})
    assert any("main" in f.lower() for f in fails), fails


def test_env_preflight_branch_policy_extra_branch_fails() -> None:
    fails = rg.check_env_protection(
        _env_ok(), {"branch_policies": [{"name": "main"}, {"name": "release/*"}]}
    )
    assert fails, "a policy list beyond exactly {main} must fail"


# ── CLI smoke (exit codes) ────────────────────────────────────────────────────
def test_cli_version_lockstep_exit_codes(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT.format(v="0.7.2"))
    import json

    (tmp_path / "server.json").write_text(json.dumps(_server("0.7.2", ["0.7.2"])))
    ok = rg.main(
        [
            "version-lockstep",
            "--version",
            "0.7.2",
            "--pyproject",
            str(tmp_path / "pyproject.toml"),
            "--server-json",
            str(tmp_path / "server.json"),
        ]
    )
    assert ok == 0
    bad = rg.main(
        [
            "version-lockstep",
            "--version",
            "0.7.3",
            "--pyproject",
            str(tmp_path / "pyproject.toml"),
            "--server-json",
            str(tmp_path / "server.json"),
        ]
    )
    assert bad == 1
