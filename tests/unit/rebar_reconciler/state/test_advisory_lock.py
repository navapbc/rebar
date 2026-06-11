"""Unit tests for _advisory_lock.py.

Tests cover the 7 acceptance criteria from task a2ba-0875-d23f-4696:

  1. test_check_pass_lock_absent       — check_pass_lock returns False when no lock file on tickets branch
  2. test_check_pass_lock_present      — check_pass_lock returns True when .reconciler-pass-lock present on tickets
  3. test_missing_tickets_branch_fails_closed — check_pass_lock raises ReconcileLockError when tickets branch missing
  4. test_acquire_release_uses_rebase_retry   — acquire/release roundtrip via _concurrency.rebase_retry
  5. test_phase_gate_blocks_advancement — check_phase_gate blocks BOOTSTRAP_THROTTLE when gate file present,
                                          allows BOOTSTRAP_STRICT

Plus two additional tests from AC amendment comments (G4 + G5):
  6. test_git_show_unrecognised_stderr_fails_closed — unrecognised non-zero exit raises ReconcileLockError
  7. test_release_pass_lock_ownership_check          — release with wrong pass_id leaves file, logs warning

Module loading follows the importlib.util.spec_from_file_location convention
established in conftest.py and test_concurrency.py.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
LOCK_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_advisory_lock.py"
)
MODE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mode.py"
CONCURRENCY_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_concurrency.py"
)


def _load_mode_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rebar_reconciler_mode", MODE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler_mode"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_concurrency_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rebar_reconciler_concurrency", CONCURRENCY_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler_concurrency"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_advisory_lock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rebar_reconciler_advisory_lock", LOCK_PATH
    )
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
    """Return the _advisory_lock module; fail all tests if absent."""
    if not LOCK_PATH.exists():
        pytest.fail(
            f"_advisory_lock.py not found at {LOCK_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_advisory_lock_module()


@pytest.fixture(scope="module")
def mode_mod() -> ModuleType:
    """Return the mode module."""
    return _load_mode_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], repo: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo)] + args,
        capture_output=True,
        text=True,
        check=True,
        **kwargs,
    )


@pytest.fixture()
def tmp_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with one commit on main and return its root."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


@pytest.fixture()
def tmp_git_repo_with_tickets(tmp_git_repo: Path) -> Path:
    """Extend tmp_git_repo with an orphan tickets branch (no lock file)."""
    _git(["checkout", "--orphan", "tickets"], tmp_git_repo)
    _git(["rm", "-rf", "--cached", "."], tmp_git_repo)
    # Remove all actual files from the working tree in a cross-platform way
    import shutil

    for item in tmp_git_repo.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    placeholder = tmp_git_repo / ".gitkeep"
    placeholder.write_text("")
    _git(["add", ".gitkeep"], tmp_git_repo)
    _git(["commit", "-m", "tickets branch init"], tmp_git_repo)
    # Switch back to main/master
    try:
        _git(["checkout", "main"], tmp_git_repo)
    except subprocess.CalledProcessError:
        _git(["checkout", "master"], tmp_git_repo)
    return tmp_git_repo


@pytest.fixture()
def tmp_git_repo_with_lock(tmp_git_repo_with_tickets: Path) -> tuple[Path, str]:
    """Extend tickets branch to include a .reconciler-pass-lock file.

    Returns (repo_root, pass_id).
    """
    repo = tmp_git_repo_with_tickets
    pass_id = "test-pass-001"
    # Switch to tickets branch, add lock file, commit, switch back
    _git(["checkout", "tickets"], repo)
    lock_file = repo / ".reconciler-pass-lock"
    lock_file.write_text(f"{pass_id}\n{time.time_ns()}\n")
    _git(["add", ".reconciler-pass-lock"], repo)
    _git(["commit", "-m", "acquire lock"], repo)
    try:
        _git(["checkout", "main"], repo)
    except subprocess.CalledProcessError:
        _git(["checkout", "master"], repo)
    return repo, pass_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_check_pass_lock_absent(
    advisory_lock: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """check_pass_lock returns False when no lock file on tickets branch."""
    result = advisory_lock.check_pass_lock(tmp_git_repo_with_tickets)
    assert result is False, (
        "check_pass_lock must return False when .reconciler-pass-lock is absent "
        f"on the tickets branch; got {result!r}"
    )


def test_check_pass_lock_present(
    advisory_lock: ModuleType, tmp_git_repo_with_lock: tuple
) -> None:
    """check_pass_lock returns True when .reconciler-pass-lock present on tickets."""
    repo, _pass_id = tmp_git_repo_with_lock
    result = advisory_lock.check_pass_lock(repo)
    assert result is True, (
        "check_pass_lock must return True when .reconciler-pass-lock exists "
        f"on the tickets branch; got {result!r}"
    )


def test_missing_tickets_branch_fails_closed(
    advisory_lock: ModuleType, tmp_git_repo: Path
) -> None:
    """check_pass_lock raises ReconcileLockError when tickets branch is missing.

    Per fail-CLOSED discipline: a missing tickets branch must raise
    ReconcileLockError, not silently return False (which would disable
    concurrency protection).
    """
    # tmp_git_repo has no tickets branch
    with pytest.raises(advisory_lock.ReconcileLockError):
        advisory_lock.check_pass_lock(tmp_git_repo)


def test_git_show_unrecognised_stderr_fails_closed(
    advisory_lock: ModuleType, tmp_path: Path
) -> None:
    """G4: unrecognised non-zero git exit raises ReconcileLockError (fail-CLOSED).

    Covers the third stderr-discrimination path: exit!=0 with stderr that
    matches neither 'unknown revision' nor 'does not exist in'.
    """
    # We mock subprocess.run so we can inject an unrecognised stderr payload.
    import subprocess as sp

    fake_result = MagicMock()
    fake_result.returncode = 128
    fake_result.stderr = "some completely unrecognised git error output"
    fake_result.stdout = ""

    with patch.object(sp, "run", return_value=fake_result):
        with pytest.raises(advisory_lock.ReconcileLockError):
            advisory_lock.check_pass_lock(tmp_path)


def test_acquire_release_uses_rebase_retry(
    advisory_lock: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """acquire/release roundtrip uses _concurrency.rebase_retry (not raw git commit).

    Verifies:
    - acquire_pass_lock + release_pass_lock complete without error.
    - After acquire, check_pass_lock returns True.
    - After release with matching pass_id, check_pass_lock returns False.
    - The internal write path goes through rebase_retry (tested via monkeypatching).
    """
    repo = tmp_git_repo_with_tickets
    pass_id = f"roundtrip-{time.time_ns()}"

    rebase_retry_call_count = {"n": 0}

    # Load _concurrency module to get the real rebase_retry
    concurrency_mod = _load_concurrency_module()
    original_rebase_retry = concurrency_mod.rebase_retry

    def counting_rebase_retry(repo_root, write_fn, **kwargs):
        rebase_retry_call_count["n"] += 1
        return original_rebase_retry(repo_root, write_fn, **kwargs)

    # Patch rebase_retry on the advisory_lock module's reference to _concurrency
    original_fn = advisory_lock._rebase_retry  # type: ignore[attr-defined]
    advisory_lock._rebase_retry = counting_rebase_retry  # type: ignore[attr-defined]
    try:
        advisory_lock.acquire_pass_lock(pass_id, repo)
        assert advisory_lock.check_pass_lock(repo) is True, (
            "After acquire_pass_lock, check_pass_lock must return True"
        )
        advisory_lock.release_pass_lock(pass_id, repo)
        assert advisory_lock.check_pass_lock(repo) is False, (
            "After release_pass_lock with matching pass_id, check_pass_lock must return False"
        )
    finally:
        advisory_lock._rebase_retry = original_fn  # type: ignore[attr-defined]

    assert rebase_retry_call_count["n"] >= 2, (
        f"Expected rebase_retry to be called at least twice (acquire + release); "
        f"got {rebase_retry_call_count['n']}"
    )


def test_release_pass_lock_ownership_check(
    advisory_lock: ModuleType, tmp_git_repo_with_tickets: Path, caplog
) -> None:
    """G5: release with wrong pass_id leaves file in place and logs a warning.

    Acquire with pass_id=X, attempt release with pass_id=Y:
    - The lock file must remain (check_pass_lock still returns True).
    - A warning must be logged about the ownership mismatch.
    - No exception is raised (defensive — don't disrupt callers).
    """
    repo = tmp_git_repo_with_tickets
    pass_id_x = f"owner-x-{time.time_ns()}"
    pass_id_y = f"owner-y-{time.time_ns()}"

    advisory_lock.acquire_pass_lock(pass_id_x, repo)
    assert advisory_lock.check_pass_lock(repo) is True

    with caplog.at_level(logging.WARNING):
        # Should NOT raise
        advisory_lock.release_pass_lock(pass_id_y, repo)

    # Lock file must still be present
    assert advisory_lock.check_pass_lock(repo) is True, (
        "release_pass_lock with wrong pass_id must leave the lock file in place"
    )

    # A warning must have been logged
    warning_texts = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "pass_id" in w.lower() or "mismatch" in w.lower() or "owner" in w.lower()
        for w in warning_texts
    ), (
        f"Expected a warning about pass_id mismatch; got caplog records: {caplog.records}"
    )

    # Cleanup: release with correct id
    advisory_lock.release_pass_lock(pass_id_x, repo)


def test_acquire_pass_lock_when_tickets_checked_out_in_sibling_worktree(
    advisory_lock: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """acquire_pass_lock succeeds even when tickets branch is already checked out in a sibling worktree.

    Reproduces the bug where `git worktree add <dir> tickets` fails with exit 128
    ("fatal: 'tickets' is already used by worktree at ...") when a sibling worktree
    has the tickets branch checked out (e.g. .tickets-tracker mounted by CI pre-flight).
    The fix uses --detach so no branch-pointer conflict occurs.
    """
    repo = tmp_git_repo_with_tickets
    pass_id = f"sibling-wt-acquire-{time.time_ns()}"

    import shutil
    import tempfile

    # Simulate a sibling worktree with tickets branch checked out
    sibling_wt_parent = Path(tempfile.mkdtemp(prefix="sibling-wt-"))
    sibling_wt = sibling_wt_parent / "tickets-tracker"
    try:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", str(sibling_wt), "tickets"],
            check=True,
            capture_output=True,
        )

        # Now acquire_pass_lock must succeed despite the tickets branch being
        # checked out in the sibling worktree above.
        advisory_lock.acquire_pass_lock(pass_id, repo)
        assert advisory_lock.check_pass_lock(repo) is True, (
            "acquire_pass_lock must succeed and lock must be visible even when "
            "tickets branch is checked out in a sibling worktree"
        )

        # Cleanup: release the lock
        advisory_lock.release_pass_lock(pass_id, repo)
        assert advisory_lock.check_pass_lock(repo) is False
    finally:
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(sibling_wt),
                ],
                check=False,
                capture_output=True,
            )
        except Exception:
            pass
        shutil.rmtree(sibling_wt_parent, ignore_errors=True)


def test_release_pass_lock_when_tickets_checked_out_in_sibling_worktree(
    advisory_lock: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """release_pass_lock succeeds even when tickets branch is already checked out in a sibling worktree.

    Mirrors test_acquire_pass_lock_when_tickets_checked_out_in_sibling_worktree but
    for the release path: the `git worktree add <dir> tickets` inside
    _delete_file_from_tickets_branch must not fail with exit 128.
    """
    repo = tmp_git_repo_with_tickets
    pass_id = f"sibling-wt-release-{time.time_ns()}"

    # Acquire the lock before setting up the sibling worktree
    advisory_lock.acquire_pass_lock(pass_id, repo)
    assert advisory_lock.check_pass_lock(repo) is True

    import shutil
    import tempfile

    sibling_wt_parent = Path(tempfile.mkdtemp(prefix="sibling-wt-release-"))
    sibling_wt = sibling_wt_parent / "tickets-tracker"
    try:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", str(sibling_wt), "tickets"],
            check=True,
            capture_output=True,
        )

        # Now release_pass_lock must succeed despite the tickets branch being
        # checked out in the sibling worktree above.
        advisory_lock.release_pass_lock(pass_id, repo)
        assert advisory_lock.check_pass_lock(repo) is False, (
            "release_pass_lock must succeed and lock must be removed even when "
            "tickets branch is checked out in a sibling worktree"
        )
    finally:
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(sibling_wt),
                ],
                check=False,
                capture_output=True,
            )
        except Exception:
            pass
        shutil.rmtree(sibling_wt_parent, ignore_errors=True)


def test_phase_gate_blocks_advancement(
    advisory_lock: ModuleType, mode_mod: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """check_phase_gate blocks BOOTSTRAP_THROTTLE when gate file present on tickets;
    allows BOOTSTRAP_STRICT.

    Scenario:
    - Gate file .reconciler-phase-gate contains 'bootstrap-strict' on tickets branch.
    - check_phase_gate(BOOTSTRAP_THROTTLE, repo) returns True (blocked) because
      BOOTSTRAP_THROTTLE.rank() > BOOTSTRAP_STRICT.rank().
    - check_phase_gate(BOOTSTRAP_STRICT, repo) returns False (not blocked) because
      BOOTSTRAP_STRICT.rank() == BOOTSTRAP_STRICT.rank() (equal, not strictly greater).
    - check_phase_gate when no gate file on tickets returns False (no block).
    """
    repo = tmp_git_repo_with_tickets
    Mode = mode_mod.Mode

    # First verify: no gate file → no block
    result_no_gate = advisory_lock.check_phase_gate(Mode.BOOTSTRAP_THROTTLE, repo)
    assert result_no_gate is False, (
        "check_phase_gate must return False (not blocked) when gate file is absent; "
        f"got {result_no_gate!r}"
    )

    # Add gate file to tickets branch
    _git(["checkout", "tickets"], repo)
    gate_file = repo / ".reconciler-phase-gate"
    gate_file.write_text("bootstrap-strict\n")
    _git(["add", ".reconciler-phase-gate"], repo)
    _git(["commit", "-m", "add phase gate"], repo)
    try:
        _git(["checkout", "main"], repo)
    except subprocess.CalledProcessError:
        _git(["checkout", "master"], repo)

    # BOOTSTRAP_THROTTLE (rank 2) > BOOTSTRAP_STRICT (rank 1) → blocked
    result_throttle = advisory_lock.check_phase_gate(Mode.BOOTSTRAP_THROTTLE, repo)
    assert result_throttle is True, (
        "check_phase_gate must return True (blocked) for BOOTSTRAP_THROTTLE "
        f"when gate file contains 'bootstrap-strict'; got {result_throttle!r}"
    )

    # BOOTSTRAP_STRICT (rank 1) == BOOTSTRAP_STRICT (rank 1) → NOT blocked
    result_strict = advisory_lock.check_phase_gate(Mode.BOOTSTRAP_STRICT, repo)
    assert result_strict is False, (
        "check_phase_gate must return False (not blocked) for BOOTSTRAP_STRICT "
        f"when gate file contains 'bootstrap-strict' (same rank); got {result_strict!r}"
    )
