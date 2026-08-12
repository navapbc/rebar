"""A same-host orphan is reclaimable when the container id changed (bug castoff-tigerseye-ammonite).

``_owner_stamp`` recorded ``socket.gethostname():pid``, and ``_mkdir_lock_is_stale``
refused to reclaim unless the recorded host equalled ``socket.gethostname()``. Inside a
container the hostname IS the container id, and a ``compose up -d`` recreate assigns a new
one — so a lock orphaned by the SAME host's previous container was misclassified as the
foreign-host (shared-filesystem) case and could never be reclaimed. With no age ceiling the
orphan was permanent: production 2026-07-31 saw the review-bot's boot go from <4s to ~62s
for every subsequent deploy.

The fix keeps the refusal-without-proof contract of bug yaw-gravel-linen intact and instead
makes "same host" provable independently of the hostname:

* the stamp records a **boot id** (``/proc/sys/kernel/random/boot_id``, stable across
  container recreates on one kernel) rather than the hostname, plus the **pid namespace**
  and the owning pid's **start time**;
* a same-host owner in the SAME pid namespace is still judged by probing the pid (now
  start-time-qualified, so a recycled pid cannot masquerade as the owner);
* a same-host owner in a DIFFERENT pid namespace (the container recreate) is not
  pid-probeable, so it is reclaimed only under the fcntl proof: the caller holds the
  exclusive ``fcntl.flock`` leg of the same tracker, which the kernel shares across
  namespaces on one host and releases when its holder dies — so no live same-host owner
  of a stamped mkdir lock can exist while we hold it.

A genuinely foreign host has a different boot id and is still never reclaimed.
"""

from __future__ import annotations

import os
import socket
import subprocess

import pytest

from rebar._store import lock as _lock
from rebar._store import lock_owner as _owner

_OLD_CONTAINER = "1e0fab75d5bd"  # the container that was SIGKILLed mid-store-write
_NEW_CONTAINER = "9a7c22ef4411"  # its replacement after `compose up -d`
_HOST_BOOT_ID = "3f2b1c58-0d4a-4b2e-9a77-6c1f0e5d8a90"  # unchanged across the recreate
_OLD_NS = "4026531836"
_NEW_NS = "4026533117"


def _dead_pid() -> int:
    """A PID that is no longer alive: spawn a trivial child and reap it."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _seed(tmp_path, stamp: str) -> str:
    """Create a held mkdir lock dir carrying *stamp* as its owner file."""
    lock_dir = os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME)
    os.mkdir(lock_dir)
    with open(os.path.join(lock_dir, _owner._MKDIR_OWNER_FILE), "w", encoding="utf-8") as fh:
        fh.write(stamp)
    return lock_dir


def _pose_as(monkeypatch, *, boot_id: str | None, hostname: str, ns: str | None) -> None:
    """Make this process look like a container: fixed boot id, container-id hostname, ns."""
    monkeypatch.setattr(_owner, "_read_boot_id", lambda: boot_id)
    monkeypatch.setattr(_owner, "_read_pid_namespace_id", lambda: ns)
    monkeypatch.setattr(_owner.socket, "gethostname", lambda: hostname)


def test_container_recreate_orphan_is_reclaimed(tmp_path, monkeypatch):
    """THE BUG: the dead container's stamp has a different hostname AND a different pid
    namespace, but the same boot id — the same host. acquire() must reclaim it and succeed
    promptly instead of refusing forever."""
    # The doomed container stamps the lock, then is SIGKILLed (pid never released it).
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname=_OLD_CONTAINER, ns=_OLD_NS)
    orphan_stamp = _owner._owner_stamp()
    _seed(tmp_path, orphan_stamp)

    # Its replacement boots: new container id, new pid namespace, same host.
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname=_NEW_CONTAINER, ns=_NEW_NS)

    handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
    try:
        assert os.path.isdir(os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME))
    finally:
        handle.release()
    assert not os.path.exists(os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME))


def test_cross_namespace_reclaim_requires_the_fcntl_proof(tmp_path, monkeypatch):
    """The cross-namespace reclaim rests entirely on holding the fcntl leg. Without that
    proof asserted, the same stamp is NOT judged stale — the guard is not merely loosened."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname=_OLD_CONTAINER, ns=_OLD_NS)
    lock_dir = _seed(tmp_path, _owner._owner_stamp())
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname=_NEW_CONTAINER, ns=_NEW_NS)

    assert _owner._mkdir_lock_is_stale(lock_dir) is False, "no reclaim without the fcntl proof"
    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True


def test_live_owner_in_same_namespace_is_still_never_reclaimed(tmp_path, monkeypatch):
    """SAFETY (no regression of yaw-gravel-linen): a lock owned by a LIVE process in this
    same pid namespace is never reclaimed, even under the fcntl proof — acquire times out."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname=_OLD_CONTAINER, ns=_OLD_NS)
    lock_dir = _seed(tmp_path, _owner._owner_stamp())  # stamped with OUR live pid

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False
    with pytest.raises(_lock.LockTimeout):
        _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    assert os.path.isdir(lock_dir)


def test_foreign_host_is_still_never_reclaimed(tmp_path, monkeypatch):
    """SAFETY (no regression of yaw-gravel-linen): a DIFFERENT boot id is a genuinely foreign
    host on a shared filesystem — never reclaimed, even with a dead-looking pid, a differing
    namespace, and the fcntl proof held."""
    _pose_as(
        monkeypatch,
        boot_id="11111111-2222-3333-4444-555555555555",
        hostname="host-a",
        ns=_OLD_NS,
    )
    foreign = _owner._owner_stamp().replace(f"pid={os.getpid()}", f"pid={_dead_pid()}")
    lock_dir = _seed(tmp_path, foreign)

    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host-b", ns=_NEW_NS)
    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False
    with pytest.raises(_lock.LockTimeout):
        _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    assert os.path.isdir(lock_dir)


def test_dead_owner_in_same_namespace_is_reclaimed(tmp_path, monkeypatch):
    """The classic same-host orphan still reclaims through the v2 stamp: same boot id, same
    namespace, dead pid."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname=_OLD_CONTAINER, ns=_OLD_NS)
    dead = _dead_pid()
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: None)
    lock_dir = _seed(tmp_path, _owner._owner_stamp().replace(f"pid={os.getpid()}", f"pid={dead}"))

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True


def test_recycled_pid_does_not_masquerade_as_the_owner(tmp_path, monkeypatch):
    """A live pid whose START TIME differs from the stamp is a DIFFERENT process that merely
    inherited the number — the true owner is gone, so the lock is stale. Without this the
    lock would be permanently unreclaimable on pid reuse (the same defect class)."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname=_OLD_CONTAINER, ns=_OLD_NS)
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: "111")
    lock_dir = _seed(tmp_path, _owner._owner_stamp())  # stamped start=111, our own live pid

    # The pid is still alive, but it is now a different process (start time moved on).
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: "222")
    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True

    # Same pid, same start time → genuinely the live owner → never reclaimed.
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: "111")
    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False


def test_new_stamp_is_inert_to_a_legacy_reader(tmp_path, monkeypatch):
    """FORWARD COMPAT: an older rebar parses the stamp as ``<host>:<pid>``. The v2 stamp must
    never parse into "this host + a dead pid" for such a reader, or the old code could reclaim
    a live lock. Reproduces the legacy parse verbatim and asserts it declines."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname=_OLD_CONTAINER, ns=_OLD_NS)
    stamp = _owner._owner_stamp()

    host, sep, pid_s = stamp.partition(":")
    legacy_would_probe = bool(sep) and host == socket.gethostname() and pid_s.isdigit()
    assert not legacy_would_probe, f"legacy reader would act on the v2 stamp: {stamp!r}"
