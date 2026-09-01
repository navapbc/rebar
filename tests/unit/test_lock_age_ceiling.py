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
ON A TIMER ALONE" — a branch with a POSITIVE liveness signal keeps refusing regardless of age.

What counts as positive requires care, and bug larval-tribal-tigermoth narrowed it. A live-pid
probe is proof only when a start time CORROBORATES it: ``os.kill(pid, 0)`` alone says the pid
NUMBER is in use, and numbers recycle. On a platform with no ``/proc`` the start time is never
observable, so that branch was refusing forever on no proof at all — an UNBOUNDED wedge, the
worst instance of the very class this ceiling exists to close. It now takes the ceiling like
every other refuse-without-proof branch; the corroborated-live case is still never reclaimed
on a timer. (This was research rec #6, previously deferred.)
"""

from __future__ import annotations

import logging
import os
import subprocess

import pytest

from rebar._store import lock as _lock
from rebar._store import lock_owner as _owner

_HOST_BOOT_ID = "3f2b1c58-0d4a-4b2e-9a77-6c1f0e5d8a90"
_NS = "4026531836"
_OTHER_NS = "4026533117"


def _dead_pid() -> int:
    """A PID that is no longer alive: spawn a trivial child and reap it."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _pose_as(monkeypatch, *, boot_id: str | None, hostname: str, ns: str | None) -> None:
    monkeypatch.setattr(_owner, "_read_boot_id", lambda: boot_id)
    monkeypatch.setattr(_owner, "_read_pid_namespace_id", lambda: ns)
    monkeypatch.setattr(_owner.socket, "gethostname", lambda: hostname)


def _seed(tmp_path, stamp: str | None) -> str:
    """Create a held mkdir lock dir carrying *stamp* (or no owner file when None)."""
    lock_dir = os.path.join(str(tmp_path), _lock.MKDIR_LOCK_NAME)
    os.mkdir(lock_dir)
    if stamp is not None:
        with open(os.path.join(lock_dir, _owner._MKDIR_OWNER_FILE), "w", encoding="utf-8") as fh:
            fh.write(stamp)
    return lock_dir


def _age_past_ceiling(lock_dir: str, *, over_s: int = 1) -> float:
    """Backdate the lock dir's mtime to *over_s* seconds beyond the stale ceiling.

    The stamp is never refreshed (no heartbeat), so the dir mtime is the acquisition
    time — the wall-clock quantity the ceiling is measured against."""
    import time

    aged = time.time() - (_owner._MKDIR_LOCK_STALE_CEILING_S + over_s)
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

    assert _owner._mkdir_lock_is_stale(lock_dir) is True
    handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
    handle.release()


def test_aged_foreign_host_is_reclaimed_but_fresh_is_not(tmp_path, monkeypatch):
    """A genuinely foreign-host stamp (different boot id, shared filesystem) is never
    reclaimed while fresh — but past the ceiling the backstop breaks the permanent wedge.
    Only AGE changed between the two assertions, proving the ceiling, not identity, decided."""
    _pose_as(monkeypatch, boot_id="11111111-2222-3333-4444-555555555555", hostname="host-a", ns=_NS)
    foreign = _owner._owner_stamp()
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host-b", ns=_OTHER_NS)
    lock_dir = _seed(tmp_path, foreign)

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False, "fresh: refused"
    _age_past_ceiling(lock_dir)
    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True, "aged: reclaimed"


def test_aged_malformed_v2_stamp_is_reclaimed_but_fresh_is_not(tmp_path, monkeypatch):
    """A torn/malformed v2 stamp carries no usable identity — refused while fresh, reclaimed
    past the ceiling."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    lock_dir = _seed(tmp_path, "rebar-lock v2 host=only-a-host-field")

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False, "fresh: refused"
    _age_past_ceiling(lock_dir)
    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True, "aged: reclaimed"


# --- SAFETY: a positive liveness signal beats age (never on a timer alone) -----------------


def test_aged_live_owner_is_never_reclaimed(tmp_path, monkeypatch):
    """A stamp for a LIVE same-host, same-namespace, start-time-matching owner is proven
    alive — the ceiling must never override that, even when the dir is older than the
    ceiling. Proof of life beats age."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: "111")
    lock_dir = _seed(tmp_path, _owner._owner_stamp())  # our own live pid, start=111
    _age_past_ceiling(lock_dir)

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False
    with pytest.raises(_lock.LockTimeout):
        _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    assert os.path.isdir(lock_dir)


def test_aged_live_pid_with_unknown_start_is_reclaimed_by_the_ceiling(tmp_path, monkeypatch):
    """THE BOUNDARY, MOVED (bug larval-tribal-tigermoth). Same host, same namespace, the
    stamped pid probes ALIVE but its start time is unconfirmable — the macOS case, where
    ``_process_start_time`` reads ``/proc`` and so ALWAYS returns None.

    This used to assert refusal, on the reading that ``os.kill(pid, 0)`` is a positive
    liveness signal. It is not, unqualified: with no start time it only says the NUMBER is in
    use, and pid numbers recycle. Left refusing, an orphan stamp wedged the store with NO
    bound at all — the unbounded wedge this bug was filed for. It is therefore a
    refuse-without-proof branch and takes the ceiling, like every other such branch.

    Breaking a genuinely-live owner remains impossible here: reaching this call at all means
    the caller holds the exclusive fcntl leg, which a live owner inside its own hold would
    still be holding. See ``test_aged_live_owner_is_never_reclaimed`` for the corroborated
    case, which is still never reclaimed on a timer."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: None)  # start unknowable
    lock_dir = _seed(tmp_path, _owner._owner_stamp())  # our own live pid
    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is False, (
        "a FRESH unqualified-live stamp is still honoured — the bound is the ceiling, "
        "not an instant reclaim"
    )

    _age_past_ceiling(lock_dir)

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True


# --- OBSERVABILITY: reclaim discloses the holder + age (research rec #8) --------------------


def test_reclaim_logs_the_holder_stamp_and_age(tmp_path, monkeypatch, caplog):
    """Breaking an unprovable-but-aged lock must be observable, not silent: the reclaim logs
    the owner stamp and the dir age at WARNING so the wedge is attributable."""
    _pose_as(monkeypatch, boot_id="11111111-2222-3333-4444-555555555555", hostname="host-a", ns=_NS)
    foreign = _owner._owner_stamp()
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host-b", ns=_OTHER_NS)
    lock_dir = _seed(tmp_path, foreign)
    _age_past_ceiling(lock_dir)

    with caplog.at_level(logging.WARNING, logger="rebar._store.lock_owner"):
        _owner._reclaim_mkdir_lock(lock_dir)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("11111111-2222-3333-4444-555555555555" in m for m in warnings), (
        f"holder stamp not disclosed: {warnings!r}"
    )
    assert any("age" in m.lower() for m in warnings), f"dir age not disclosed: {warnings!r}"


# --- RECYCLED PID ON A PLATFORM WITH NO /proc (bug larval-tribal-tigermoth) ----------------
# The reported wedge: the holder is DEAD, but its pid NUMBER has been reissued to an unrelated
# live process. `_pid_alive` says "live", and with no start time to qualify that verdict the
# lock was judged fresh FOREVER. These pin the reclamation and its bound.


def _recycled_stamp(dead_pid: int) -> str:
    """An owner stamp for a pid that has been RECYCLED: the number now belongs to a live
    process (ours), but the stamp was written by the dead original owner. `start=-` is what
    every no-/proc platform records, so the recycled-pid discriminator has nothing to compare."""
    return (
        f"{_owner._STAMP_V2_PREFIX} host={_owner._host_identity()} ns={_NS} pid={dead_pid} start=-"
    )


def test_recycled_pid_with_no_proc_is_reclaimed_when_fcntl_is_held(tmp_path, monkeypatch):
    """A dead holder whose pid number is now held by a LIVE unrelated process, on a platform
    where the start time is unobservable. The free fcntl leg is authoritative for another
    process: if that process still owned the write lock, this waiter could not hold fcntl."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: None)  # no /proc
    # The stamp names a pid that IS alive but is NOT the process that wrote the stamp.
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: True)
    lock_dir = _seed(tmp_path, _recycled_stamp(_dead_pid()))

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True


def test_unqualified_live_pid_without_fcntl_proof_still_waits_for_the_ceiling(
    tmp_path, monkeypatch
):
    """Without the kernel-fcntl proof, a fresh unqualified pid match is still refused.

    This is the contrast case that keeps the fix from becoming a bare "pid live is stale"
    rule: file-shaped locks and diagnostic callers without fcntl proof retain the ceiling.
    """
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: None)
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: True)
    lock_dir = _seed(tmp_path, _recycled_stamp(_dead_pid()))

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=False) is False, (
        "fresh without fcntl proof: still honoured — the instant reclaim is tied to the "
        "kernel lock proof"
    )

    _age_past_ceiling(lock_dir)

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=False) is True


def test_recycled_pid_with_a_known_differing_start_is_reclaimed_at_once(tmp_path, monkeypatch):
    """Where /proc DOES exist the recycled pid is proven recycled by its start time, and is
    reclaimed immediately — no ceiling wait. Regression guard: the fix must not have made this
    case depend on age."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    monkeypatch.setattr(_owner, "_process_start_time", lambda pid: "111")
    stamp = _owner._owner_stamp().replace("start=111", "start=999")
    lock_dir = _seed(tmp_path, stamp)  # live pid, but a DIFFERENT process

    assert _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True) is True, (
        "proven recycled: reclaimed on evidence, not on a timer"
    )


def test_ownerless_dir_left_by_a_failed_rmdir_is_bounded(tmp_path, monkeypatch):
    """`_reclaim_mkdir_lock` removes the owner file then rmdirs, both best-effort. If the
    rmdir fails an owner-LESS dir remains, and only the ceiling can clear it. Pins that
    secondary wedge as bounded."""
    _pose_as(monkeypatch, boot_id=_HOST_BOOT_ID, hostname="host", ns=_NS)
    lock_dir = _seed(tmp_path, _owner._owner_stamp())
    monkeypatch.setattr(_owner.os, "rmdir", _boom)
    _owner._reclaim_mkdir_lock(lock_dir)  # best-effort: must not raise

    assert os.path.isdir(lock_dir), "the failed rmdir left the dir behind"
    assert not os.path.exists(os.path.join(lock_dir, _owner._MKDIR_OWNER_FILE))
    assert _owner._mkdir_lock_is_stale(lock_dir) is False, "fresh owner-less dir: still honoured"

    _age_past_ceiling(lock_dir)

    assert _owner._mkdir_lock_is_stale(lock_dir) is True


def _boom(*_a, **_kw):
    raise OSError("rmdir refused")


# --- THE SPLIT ITSELF: no re-export shim ---------------------------------------------------


def test_lock_module_does_not_re_export_the_moved_private_names():
    """The staleness cluster moved to `lock_owner` (module-size seam). A convenience re-export
    on `lock` would keep attribute access working while SILENTLY breaking every test that
    patches these names, because `_mkdir_lock_is_stale` resolves them through its own module
    globals. There must be exactly one home, so patches reach the code under test."""
    moved = (
        "_mkdir_lock_is_stale",
        "_reclaim_mkdir_lock",
        "_owner_stamp",
        "_pid_alive",
        "_process_start_time",
        "_parse_v2_stamp",
        "_host_identity",
        "_read_boot_id",
        "_read_pid_namespace_id",
        "_legacy_stamp_is_stale",
        "_describe_stamped_pid",
        "_mkdir_lock_age_s",
        "_mkdir_lock_age_exceeds_ceiling",
        "_MKDIR_OWNER_FILE",
        "_MKDIR_LOCK_STALE_CEILING_S",
    )
    leaked = [name for name in moved if name in vars(_lock)]
    assert leaked == [], f"lock.py re-exports moved names, defeating patch targets: {leaked}"
    assert all(hasattr(_owner, name) for name in moved), "lock_owner must own all moved names"
