"""A write reclaims an orphaned mkdir lock left by a dead process (yaw-gravel-linen).

End-to-end replication of the ticket: an orphaned ``.ticket-write.lock.d`` (the
kind a writer/push that was SIGKILLed mid-commit leaves — now stamped with its
owning host:pid) must not block the next write for the full ~60s lock budget. The
write detects the dead owner, reclaims the lock, and proceeds promptly.

The second test pins the git ``index.lock`` stale-reclaim TOCTOU hardening
(story sundried-bonny-sloth): reclamation must re-validate the lock's identity
(device+inode) and age at the moment of removal, so a peer that replaces a stale
lock with a fresh LIVE one mid-reclaim is never wrongly clobbered.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import rebar
from rebar._store import gitutil
from rebar._store import lock as _lock


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_write_reclaims_dead_owner_lock_promptly(rebar_repo: Path):
    tid = rebar.create_ticket("task", "stale-lock target", repo_root=str(rebar_repo))
    tracker = rebar_repo / ".tickets-tracker"

    # Orphan a mkdir lock stamped by a now-dead process (as a killed rebar writer
    # would leave behind).
    lock_dir = tracker / _lock.MKDIR_LOCK_NAME
    lock_dir.mkdir()
    (lock_dir / "owner").write_text(f"{socket.gethostname()}:{_dead_pid()}", encoding="utf-8")

    t0 = time.monotonic()
    rebar.comment(tid, "written past the stale lock", repo_root=str(rebar_repo))
    elapsed = time.monotonic() - t0

    # Before the fix this blocked the full write budget (~60s) then failed.
    assert elapsed < 15.0, f"write blocked on the orphaned lock: {elapsed:.1f}s"
    bodies = [c["body"] for c in rebar.show_ticket(tid, repo_root=str(rebar_repo))["comments"]]
    assert "written past the stale lock" in bodies
    # The lock was released cleanly (stamp + dir gone).
    assert not os.path.exists(lock_dir)


def _resolved_index_lock(tracker: str) -> Path:
    """The git ``index.lock`` path the reclaim path targets, via the prod resolver."""
    from rebar._commands.fsck import _resolve_tracker_git_dir

    git_dir = _resolve_tracker_git_dir(tracker)
    assert git_dir, "tracker git dir should resolve"
    return Path(git_dir) / "index.lock"


def test_stale_index_lock_not_reclaimed_when_inode_replaced_midflight(
    rebar_repo: Path, monkeypatch
):
    """TOCTOU regression (sundried-bonny-sloth): a peer removes the stale ``index.lock``
    and drops a FRESH LIVE lock at the same path in the window between the staleness
    decision and the unlink. Reclamation must NOT delete the peer's fresh lock — it
    re-validates device+inode+age immediately before removing, and aborts on mismatch.

    Deterministic (no sleeps): the file swap is injected through the ``_reclaim_probe``
    seam that fires exactly after the stale decision and before the guarded removal.
    """
    tracker = str(rebar_repo / ".tickets-tracker")
    lock_file = _resolved_index_lock(tracker)

    # A provably-stale lock (old mtime) — the reclaim path will judge it stale.
    lock_file.write_text("stale-owner")
    old = time.time() - (gitutil._INDEX_LOCK_STALE_S + 60)
    os.utime(lock_file, (old, old))

    # The instant reclamation has judged the lock stale (and BEFORE it unlinks), a peer
    # replaces it: removes the stale file and drops a fresh live lock (fresh mtime) at the
    # same path. (The re-validation compares device+inode AND age; even where the OS reuses
    # the freed inode number, the fresh mtime alone must be enough to abort the unlink.)
    fresh_marker = "peer-fresh-owner"

    def _peer_replaces_lock() -> None:
        lock_file.unlink()
        lock_file.write_text(fresh_marker)  # fresh mtime (inode number may be reused by the OS)

    monkeypatch.setattr(gitutil, "_reclaim_probe", _peer_replaces_lock)

    gitutil._reclaim_if_stale_index_lock(tracker)

    # The peer's fresh LIVE lock must survive — reclamation must have aborted the unlink.
    assert lock_file.exists(), "the peer's fresh live index.lock was wrongly reclaimed"
    assert lock_file.read_text() == fresh_marker


def test_stationary_stale_index_lock_is_still_reclaimed(rebar_repo: Path):
    """The hardening must not regress the base case: a genuinely stale lock that is NOT
    replaced mid-flight is still reclaimed (unlinked). Guards against an over-strict
    re-validation that never removes anything."""
    tracker = str(rebar_repo / ".tickets-tracker")
    lock_file = _resolved_index_lock(tracker)

    lock_file.write_text("stale-owner")
    old = time.time() - (gitutil._INDEX_LOCK_STALE_S + 60)
    os.utime(lock_file, (old, old))

    gitutil._reclaim_if_stale_index_lock(tracker)

    assert not lock_file.exists(), "a stationary stale lock should be reclaimed"
