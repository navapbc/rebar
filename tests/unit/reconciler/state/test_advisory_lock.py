"""Unit tests for _advisory_lock.py (the self-healing refs/reconciler/* backend).

The legacy tickets-branch file backend, its b859 retry loop, and the file-lock
tests were retired in epic dust-troth-naval / C4 (ADR 0031). What remains here is
the ref-backend coverage: check_pass_lock, acquire/release, the named
double-acquire convergence test, and the phase gate — all over refs/reconciler/*.

Module loading follows the importlib.util.spec_from_file_location convention.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
LOCK_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "_advisory_lock.py"
MODE_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "mode.py"


def _load_mode_module() -> ModuleType:
    return importlib.import_module("rebar.reconciler.mode")


def _load_advisory_lock_module() -> ModuleType:
    return importlib.import_module("rebar.reconciler._advisory_lock")


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
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **kwargs
    )


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    _git(["commit", "--allow-empty", "-m", "init"], tmp_path)
    return tmp_path


@pytest.fixture
def _ref_backend(advisory_lock: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """A local (no-remote) CAS, a short lease, and headroom over the local git bound.

    The production bound ``_ref_lock._LOCAL_TIMEOUT_SECS`` (5.0 s) is a fail-closed safety
    policy and is not touched; ``monkeypatch`` restores it after each test. These tests assert
    lock semantics, not latency, so a host starved by concurrent suites must not fail them with
    a correct "held" verdict (bug ddc8). That a timeout becomes ``RefLockTimeoutError`` is
    proved by ``test_ref_lock.py::test_timeout_raises_reflocktimeout``, independent of the bound.
    """
    monkeypatch.setattr(advisory_lock, "_lock_lease_secs", lambda: 1)
    monkeypatch.setattr(advisory_lock, "_lock_remote", lambda repo_root: None)
    monkeypatch.setattr(advisory_lock._load_ref_lock(), "_LOCAL_TIMEOUT_SECS", 60.0)


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
        check=False,
    )
    assert show.returncode != 0, "ref backend must not write a tickets-branch lock file"
    advisory_lock.release_pass_lock("pass-1", tmp_git_repo, oid=oid)
    gone = subprocess.run(
        ["git", "-C", str(tmp_git_repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        check=False,
    )
    assert gone.returncode != 0, "release must delete the ref"


# One local `git update-ref` slower than the production bound `_ref_lock._LOCAL_TIMEOUT_SECS`
# (5.0 s). The bound is a fail-closed safety policy (a timeout means "treat as HELD"), proved
# independently of its value by test_ref_lock.py::test_timeout_raises_reflocktimeout; this test
# is about lock semantics, so its fixture gives it headroom (bug ddc8).
_SLOW_UPDATE_REF_SECS = 5.6


def test_ref_backend_roundtrip_survives_a_slow_local_git_call(
    advisory_lock: ModuleType,
    tmp_git_repo: Path,
    _ref_backend: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Acquire and release still succeed when each ``git update-ref`` takes longer than the
    production bound, as it can on a host starved by concurrent suites."""
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path_factory.mktemp("slowgit")
    shim = bin_dir / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "if 'update-ref' in sys.argv[1:]:\n"
        f"    time.sleep({_SLOW_UPDATE_REF_SECS})\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(bin_dir), os.environ["PATH"])))

    oid = advisory_lock.acquire_pass_lock("pass-slow", tmp_git_repo)
    assert oid, "a slow local git call must not make the free lock look held"
    ref = advisory_lock._load_ref_lock().LOCK_REF
    advisory_lock.release_pass_lock("pass-slow", tmp_git_repo, oid=oid)
    gone = subprocess.run(
        ["git", "-C", str(tmp_git_repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        check=False,
    )
    assert gone.returncode != 0, "release must delete the ref"


def test_git_adapter_patches_are_shared_across_package_and_by_path_loaders(
    advisory_lock: ModuleType,
    tmp_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both standalone lock seams reuse the canonical package module object."""
    from rebar.reconciler import git_adapter

    remote_calls: list[tuple[Path, str]] = []
    git_calls: list[tuple[str, ...]] = []

    def remote_get_url(repo_root: Path, remote: str) -> subprocess.CompletedProcess:
        remote_calls.append((repo_root, remote))
        return subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")

    def run_git(repo_root: str, *args: str, **_kwargs) -> subprocess.CompletedProcess:
        git_calls.append((repo_root, *args))
        return subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_adapter, "remote_get_url", remote_get_url)
    monkeypatch.setattr(git_adapter, "run_git", run_git)

    assert advisory_lock._lock_remote(tmp_git_repo) == "origin"
    advisory_lock._load_ref_lock()._git(
        tmp_git_repo,
        ["status", "--porcelain"],
        timeout=1,
        check=False,
    )

    assert remote_calls == [(tmp_git_repo, "origin")]
    assert git_calls == [(str(tmp_git_repo), "status", "--porcelain")]


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
    """A historical pause sentinel blocks all write-capable modes."""
    ref_lock = advisory_lock._load_ref_lock()
    assert advisory_lock.check_phase_gate(mode_mod.Mode.BOOTSTRAP_THROTTLE, tmp_git_repo) is False
    ref_lock.set_gate(tmp_git_repo, "reconcile-check")
    assert ref_lock.read_gate(tmp_git_repo) == "reconcile-check"
    assert (
        ref_lock.read_gate(tmp_git_repo, remote=advisory_lock._lock_remote(tmp_git_repo))
        == "reconcile-check"
    )
    for write_mode in (
        mode_mod.Mode.BOOTSTRAP_STRICT,
        mode_mod.Mode.BOOTSTRAP_THROTTLE,
        mode_mod.Mode.LIVE,
    ):
        assert advisory_lock._mode_blocks(write_mode, "reconcile-check") is True
        assert advisory_lock.check_phase_gate(write_mode, tmp_git_repo) is True
    assert advisory_lock.check_phase_gate(mode_mod.Mode.DRY_RUN, tmp_git_repo) is False
