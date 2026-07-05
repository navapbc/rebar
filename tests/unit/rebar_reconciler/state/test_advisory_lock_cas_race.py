"""Ref-backend CAS-race coverage for _advisory_lock.py.

The original tickets-branch file-lock CAS tests were retired with the file backend
in epic dust-troth-naval / C4 (ADR 0031). What remains is the ref-backend CAS race:
concurrent acquirers of the create-only CAS on refs/reconciler/* must resolve to a
single winner (the shared _is_cas_mismatch discriminator classifies the exit-128
'reference already exists' for the reconciler ref).

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


def _load_advisory_lock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rebar_reconciler_advisory_lock_casrace", LOCK_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler_advisory_lock_casrace"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def lock_mod() -> ModuleType:
    return _load_advisory_lock_module()


def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo)] + args, capture_output=True, text=True, check=True
    )


@pytest.fixture
def tmp_git_repo_with_tickets(tmp_path: Path) -> Path:
    """A git repo with an orphan tickets branch, checked out on main."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


def test_ref_backend_create_cas_single_winner(
    lock_mod: ModuleType, tmp_git_repo_with_tickets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two acquirers race the create-only CAS on refs/reconciler/lock; exactly one
    wins and the other gets ReconcileLockError (the CAS discriminator classifies the
    exit-128 'reference already exists' correctly for the reconciler ref)."""
    monkeypatch.setattr(lock_mod, "_lock_backend", lambda: "ref")
    monkeypatch.setattr(lock_mod, "_lock_lease_secs", lambda: 1)
    monkeypatch.setattr(lock_mod, "_lock_remote", lambda repo_root: None)
    repo = tmp_git_repo_with_tickets

    first = lock_mod.acquire_pass_lock("pass-A", repo)
    assert first, "first acquirer wins the create-only CAS"
    with pytest.raises(lock_mod.ReconcileLockError):
        lock_mod.acquire_pass_lock("pass-B", repo)
    lock_mod.release_pass_lock("pass-A", repo, oid=first)
    second = lock_mod.acquire_pass_lock("pass-B", repo)
    assert second, "after release the ref is re-acquirable"
