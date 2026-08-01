"""A wall-clock age ceiling closes the permanent-wedge CLASS (9305 rec #1).

``_mkdir_lock_is_stale`` refuses to reclaim without POSITIVE proof of a dead same-host
owner (bug yaw-gravel-linen). That is correct while an owner might be live, but several
of its branches refuse for the mere ABSENCE of a liveness signal — an absent/unreadable
owner stamp, a malformed v2 stamp, a genuinely foreign-host stamp on a shared filesystem,
or a same-host stamp in an unprobeable pid namespace seen without the fcntl proof. With no
upper bound in time, any of those wedges the store FOREVER: the 2026-07-31 production
incident (review-bot boot 62s vs a 30s health check, seven rolled-back deploys) was exactly
this class, and 304e/castoff-tigerseye-ammonite fixed only ONE instance of it.

The 9305 research (rec #1) prescribes git gc's gc.pid precedent (builtin/gc.c, 12h): honour
an unprovable stamp UNTIL a generous ceiling, then reclaim, so the store can never wedge
permanently. rebar holds the write lock for a single event append (seconds), so the ceiling
is far tighter than git's 12h.

The ceiling is a BACKSTOP, never a timer on its own. Per the research's rule — "if you ever
break a lock, do it under a conservative fail-closed guard PLUS a wall-clock backstop, NEVER
ON A TIMER ALONE" — a branch with a POSITIVE liveness signal (``os.kill(pid, 0)`` says the
owner is alive) keeps refusing regardless of age. The macOS "pid alive but start-time
unconfirmable" gap is a SEPARATE research recommendation (#6) and is out of scope here.
"""

from __future__ import annotations

import logging
import os
import subprocess

import pytest

from rebar._store import lock as _lock

_HOST_BOOT_ID = "3f2b1c58-0d4a-4b2e-9a77-6c1f0e5d8a90"
_NS = "4026531836"
_OTHER_NS = "4026533117"


def _dead_pid() -> int:
    """A PID that is no longer alive: spawn a trivial child and reap it."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _pose_as(monkeypatch, *, boot_id: str | None, hostname: str, ns: str | None) -> None:
    monkeypatch.setattr(_lock, "_read_boot_id", lambda: boot_id)
    monkeypatch.setattr(_lock, "_read_pid_namespace_id", lambda: ns)
    monkeypatch.setattr(_lock.socket, "gethostname", lambda: hostname)


def _seed(tmp_path, stamp: str | None) -> str:
    """Create a held mkdir lock dir carrying *stamp* (or no owner file when None)."""
    lock_dir = os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME)
    os.mkdir(lock_dir)
    if stamp is not None:
        with open(os.path.join(lock_dir, _lock._MKDIR_OWNER_FILE), "w", encoding="utf-8") as fh:
            fh.write(stamp)
    return lock_dir


def _age_past_ceiling(lock_dir: str, *, over_s: int = 1) -> float:
    """Backdate the lock dir's mtime to *over_s* seconds beyond the stale ceiling.

    The stamp is never refreshed (no heartbeat), so the dir mtime is the acquisition
    time — the wall-clock quantity the ceiling is measured against."""
    import time

    aged = time.time() - (_lock._MKDIR_LOCK_STALE_CEILING_S + over_s)
    os.utime(lock_dir, (aged, aged))
    return aged


# --- THE WEDGE CLASS: aged, unprovable stamps are reclaimed (were permanent) --------------


def test_aged_absent_owner_stamp_is_reclaimed(tmp_path, monkeypatch):
    """A lock dir with NO owner file (the mkdir/stamp-write window, or a bash-style lock)
    is refused indefinitely today. Past the ceiling it must be reclaimed and acquire must
    succeed promptly instead of wedging forever."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    lock_dir = _seed(tmp_path, None)
    _age_past_ceiling(lock_dir)

    assert _lock._mkdir_lock_is_stale(lock_dir) is True
    handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
    handle.release()


def test_aged_foreign_host_is_reclaimed_but_fresh_is_not(tmp_path, monkeypatch):
    """A genuinely foreign-host stamp (different boot id, shared filesystem) is never
    reclaimed while fresh — but past the ceiling the backstop breaks the permanent wedge.
    Only AGE changed between the two assertions, proving the ceiling, not identity, decided."""
    _pose_as(monkeypatch, boot_id="11111111-2222-3333-4444-555555555555", hostname="host-a", ns=_NS)
    foreign = _lock._owner_stamp()
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host-b", ns=_OTHER_NS)
    lock_dir = _seed(tmp_path, foreign)

    assert _lock._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False, "fresh: refused"
    _age_past_ceiling(lock_dir)
    assert _lock._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True, "aged: reclaimed"


def test_aged_malformed_v2_stamp_is_reclaimed_but_fresh_is_not(tmp_path, monkeypatch):
    """A torn/malformed v2 stamp carries no usable identity — refused while fresh, reclaimed
    past the ceiling."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    lock_dir = _seed(tmp_path, "rebar-lock v2 host=only-a-host-field")

    assert _lock._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False, "fresh: refused"
    _age_past_ceiling(lock_dir)
    assert _lock._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True, "aged: reclaimed"


# --- SAFETY: a positive liveness signal beats age (never on a timer alone) -----------------


def test_aged_live_owner_is_never_reclaimed(tmp_path, monkeypatch):
    """A stamp for a LIVE same-host, same-namespace, start-time-matching owner is proven
    alive — the ceiling must never override that, even when the dir is older than the
    ceiling. Proof of life beats age."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    monkeypatch.setattr(_lock, "_process_start_time", lambda pid: "111")
    lock_dir = _seed(tmp_path, _lock._owner_stamp())  # our own live pid, start=111
    _age_past_ceiling(lock_dir)

    assert _lock._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False
    with pytest.raises(_lock.LockTimeout):
        _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    assert os.path.isdir(lock_dir)


def test_aged_live_pid_with_unknown_start_is_not_reclaimed_by_the_ceiling(tmp_path, monkeypatch):
    """THE BOUNDARY: same host, same namespace, the stamped pid probes ALIVE but its start
    time is unconfirmable (the macOS gap). ``os.kill(pid, 0)`` is a positive liveness signal,
    so this is "live pid + timer alone" — the ceiling must NOT reclaim it. Closing this gap is
    research rec #6 (start-time fallback), deliberately NOT this change."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    monkeypatch.setattr(_lock, "_process_start_time", lambda pid: None)  # start unknowable
    lock_dir = _seed(tmp_path, _lock._owner_stamp())  # our own live pid
    _age_past_ceiling(lock_dir)

    assert _lock._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False


# --- OBSERVABILITY: reclaim discloses the holder + age (research rec #8) --------------------


def test_reclaim_logs_the_holder_stamp_and_age(tmp_path, monkeypatch, caplog):
    """Breaking an unprovable-but-aged lock must be observable, not silent: the reclaim logs
    the owner stamp and the dir age at WARNING so the wedge is attributable."""
    _pose_as(monkeypatch, boot_id="11111111-2222-3333-4444-555555555555", hostname="host-a", ns=_NS)
    foreign = _lock._owner_stamp()
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host-b", ns=_OTHER_NS)
    lock_dir = _seed(tmp_path, foreign)
    _age_past_ceiling(lock_dir)

    with caplog.at_level(logging.WARNING, logger="rebar._store.lock"):
        _lock._reclaim_mkdir_lock(lock_dir)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("11111111-2222-3333-4444-555555555555" in m for m in warnings), (
        f"holder stamp not disclosed: {warnings!r}"
    )
    assert any("age" in m.lower() for m in warnings), f"dir age not disclosed: {warnings!r}"
