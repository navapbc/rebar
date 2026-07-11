"""The compaction write path self-heals a git ``index.lock`` too (ticket
snide-cut-mussel AC: ``compact.py`` is a named sibling committer).

``compact.py`` collapses a ticket's events into a SNAPSHOT via its own ``_git`` shim.
That shim now routes through ``gitutil.run_git_write`` (like the event-append and
transition/claim paths), so a stale/contended ``index.lock`` on the shared tickets
worktree is reclaimed-if-stale / ridden-out here as well, instead of failing the
compaction hard. This test plants a REAL stale lock and asserts compaction succeeds.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import rebar
from rebar._commands import compact as _compact
from rebar._store import gitutil

_STALE_S = getattr(gitutil, "_INDEX_LOCK_STALE_S", 300)


def _seed(repo: Path, title: str) -> str:
    return rebar.create_ticket(
        "task",
        title,
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )


def _tracker(repo: Path) -> str:
    return str(repo / ".tickets-tracker")


def _index_lock_path(tracker: str) -> Path:
    p = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-path", "index.lock"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(p) if os.path.isabs(p) else Path(tracker) / p


def _has_snapshot(repo: Path, tid: str) -> bool:
    tdir = repo / ".tickets-tracker" / tid
    return any(p.name.endswith("-SNAPSHOT.json") for p in tdir.glob("*.json"))


def test_compact_reclaims_stale_index_lock(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo, "compactable")
    # a couple more events so there is something to fold
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.comment(tid, "note", repo_root=str(rebar_repo))

    lock = _index_lock_path(_tracker(rebar_repo))
    lock.write_text("")  # leftover lock from a crashed git process
    old = time.time() - (_STALE_S + 60)
    os.utime(lock, (old, old))

    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    assert rc == 0, "compaction must self-heal a stale index.lock, not fail hard"
    assert _has_snapshot(rebar_repo, tid), "a SNAPSHOT should have been written"
    assert not lock.exists(), "the stale lock should have been reclaimed by the compact write"


def test_compact_rides_out_contended_index_lock(rebar_repo: Path, monkeypatch) -> None:
    """A CONTENDED (live/young) index.lock is ridden out by the retry loop on the compact
    write path. Deterministic (no timer): a fresh lock is planted and released via the
    ``_retry_probe`` seam ONLY after the first attempt is confirmed to have failed with a
    genuine index.lock error — so the retry path is provably exercised even on a slow runner.
    """
    tid = _seed(rebar_repo, "compactable-contended")
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.comment(tid, "note", repo_root=str(rebar_repo))

    lock = _index_lock_path(_tracker(rebar_repo))
    lock.write_text("")  # a fresh (young) lock: a live peer mid-write — NOT reclaimable-as-stale

    attempts: list[tuple[int, int, bool]] = []
    released = {"done": False}

    def _probe(n: int, result) -> None:  # noqa: ANN001
        is_lock = gitutil._is_index_lock_error(result.stderr or result.stdout or "")
        attempts.append((n, result.returncode, is_lock))
        # Release the lock only AFTER the first genuine index.lock failure is confirmed, so
        # the NEXT attempt succeeds — deterministic, never released before the first failure.
        if not released["done"] and result.returncode != 0 and is_lock and lock.exists():
            lock.unlink()
            released["done"] = True

    monkeypatch.setattr(gitutil, "_retry_probe", _probe)

    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    assert rc == 0, "compaction must ride out a contended index.lock via retry backoff"
    assert _has_snapshot(rebar_repo, tid), "a SNAPSHOT should have been written"
    # The retry path was actually exercised: some git op reached a 2nd attempt …
    assert any(n >= 2 for n, _, _ in attempts), f"retry path not exercised: {attempts}"
    # … and the first observed failure was a genuine index.lock error (not some other error).
    first_fail = next((a for a in attempts if a[1] != 0), None)
    assert first_fail is not None and first_fail[2], f"first failure not index.lock: {attempts}"


def test_compact_path_nonlock_failure_is_not_retried(monkeypatch) -> None:
    """The retry loop rides out ONLY the index.lock signature: a non-lock git failure
    returns on the FIRST attempt and is never retried. Asserted directly against the shared
    ``_with_index_lock_retry`` seam that the compact write path routes through."""
    attempts: list[int] = []
    monkeypatch.setattr(gitutil, "_retry_probe", lambda n, r: attempts.append(n))
    calls = {"n": 0}

    def run_once():
        calls["n"] += 1
        return subprocess.CompletedProcess(["git"], 1, stdout="", stderr="fatal: some other error")

    result = gitutil._with_index_lock_retry("/nonexistent-tracker", run_once)
    assert result.returncode == 1
    assert calls["n"] == 1, "a non-lock failure must not be retried"
    assert attempts == [1], f"probe should observe exactly one attempt: {attempts}"
