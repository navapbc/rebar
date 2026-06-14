"""Unit tests for the tickets-ref CAS-race retry fix (bug 1f47-9337-3db0-4f3c).

Background
----------
``_write_file_to_tickets_branch`` / ``_delete_file_from_tickets_branch`` read the
tickets-branch tip (``old_sha``), build a commit in a detached worktree, then
advance the ref with a single-shot compare-and-swap::

    git update-ref refs/heads/tickets <new_sha> <old_sha>

When a *concurrent* tickets-branch writer (ticket-CLI event commit, per-pass
bindings commit, agent comment) advances the ref between the ``old_sha`` read
and the CAS, ``git update-ref`` exits 128 (old-sha mismatch). Previously this
raised ``CalledProcessError`` → ``rebase_retry`` returned ``abort_due_to_error``
→ ``acquire_pass_lock``'s outer loop (which only retries
``reject_and_reschedule``) broke immediately and raised ``ReconcileLockError``,
silently aborting the reconcile pass.

The fix wraps the read-tip → build-commit → CAS sequence in a bounded retry
loop: on an exit-128 CAS mismatch it re-reads the tip and rebuilds on the new
tip. A *legitimate* lock-held condition is a higher-level concern (the lock file
is present) and is unaffected — these tests assert the CAS-race path retries and
succeeds, while a non-CAS git failure still fails fast.

Module loading follows the importlib.util.spec_from_file_location convention.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
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
        ["git", "-C", str(repo)] + args,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def tmp_git_repo_with_tickets(tmp_path: Path) -> Path:
    """A git repo with an orphan tickets branch (no lock file), checked out on main."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)

    _git(["checkout", "--orphan", "tickets"], tmp_path)
    _git(["rm", "-rf", "--cached", "."], tmp_path)
    import shutil

    for item in tmp_path.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    (tmp_path / ".gitkeep").write_text("")
    _git(["add", ".gitkeep"], tmp_path)
    _git(["commit", "-m", "tickets branch init"], tmp_path)
    try:
        _git(["checkout", "main"], tmp_path)
    except subprocess.CalledProcessError:
        _git(["checkout", "master"], tmp_path)
    return tmp_path


def _advance_tickets_ref(repo: Path) -> None:
    """Simulate a concurrent writer: add an empty commit on the tickets tip.

    Uses a detached worktree (mirrors the production write path) so the main
    branch pointer is untouched.
    """
    import shutil
    import tempfile

    parent = Path(tempfile.mkdtemp(prefix="concurrent-writer-"))
    wt = parent / "wt"
    try:
        _git(["worktree", "add", "--detach", str(wt), "tickets"], repo)
        old = _git(["rev-parse", "tickets"], repo).stdout.strip()
        marker = wt / f"concurrent-{time.time_ns()}.txt"
        marker.write_text("concurrent writer\n")
        subprocess.run(["git", "add", marker.name], cwd=str(wt), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "concurrent writer commit"],
            cwd=str(wt),
            check=True,
            capture_output=True,
        )
        new = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(wt),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _git(["update-ref", "refs/heads/tickets", new, old], repo)
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(parent, ignore_errors=True)


def _patch_update_ref_to_race(lock_mod, repo: Path, fail_times: int):
    """Patch lock_mod._git_run so the FIRST *fail_times* update-ref CAS calls
    are preceded by a real concurrent ref advance — causing the CAS old-sha to
    be stale and exit 128 — then subsequent calls proceed normally.

    Returns a dict capturing {"update_ref_calls": int}.
    """
    original_git_run = lock_mod._git_run
    state = {"update_ref_calls": 0, "races_injected": 0}

    def patched_git_run(repo_root, args):
        if args and args[0] == "update-ref" and "refs/heads/tickets" in args:
            state["update_ref_calls"] += 1
            if state["races_injected"] < fail_times:
                state["races_injected"] += 1
                # Advance the ref out from under this CAS so old_sha is stale.
                _advance_tickets_ref(Path(repo_root))
        return original_git_run(repo_root, args)

    lock_mod._git_run = patched_git_run
    return state


def test_acquire_pass_lock_retries_on_cas_race(
    lock_mod: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """acquire_pass_lock survives a concurrent tickets-ref advance during CAS.

    Inject a concurrent ref advance before the update-ref CAS twice; the
    helper must re-read the tip and rebuild on the new tip, eventually
    succeeding. Without the CAS-retry fix this raises ReconcileLockError
    (abort_due_to_error).
    """
    repo = tmp_git_repo_with_tickets
    pass_id = f"cas-race-{time.time_ns()}"

    state = _patch_update_ref_to_race(lock_mod, repo, fail_times=2)
    try:
        lock_mod.acquire_pass_lock(pass_id, repo)
    finally:
        # restore handled by fresh module load per test
        pass

    assert lock_mod.check_pass_lock(repo) is True, (
        "After a CAS race retry, the lock file must be present on the tickets branch"
    )
    assert state["races_injected"] == 2, (
        "Test must have injected 2 CAS races; "
        f"got {state['races_injected']} (update_ref_calls={state['update_ref_calls']})"
    )
    # The CAS must have been retried beyond the first (failed) attempt.
    assert state["update_ref_calls"] >= 3, (
        "Expected at least 3 update-ref CAS attempts (2 raced + 1 success); "
        f"got {state['update_ref_calls']}"
    )


def test_release_pass_lock_retries_on_cas_race(
    lock_mod: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """release_pass_lock survives a concurrent tickets-ref advance during CAS.

    Mirrors the acquire test for the delete path
    (_delete_file_from_tickets_branch).
    """
    repo = tmp_git_repo_with_tickets
    pass_id = f"cas-race-release-{time.time_ns()}"

    # Acquire cleanly first (no race).
    lock_mod.acquire_pass_lock(pass_id, repo)
    assert lock_mod.check_pass_lock(repo) is True

    state = _patch_update_ref_to_race(lock_mod, repo, fail_times=2)
    lock_mod.release_pass_lock(pass_id, repo)

    assert lock_mod.check_pass_lock(repo) is False, (
        "After a CAS race retry on release, the lock file must be removed"
    )
    assert state["races_injected"] == 2
    assert state["update_ref_calls"] >= 3


def test_cas_retry_bounded_then_fails(
    lock_mod: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """Unbounded CAS contention eventually surfaces as a ReconcileLockError.

    If every CAS attempt races (a pathological case), acquire_pass_lock must
    NOT loop forever — it must exhaust its bounded budget and raise. This keeps
    the lock-held / genuine-failure path honest (no retry-forever).
    """
    repo = tmp_git_repo_with_tickets
    pass_id = f"cas-race-forever-{time.time_ns()}"

    # Race on EVERY update-ref CAS (large fail_times).
    _patch_update_ref_to_race(lock_mod, repo, fail_times=10_000)

    with pytest.raises(lock_mod.ReconcileLockError):
        lock_mod.acquire_pass_lock(pass_id, repo)


def test_non_cas_git_failure_still_fails_fast(
    lock_mod: ModuleType, tmp_git_repo_with_tickets: Path
) -> None:
    """A non-CAS git failure (e.g. worktree add error) must NOT be CAS-retried.

    Only the update-ref exit-128 old-sha mismatch is a retryable CAS race.
    Other CalledProcessErrors must propagate (fail-CLOSED) so genuine faults
    are not masked by the retry loop.
    """
    repo = tmp_git_repo_with_tickets
    pass_id = f"non-cas-fail-{time.time_ns()}"

    original_git_run = lock_mod._git_run
    state = {"worktree_add_calls": 0}

    def patched_git_run(repo_root, args):
        if args and args[0] == "worktree" and "add" in args:
            state["worktree_add_calls"] += 1
            raise subprocess.CalledProcessError(
                1, ["git", "worktree", "add"], "", "fatal: simulated worktree failure"
            )
        return original_git_run(repo_root, args)

    lock_mod._git_run = patched_git_run

    with pytest.raises(lock_mod.ReconcileLockError):
        lock_mod.acquire_pass_lock(pass_id, repo)

    # Must fail fast: the worktree-add error is not a CAS race, so it is not
    # retried across many CAS attempts.
    assert state["worktree_add_calls"] <= 1, (
        "Non-CAS git failures must fail fast, not be CAS-retried; "
        f"worktree_add_calls={state['worktree_add_calls']}"
    )
