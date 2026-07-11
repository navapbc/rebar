"""The delete and init store-write paths self-heal a git ``index.lock`` too
(sibling ticket 3b4e — every ticket-store committer must route through the
index.lock-resilient ``gitutil.run_git_write``, with a regression test per path).

Companion to ``test_compact_index_lock.py`` (which covers the compact path). Here we
pin the ``delete.py`` and ``init.py`` write paths: a provably-stale ``index.lock`` on
the tracker is reclaimed and the write succeeds, instead of failing hard.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import rebar
from rebar import config
from rebar._commands import delete as _delete
from rebar._commands import init as _init
from rebar._store import gitutil

_STALE_S = getattr(gitutil, "_INDEX_LOCK_STALE_S", 300)


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> str:
    return str(config.tracker_dir(str(repo)))


def _plant_stale_lock(tracker: str) -> Path:
    p = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-path", "index.lock"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    lock = Path(p) if os.path.isabs(p) else Path(tracker) / p
    lock.write_text("")  # leftover lock from a crashed git process
    old = time.time() - (_STALE_S + 60)
    os.utime(lock, (old, old))
    return lock


def test_delete_reclaims_stale_index_lock(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "del")
    tid = rebar.create_ticket("task", "doomed", repo_root=str(repo))
    lock = _plant_stale_lock(_tracker(repo))

    rc = _delete.delete_cli([tid, "--user-approved"], repo_root=str(repo))
    assert rc == 0, "delete must self-heal a stale index.lock, not fail hard"
    assert not lock.exists(), "the stale lock should have been reclaimed by the delete write"
    # the ticket is tombstoned (its .tombstone.json marker was committed)
    assert (Path(_tracker(repo)) / tid / ".tombstone.json").is_file()


def test_init_git_commit_reclaims_stale_index_lock(tmp_path: Path) -> None:
    """init.py's ``_git`` shim (which routes through ``run_git_write``) self-heals a stale
    index.lock on an index-mutating commit — the same resilience the shared wrapper gives
    the event-append/transition/compact/delete paths."""
    repo = _init_repo(tmp_path, "ini")
    tracker = _tracker(repo)
    # stage a change so there is something to commit through init._git
    (Path(tracker) / ".init-lock-probe").write_text("x")
    _init._git(tracker, "add", "--", ".init-lock-probe")
    lock = _plant_stale_lock(tracker)

    cp = _init._git(tracker, "commit", "-q", "--no-verify", "-m", "probe: init _git self-heal")
    assert cp.returncode == 0, "init._git must self-heal a stale index.lock, not fail hard"
    assert not lock.exists(), "the stale lock should have been reclaimed by init._git"


def _plant_fresh_lock(tracker: str) -> Path:
    """Plant a *fresh* (young) index.lock — a live peer mid-write, NOT reclaimable-as-stale.
    Deterministically released later by the ``_retry_probe`` (see ``_release_after_first_fail``),
    not by a timer, so the retry path is provably exercised regardless of runner speed."""
    p = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-path", "index.lock"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    lock = Path(p) if os.path.isabs(p) else Path(tracker) / p
    lock.write_text("")  # fresh mtime
    return lock


def _release_after_first_fail(lock: Path, attempts: list) -> Callable[[int, object], None]:
    """A ``_retry_probe`` that records ``(attempt, returncode, is_index_lock_error)`` and
    unlinks *lock* exactly ONCE — only after the first genuine index.lock failure — so the
    NEXT attempt succeeds. The lock is never released before the first failure is confirmed."""
    released = {"done": False}

    def _probe(n: int, result) -> None:  # noqa: ANN001
        is_lock = gitutil._is_index_lock_error(result.stderr or result.stdout or "")
        attempts.append((n, result.returncode, is_lock))
        if not released["done"] and result.returncode != 0 and is_lock and lock.exists():
            lock.unlink()
            released["done"] = True

    return _probe


def _assert_retry_exercised(attempts: list) -> None:
    """Assert some git op reached a 2nd attempt; its first failure was a real index.lock error."""
    assert any(n >= 2 for n, _, _ in attempts), f"retry path not exercised: {attempts}"
    first_fail = next((a for a in attempts if a[1] != 0), None)
    assert first_fail is not None and first_fail[2], f"first failure not index.lock: {attempts}"


def test_delete_rides_out_contended_index_lock(tmp_path: Path, monkeypatch) -> None:
    """A CONTENDED lock is ridden out by the retry loop on the delete path — not reclaimed as
    stale, but retried until it clears. Deterministic: released via the ``_retry_probe`` seam
    only after the first confirmed index.lock failure (no timer race)."""
    repo = _init_repo(tmp_path, "delc")
    tid = rebar.create_ticket("task", "doomed", repo_root=str(repo))
    lock = _plant_fresh_lock(_tracker(repo))
    attempts: list = []
    monkeypatch.setattr(gitutil, "_retry_probe", _release_after_first_fail(lock, attempts))

    rc = _delete.delete_cli([tid, "--user-approved"], repo_root=str(repo))
    assert rc == 0, "delete must ride out a contended index.lock via retry backoff"
    assert (Path(_tracker(repo)) / tid / ".tombstone.json").is_file()
    _assert_retry_exercised(attempts)


def test_init_git_rides_out_contended_index_lock(tmp_path: Path, monkeypatch) -> None:
    """init._git rides out a contended index.lock via the retry loop. Deterministic: the lock
    is released through the ``_retry_probe`` seam only after the first confirmed failure."""
    repo = _init_repo(tmp_path, "inic")
    tracker = _tracker(repo)
    (Path(tracker) / ".init-contended-probe").write_text("x")
    _init._git(tracker, "add", "--", ".init-contended-probe")
    lock = _plant_fresh_lock(tracker)
    attempts: list = []
    monkeypatch.setattr(gitutil, "_retry_probe", _release_after_first_fail(lock, attempts))

    cp = _init._git(tracker, "commit", "-q", "--no-verify", "-m", "probe: init _git ride-out")
    assert cp.returncode == 0, "init._git must ride out a contended index.lock via retry"
    _assert_retry_exercised(attempts)


def test_delete_init_path_nonlock_failure_is_not_retried(monkeypatch) -> None:
    """The retry loop rides out ONLY the index.lock signature: a non-lock git failure returns
    on the FIRST attempt and is never retried. Asserted directly against the shared
    ``_with_index_lock_retry`` seam that the delete/init write paths route through."""
    attempts: list[int] = []
    monkeypatch.setattr(gitutil, "_retry_probe", lambda n, r: attempts.append(n))
    calls = {"n": 0}

    def run_once():
        calls["n"] += 1
        return subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal: unrelated error")

    result = gitutil._with_index_lock_retry("/nonexistent-tracker", run_once)
    assert result.returncode == 128
    assert calls["n"] == 1, "a non-lock failure must not be retried"
    assert attempts == [1], f"probe should observe exactly one attempt: {attempts}"
