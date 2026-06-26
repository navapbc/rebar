"""Stale mkdir-lock reclamation (bug yaw-gravel-linen).

The fcntl leg of the write lock is kernel-backed and auto-released when its holder
dies; the mkdir leg (``.ticket-write.lock.d``) was not — an orphaned dir left by a
writer that was SIGKILLed mid-commit blocked every subsequent acquire for the full
budget and then failed. The lock now stamps the owning host:pid on acquire and
reclaims a held mkdir lock ONLY when that stamp proves the owner is a dead process
on this host. Reclamation is conservative by construction — a live owner, an
absent/unparseable stamp, or a foreign-host owner is never reclaimed — so mutual
exclusion is preserved.
"""

from __future__ import annotations

import os
import socket
import subprocess

import pytest

from rebar._store import lock as _lock


def _dead_pid() -> int:
    """A PID that is no longer alive: spawn a trivial child and reap it."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _seed_mkdir_lock(tracker, owner: str | None) -> None:
    lock_dir = os.path.join(str(tracker), _lock.MKDIR_LOCK_NAME)
    os.mkdir(lock_dir)
    if owner is not None:
        with open(os.path.join(lock_dir, "owner"), "w", encoding="utf-8") as fh:
            fh.write(owner)


def test_acquire_reclaims_dead_owner_mkdir_lock(tmp_path):
    """An orphaned mkdir lock stamped with a DEAD pid on this host is reclaimed, so
    acquire succeeds promptly instead of blocking the whole budget then failing."""
    _seed_mkdir_lock(tmp_path, owner=f"{socket.gethostname()}:{_dead_pid()}")

    handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
    try:
        # We hold it now (reclaimed the stale one and re-created our own).
        assert os.path.isdir(os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME))
    finally:
        handle.release()
    # Fully released — no residue.
    assert not os.path.exists(os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME))


def test_acquire_does_not_reclaim_live_owner(tmp_path):
    """SAFETY: a mkdir lock owned by a LIVE process (here, ourselves) is never
    reclaimed — acquire must time out, preserving mutual exclusion."""
    _seed_mkdir_lock(tmp_path, owner=f"{socket.gethostname()}:{os.getpid()}")
    with pytest.raises(_lock.LockTimeout):
        _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    assert os.path.isdir(os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME))


def test_acquire_does_not_reclaim_ownerless_lock(tmp_path):
    """SAFETY: a stamp-less mkdir lock (a bash-style lock, or one observed mid-stamp)
    is never reclaimed — its owner cannot be proven dead."""
    _seed_mkdir_lock(tmp_path, owner=None)
    with pytest.raises(_lock.LockTimeout):
        _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    assert os.path.isdir(os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME))


def test_acquire_does_not_reclaim_foreign_host_owner(tmp_path):
    """SAFETY: an owner on a DIFFERENT host is never reclaimed — we cannot check the
    liveness of a remote PID (guards the shared-filesystem case)."""
    _seed_mkdir_lock(tmp_path, owner=f"some-other-host:{_dead_pid()}")
    with pytest.raises(_lock.LockTimeout):
        _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    assert os.path.isdir(os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME))
