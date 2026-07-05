"""Unit tests for _advisory_lock.py (the self-healing refs/reconciler/* backend).

The legacy tickets-branch file backend, its b859 retry loop, and the file-lock
tests were retired in epic dust-troth-naval / C4 (ADR 0031). What remains here is
the ref-backend coverage: check_pass_lock, acquire/release, the named
double-acquire convergence test, and the phase gate — all over refs/reconciler/*.

Module loading follows the importlib.util.spec_from_file_location convention.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
LOCK_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_advisory_lock.py"
MODE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mode.py"


def _load_mode_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rebar_reconciler_mode", MODE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler_mode"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_advisory_lock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rebar_reconciler_advisory_lock", LOCK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler_advisory_lock"] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop("rebar_reconciler_advisory_lock", None)
        raise
    return mod


@pytest.fixture(scope="module")
def advisory_lock() -> ModuleType:
    if not LOCK_PATH.exists():
        pytest.fail(f"_advisory_lock.py not found at {LOCK_PATH}")
    return _load_advisory_lock_module()


@pytest.fixture(scope="module")
def mode_mod() -> ModuleType:
    return _load_mode_module()


def _git(args: list[str], repo: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo)] + args, capture_output=True, text=True, check=True, **kwargs
    )


@pytest.fixture()
def tmp_git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    _git(["commit", "--allow-empty", "-m", "init"], tmp_path)
    return tmp_path


@pytest.fixture()
def _ref_backend(advisory_lock: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force lock_backend=ref, a local (no-remote) CAS, and a short lease."""
    monkeypatch.setattr(advisory_lock, "_lock_backend", lambda: "ref")
    monkeypatch.setattr(advisory_lock, "_lock_lease_secs", lambda: 1)
    monkeypatch.setattr(advisory_lock, "_lock_remote", lambda repo_root: None)


# ---------------------------------------------------------------------------
# check_pass_lock
# ---------------------------------------------------------------------------


def test_check_pass_lock_absent(
    advisory_lock: ModuleType, tmp_git_repo: Path, _ref_backend: None
) -> None:
    """check_pass_lock returns False when refs/reconciler/lock is absent (free)."""
    assert advisory_lock.check_pass_lock(tmp_git_repo) is False


def test_check_pass_lock_present_after_acquire(
    advisory_lock: ModuleType, tmp_git_repo: Path, _ref_backend: None
) -> None:
    """After acquire, check_pass_lock reports the lock as held."""
    advisory_lock.acquire_pass_lock("pass-1", tmp_git_repo)
    assert advisory_lock.check_pass_lock(tmp_git_repo) is True


# ---------------------------------------------------------------------------
# acquire / release
# ---------------------------------------------------------------------------


def test_ref_backend_acquire_release_roundtrip(
    advisory_lock: ModuleType, tmp_git_repo: Path, _ref_backend: None
) -> None:
    """acquire returns an oid on refs/reconciler/lock and writes NO
    .reconciler-pass-lock to the tickets branch; release tears the ref down."""
    oid = advisory_lock.acquire_pass_lock("pass-1", tmp_git_repo)
    assert oid, "ref-backend acquire must return the ref oid"
    ref = advisory_lock._load_ref_lock().LOCK_REF
    assert _git(["rev-parse", ref], tmp_git_repo).stdout.strip() == oid
    show = subprocess.run(
        ["git", "-C", str(tmp_git_repo), "cat-file", "-e", "tickets:.reconciler-pass-lock"],
        capture_output=True,
    )
    assert show.returncode != 0, "ref backend must not write a tickets-branch lock file"
    advisory_lock.release_pass_lock("pass-1", tmp_git_repo, oid=oid)
    gone = subprocess.run(
        ["git", "-C", str(tmp_git_repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
    )
    assert gone.returncode != 0, "release must delete the ref"


def test_double_acquire_convergence_single_winner(
    advisory_lock: ModuleType, tmp_git_repo: Path, _ref_backend: None
) -> None:
    """Named double-acquire convergence test: a second acquire against a held ref
    converges to exactly one winner (the loser raises ReconcileLockError)."""
    advisory_lock.acquire_pass_lock("pass-1", tmp_git_repo)
    with pytest.raises(advisory_lock.ReconcileLockError):
        advisory_lock.acquire_pass_lock("pass-2", tmp_git_repo)


# ---------------------------------------------------------------------------
# phase gate
# ---------------------------------------------------------------------------


def test_ref_backend_phase_gate(
    advisory_lock: ModuleType, mode_mod: ModuleType, tmp_git_repo: Path, _ref_backend: None
) -> None:
    """check_phase_gate reads the refs/reconciler/gate blob and blocks a higher mode."""
    ref_lock = advisory_lock._load_ref_lock()
    assert advisory_lock.check_phase_gate(mode_mod.Mode.BOOTSTRAP_THROTTLE, tmp_git_repo) is False
    ref_lock.set_gate(tmp_git_repo, mode_mod.Mode.BOOTSTRAP_STRICT.value)
    assert advisory_lock.check_phase_gate(mode_mod.Mode.BOOTSTRAP_THROTTLE, tmp_git_repo) is True
    assert advisory_lock.check_phase_gate(mode_mod.Mode.BOOTSTRAP_STRICT, tmp_git_repo) is False
