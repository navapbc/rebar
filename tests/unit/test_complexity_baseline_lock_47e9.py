"""Semantic shrink-only LOCK for ``.github/complexity-baseline.json`` (task 47e9).

The existing baseline gate (``--check``) freezes per-symbol C901 ceilings but does NOT
stop a contributor from ADDING a new high-complexity entry or RAISING an existing ceiling
in the same change — the baseline is a freely-growable file with no CI lock, unlike
``.github/module-size-limit.txt`` which is byte-locked against ``main``.

This task adds a SEMANTIC lock that parses both the ``main`` baseline (base) and the
branch copy and compares the ``ceilings`` mappings entry by entry:

  * an entry REMOVED           -> allowed (the ratchet working)
  * an existing ceiling LOWERED -> allowed
  * an existing ceiling RAISED  -> REJECTED (administrator override required)
  * a NEW entry ADDED           -> REJECTED (the hole today)

The lock must FAIL CLOSED when the base copy cannot be established, and the module-size
lock in ``_build-and-test.yml`` must be fixed to fail closed for the same reason.

These are behavioral/contract assertions against the script's public functions and its
CLI, plus a config-contract assertion on the workflow's fail path.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_complexity_baseline.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "_build-and-test.yml"


def _load():
    spec = importlib.util.spec_from_file_location("check_complexity_baseline", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ccb = _load()


def _doc(ceilings: dict[str, int]) -> str:
    return json.dumps(
        {"schema_version": 1, "ceilings": dict(sorted(ceilings.items()))},
        indent=2,
    )


# ───────────────────────── happy path (given to implementer) ─────────────────


def test_lock_allows_shrinkage() -> None:
    """A REMOVED entry and a LOWERED ceiling produce no violations (ratchet works)."""
    base = {"src/rebar/a.py::f": 20, "src/rebar/b.py::g": 30}
    branch = {"src/rebar/b.py::g": 25}  # a removed, g lowered
    assert ccb.lock_against_base(base, branch) == []


# ───────────────────────── held-out edge cases ──────────────────────────────


def test_lock_rejects_raised_ceiling() -> None:
    """RAISING an existing ceiling is a violation."""
    base = {"src/rebar/a.py::f": 20}
    branch = {"src/rebar/a.py::f": 21}
    violations = ccb.lock_against_base(base, branch)
    assert violations
    assert any("src/rebar/a.py::f" in v for v in violations)


def test_lock_rejects_added_entry() -> None:
    """ADDING a new entry is a violation (the hole today: a new complex function admitted
    by adding a matching baseline entry in the same change)."""
    base = {"src/rebar/a.py::f": 20}
    branch = {"src/rebar/a.py::f": 20, "src/rebar/new.py::big": 30}
    violations = ccb.lock_against_base(base, branch)
    assert violations
    assert any("src/rebar/new.py::big" in v for v in violations)


def test_lock_identical_passes() -> None:
    """No change to the baseline is not a violation."""
    base = {"src/rebar/a.py::f": 20}
    assert ccb.lock_against_base(base, dict(base)) == []


# ───────────────────────── held-out CLI / fail-closed E2E ────────────────────


def _run_lock(base_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--lock", "--base", str(base_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_lock_fails_closed_on_missing_base(tmp_path: Path) -> None:
    """When the base copy cannot be read, the lock exits NONZERO (fail closed) —
    never a warning-and-continue."""
    missing = tmp_path / "does-not-exist.json"
    proc = _run_lock(missing)
    assert proc.returncode != 0


def test_cli_lock_rejects_raise_against_base(tmp_path: Path) -> None:
    """End-to-end: with the committed baseline as branch, a base that is identical
    except one LOWER ceiling means the branch RAISES it -> exit 1."""
    branch = ccb.load_baseline(ccb.BASELINE_PATH)
    assert branch, "committed baseline should be non-empty"
    lowered = dict(branch)
    a_key = sorted(lowered)[0]
    lowered[a_key] = lowered[a_key] - 1  # base is lower => branch raises => reject
    base_path = tmp_path / "base.json"
    base_path.write_text(_doc(lowered), encoding="utf-8")
    proc = _run_lock(base_path)
    assert proc.returncode != 0
    assert a_key in (proc.stdout + proc.stderr)


def test_cli_lock_allows_pure_shrink_against_base(tmp_path: Path) -> None:
    """End-to-end: a base that has an EXTRA entry the branch dropped, and a HIGHER
    ceiling the branch lowered, is pure shrinkage -> exit 0."""
    branch = ccb.load_baseline(ccb.BASELINE_PATH)
    base = dict(branch)
    a_key = sorted(base)[0]
    base[a_key] = base[a_key] + 5  # branch lowered it
    base["src/rebar/zzz_removed.py::gone"] = 99  # branch removed it
    base_path = tmp_path / "base.json"
    base_path.write_text(_doc(base), encoding="utf-8")
    proc = _run_lock(base_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ───────────────────────── module-size lock fail-closed (config contract) ────


def test_module_size_lock_fails_closed_on_fetch_failure() -> None:
    """The module-size gate must NOT warn-and-continue when it cannot fetch main to
    verify the limit lock; it must fail closed. Assert the workflow no longer contains
    the warn-and-continue text for that fetch path."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "could not fetch main to verify the module-size limit lock" not in text or (
        "::warning::could not fetch main to verify the module-size limit lock" not in text
    )
    # The offending warn-and-continue string must be gone entirely.
    assert "::warning::could not fetch main to verify the module-size limit lock" not in text


def test_cli_lock_fails_closed_without_base_when_repo_unknown() -> None:
    """The CI path (no --base) FAILS CLOSED when GITHUB_REPOSITORY is unset — it cannot
    establish the base copy, so it must exit nonzero rather than pass. No network is
    touched because the missing env is detected before any fetch."""
    import os as _os

    env = dict(_os.environ)
    env.pop("GITHUB_REPOSITORY", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--lock"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode != 0
    assert "GITHUB_REPOSITORY" in (proc.stdout + proc.stderr)
