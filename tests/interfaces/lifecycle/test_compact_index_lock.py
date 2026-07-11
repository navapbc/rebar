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
import threading
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


def test_compact_rides_out_contended_index_lock(rebar_repo: Path) -> None:
    """A CONTENDED (live/young) index.lock released mid-backoff is ridden out by the retry
    loop on the compact write path — the ride-out counterpart to the stale-reclaim test
    above (ticket 3b4e requires both scenarios per newly-covered path)."""
    tid = _seed(rebar_repo, "compactable-contended")
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.comment(tid, "note", repo_root=str(rebar_repo))

    lock = _index_lock_path(_tracker(rebar_repo))
    lock.write_text("")  # a fresh lock: a live peer mid-write
    threading.Timer(0.4, lambda: lock.exists() and lock.unlink()).start()

    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    assert rc == 0, "compaction must ride out a contended index.lock via retry backoff"
    assert _has_snapshot(rebar_repo, tid), "a SNAPSHOT should have been written"
