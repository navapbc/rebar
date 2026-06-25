"""A write reclaims an orphaned mkdir lock left by a dead process (yaw-gravel-linen).

End-to-end replication of the ticket: an orphaned ``.ticket-write.lock.d`` (the
kind a writer/push that was SIGKILLed mid-commit leaves — now stamped with its
owning host:pid) must not block the next write for the full ~60s lock budget. The
write detects the dead owner, reclaims the lock, and proceeds promptly.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import rebar
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
