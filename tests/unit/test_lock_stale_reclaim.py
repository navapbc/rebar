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

import errno as _errno
import os
import socket
import subprocess
import time as _time

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


# ── Direct _acquire_fcntl errno discrimination (story sulfureous-albino-fallowdeer) ──
#
# `_acquire_fcntl` used to catch EVERY OSError identically and wait out the deadline, so a
# genuine system fault (ENOLCK/EIO/EBADF) was masked as a spurious LockTimeout. It now branches
# on errno: contention (EAGAIN/EACCES) waits until the deadline then returns -1; any other errno
# is re-raised immediately with its identity. The fd is closed on every failure path.


def _raise_errno(err: int):
    """A fake ``fcntl.flock`` that always raises ``OSError(err, …)``."""

    def _flock(fd, op):  # noqa: ANN001
        raise OSError(err, os.strerror(err))

    return _flock


def test_acquire_fcntl_contention_waits_then_returns_minus_one(tmp_path, monkeypatch):
    """EAGAIN contention: retry until the deadline, then return -1 (unchanged behavior)."""
    lock_path = os.path.join(str(tmp_path), "wl")

    def _always_eagain(fd, op):
        raise OSError(_errno.EAGAIN, "resource temporarily unavailable")

    monkeypatch.setattr(_lock.fcntl, "flock", _always_eagain)
    deadline = _time.monotonic() + 0.15
    fd = _lock._acquire_fcntl(lock_path, deadline)
    assert fd == -1, "sustained contention past the deadline returns the -1 sentinel"


def test_acquire_fcntl_unexpected_errno_raises_immediately(tmp_path, monkeypatch):
    """A non-contention errno (ENOLCK) is re-raised at once with its errno, NOT masked as -1."""
    lock_path = os.path.join(str(tmp_path), "wl")

    def _enolck(fd, op):
        raise OSError(_errno.ENOLCK, "no locks available")

    monkeypatch.setattr(_lock.fcntl, "flock", _enolck)
    # A far-future deadline: the OLD code would wait ~here for it; the fix raises promptly.
    deadline = _time.monotonic() + 30
    t0 = _time.monotonic()
    with pytest.raises(OSError) as exc:
        _lock._acquire_fcntl(lock_path, deadline)
    assert exc.value.errno == _errno.ENOLCK
    assert _time.monotonic() - t0 < 1.0, "must surface immediately, not wait out the deadline"


def test_acquire_fcntl_closes_fd_on_timeout(tmp_path, monkeypatch):
    """The fd opened at the top is closed on the -1 (timeout) path."""
    lock_path = os.path.join(str(tmp_path), "wl")
    closed: list[int] = []
    real_close = _lock.os.close

    def _spy_close(fd):  # noqa: ANN001
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(_lock.fcntl, "flock", _raise_errno(_errno.EAGAIN))
    monkeypatch.setattr(_lock.os, "close", _spy_close)
    fd = _lock._acquire_fcntl(lock_path, _time.monotonic() + 0.1)
    assert fd == -1
    assert closed, "the opened fd must be closed on the timeout path"


def test_acquire_fcntl_closes_fd_on_unexpected_errno(tmp_path, monkeypatch):
    """The fd is closed on the re-raise (unexpected errno) path too."""
    lock_path = os.path.join(str(tmp_path), "wl")
    closed: list[int] = []
    real_close = _lock.os.close

    def _spy_close(fd):  # noqa: ANN001
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(_lock.fcntl, "flock", _raise_errno(_errno.EIO))
    monkeypatch.setattr(_lock.os, "close", _spy_close)
    with pytest.raises(OSError):
        _lock._acquire_fcntl(lock_path, _time.monotonic() + 30)
    assert closed, "the opened fd must be closed on the re-raise path"


# ── canonical_tracker symlink/realpath lock identity (story elliptic-secondbest-nuthatch) ──
#
# `acquire` canonicalizes the tracker path with `canonical_tracker` (os.path.realpath), so a
# symlinked-path caller and a real-path caller contend on the SAME `.ticket-write.lock` /
# `.ticket-write.lock.d`. These tests prove that mutual exclusion holds across the two path
# spellings, in both directions. Determinism: the two acquirers are independent `acquire()`
# calls (separate fcntl open-file-descriptions + separate os.mkdir), which contend regardless
# of process; the second uses a bounded `timeout=1, attempts=1`, so the held lock GUARANTEES a
# LockTimeout (not a sleep-timing race). Were canonicalization absent, the two paths would key
# different lock files, the second acquire would succeed, and the assertion would fail.


def _tracker_and_symlink(tmp_path) -> tuple[str, str]:
    real = os.path.join(str(tmp_path), "tracker")
    os.mkdir(real)
    link = os.path.join(str(tmp_path), "tracker-link")
    os.symlink(real, link)
    return real, link


def test_symlink_acquirer_blocked_by_realpath_holder(tmp_path):
    """Hold via the real path; a second acquire via a symlink to the tracker is blocked."""
    real, link = _tracker_and_symlink(tmp_path)
    handle = _lock.acquire(real, timeout=1, attempts=1)
    try:
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(link, timeout=1, attempts=1)
    finally:
        handle.release()
    # After release, the symlink caller can take the same lock.
    freed = _lock.acquire(link, timeout=1, attempts=1)
    freed.release()


def test_realpath_acquirer_blocked_by_symlink_holder(tmp_path):
    """Reverse direction: hold via the symlink; a second acquire via the real path is blocked."""
    real, link = _tracker_and_symlink(tmp_path)
    handle = _lock.acquire(link, timeout=1, attempts=1)
    try:
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(real, timeout=1, attempts=1)
    finally:
        handle.release()
    freed = _lock.acquire(real, timeout=1, attempts=1)
    freed.release()


# ── Malformed / unreadable mkdir owner-stamp forms (story innovative-halfcrazed-monkey) ──
#
# `_mkdir_lock_is_stale` must be CONSERVATIVE: an absent, malformed, foreign-host, or
# unreadable owner stamp must NEVER be judged stale (reclaimable) — only a stamp that proves a
# dead PID on THIS host may. These table-driven cases pin every malformed/unreadable branch.


def _seed_owner_stamp(tmp_path, content: str | None) -> str:
    """Create a held mkdir lock dir with *content* as its owner file (None = no owner file)."""
    lock_dir = os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME)
    os.mkdir(lock_dir)
    if content is not None:
        with open(os.path.join(lock_dir, _lock._MKDIR_OWNER_FILE), "w", encoding="utf-8") as fh:
            fh.write(content)
    return lock_dir


@pytest.mark.parametrize(
    "label,stamp",
    [
        ("empty", ""),
        ("missing_colon", "hostnamewithnocolon"),
        ("empty_host", ":12345"),
        ("empty_pid", f"{socket.gethostname()}:"),
        ("nonnumeric_pid", f"{socket.gethostname()}:notanumber"),
        ("negative_pid", f"{socket.gethostname()}:-1"),
        ("zero_pid", f"{socket.gethostname()}:0"),
        ("extra_delimiters", f"{socket.gethostname()}:123:456"),
        ("foreign_host", "some-other-host:1"),
        # A reader catching the stamp write mid-flight sees the hostname before the ':pid':
        ("partial_midwrite", socket.gethostname()),
    ],
)
def test_malformed_owner_stamp_is_never_stale(tmp_path, label, stamp):
    """No malformed/foreign/partial stamp is ever judged stale (conservative — no reclaim)."""
    lock_dir = _seed_owner_stamp(tmp_path, stamp)
    assert _lock._mkdir_lock_is_stale(lock_dir) is False, f"{label!r} must not be reclaimable"


def test_absent_owner_file_is_never_stale(tmp_path):
    """A mkdir lock with NO owner file (e.g. a bash-style lock) is never reclaimable."""
    lock_dir = _seed_owner_stamp(tmp_path, None)
    assert _lock._mkdir_lock_is_stale(lock_dir) is False


def test_unreadable_owner_file_is_never_stale(tmp_path):
    """An owner path that cannot be read as a file (here: a directory in its place → an
    OSError on open) is never reclaimable — the read-error branch is conservative."""
    lock_dir = os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME)
    os.mkdir(lock_dir)
    os.mkdir(os.path.join(lock_dir, _lock._MKDIR_OWNER_FILE))  # a dir where a file is expected
    assert _lock._mkdir_lock_is_stale(lock_dir) is False
