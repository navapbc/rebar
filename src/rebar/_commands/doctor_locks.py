"""Read-only diagnosis of store and worker locks.
Report held legs, stamped owner, same-host liveness, age, and staleness
through the shared lock helpers. An active holder is informational. A stale
lock is a finding. O_EXCL workers write owner stamps. Older unstamped
files use the shared wall-clock rule.
Doctor never reclaims locks, including under repair.
The snapshot-GC worker lock is read from the per-host snapshot store
without creating that store."""

from __future__ import annotations

import os
import time
from typing import Any

from rebar._store import lock as _lock
from rebar._store import lock_kernel as _kernel
from rebar._store import lock_owner as _owner
from rebar._store.paths import StorePaths

# Report/finding vocabulary. Kept as constants so the schema, the renderer and the tests
# name the same strings.
STATE_HELD = "held"
STATE_FREE = "free"
STATE_ABSENT = "absent"
STATE_UNKNOWN = "unknown"

STALENESS_STALE = "stale"
STALENESS_NOT_STALE = "not-stale"
# The kernel releases an fcntl lock when its holder dies, so a held fcntl leg always has
# a live holder: staleness is not a state it can be in, rather than one we failed to
# assess.
STALENESS_NOT_APPLICABLE = "not-applicable"
# A leg whose state could not be established at all — the degraded row emitted when a
# probe raises. Not a verdict about the lock, a statement that the probe failed.
STALENESS_NOT_ASSESSABLE = "not-assessable"

MECHANISM_FCNTL = "fcntl"
MECHANISM_MKDIR = "mkdir"
MECHANISM_EXISTENCE = "existence"

KIND_STALE_LOCK = "stale-lock"

# Leg names, stable across output formats so an operator (or a script) can key on them.
LEG_TICKETS_FCNTL = "tickets-write-fcntl"
LEG_TICKETS_MKDIR = "tickets-write-mkdir"
LEG_HLC = "hlc"
LEG_COMPACT_WORKER = "compact-worker"
LEG_SNAPSHOT_GC_WORKER = "snapshot-gc-worker"

_HLC_LOCK_NAME = "hlc.lock"
_COMPACT_WORKER_LOCK_NAME = "compact-worker.lock"

_STALE_ADVICE = (
    "no live process claims this lock; the next writer's acquire reclaims it "
    "automatically — doctor never removes a lock"
)


def _probe_fcntl(path: str) -> str:
    """Held/free for an fcntl lock file, answered without waiting.

    Exactly :func:`rebar._store.lock._acquire_fcntl`'s probe with a zero deadline —
    the platform's exclusive leg, non-blocking — and the same errno discrimination
    :func:`rebar._store.lock.write_lock_is_busy` uses: only ``EAGAIN``/``EACCES`` mean
    genuine contention, every other errno is a real fault and must not masquerade as a
    holder. The lock is released immediately (the ``finally`` closes the fd), and the
    file is opened WITHOUT ``O_CREAT`` so a diagnostic pass never creates a lock file
    that did not exist.
    """
    if not os.path.exists(path):
        return STATE_ABSENT
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return STATE_UNKNOWN
    try:
        try:
            _kernel.take_exclusive(fd)
        except _kernel.NoExclusiveLegError:
            # No primitive to probe WITH. Diagnostics must never raise and must never
            # claim a lock is free on no evidence, so this is exactly ``unknown``.
            return STATE_UNKNOWN
        except OSError as exc:
            if _kernel.is_contention(exc):
                return STATE_HELD
            return STATE_UNKNOWN
        try:
            _kernel.release_exclusive(fd)
        except OSError:
            pass
        return STATE_FREE
    finally:
        os.close(fd)  # closing the fd releases the leg even if the unlock failed


def _file_age_s(path: str) -> float | None:
    """Wall-clock age of *path* in seconds, or ``None`` if it cannot be stat'd."""
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return None


def _fcntl_report(name: str, path: str, *, note: str) -> dict[str, Any]:
    """A report row for one fcntl leg: held/free/absent plus *note* as its detail.

    An fcntl leg identifies no holder — the kernel owns the lock, and nothing is written
    beside it — so ``holder`` stays ``None`` and staleness is
    :data:`STALENESS_NOT_APPLICABLE` rather than unknown.
    """
    state = _probe_fcntl(path)
    return {
        "name": name,
        "path": path,
        "mechanism": MECHANISM_FCNTL,
        "state": state,
        "holder": None,
        "holder_description": None,
        "pid_state": None,
        "held_seconds": None,
        "staleness": STALENESS_NOT_APPLICABLE,
        "detail": note,
    }


def _mkdir_report(name: str, lock_dir: str) -> dict[str, Any]:
    """Report the tickets-store mkdir lock's owner, age, liveness, and shared
    staleness verdict. Missing or torn stamps remain unknown until the
    shared wall-clock rule can decide."""
    if not os.path.isdir(lock_dir):
        return {
            "name": name,
            "path": lock_dir,
            "mechanism": MECHANISM_MKDIR,
            "state": STATE_FREE,
            "holder": None,
            "holder_description": None,
            "pid_state": None,
            "held_seconds": None,
            "staleness": STALENESS_NOT_STALE,
            "detail": "no lock directory — the tickets write lock's portable leg is free",
        }

    holder: dict[str, str] | None = None
    pid_state: str | None = None
    stamp_path = os.path.join(lock_dir, _owner._MKDIR_OWNER_FILE)
    try:
        with open(stamp_path, encoding="utf-8") as fh:
            stamp = fh.read().strip()
    except OSError:
        stamp = None

    if stamp is None:
        description = "unknown (no ownership stamp)"
    else:
        fields = _owner._parse_v2_stamp(stamp)
        if fields is None:
            description = "unknown (unrecognised ownership stamp)"
        elif not fields:
            description = "unknown (incomplete ownership stamp)"
        else:
            holder = {
                "host": fields["host"],
                "ns": fields["ns"],
                "pid": fields["pid"],
                "start": fields["start"],
            }
            pid_state = _owner._describe_stamped_pid(fields)
            description = (
                f"host={fields['host']} ns={fields['ns']} "
                f"pid={fields['pid']} start={fields['start']}"
            )

    age = _owner._mkdir_lock_age_s(lock_dir)
    stale = _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=False)
    held_for = f"{age:.0f}s" if age is not None else "unknown"
    return {
        "name": name,
        "path": lock_dir,
        "mechanism": MECHANISM_MKDIR,
        "state": STATE_HELD,
        "holder": holder,
        "holder_description": description,
        "pid_state": pid_state,
        "held_seconds": age,
        "staleness": STALENESS_STALE if stale else STALENESS_NOT_STALE,
        "detail": f"held {held_for} by {description}" + (f", pid {pid_state}" if pid_state else ""),
    }


def _existence_report(name: str, path: str, *, note: str) -> dict[str, Any]:
    """Report an O_EXCL worker lock when its file exists.
    Read its stamped owner and ask the shared helper for liveness and
    staleness. Older unstamped files follow the same wall-clock rule.
    This diagnostic never reclaims the file."""
    if not os.path.exists(path):
        return {
            "name": name,
            "path": path,
            "mechanism": MECHANISM_EXISTENCE,
            "state": STATE_FREE,
            "holder": None,
            "holder_description": None,
            "pid_state": None,
            "held_seconds": None,
            "staleness": STALENESS_NOT_STALE,
            "detail": note,
        }

    holder: dict[str, str] | None = None
    pid_state: str | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            stamp: str | None = fh.read().strip()
    except OSError:
        stamp = None

    if not stamp:
        description = "unknown (no ownership stamp)"
    else:
        fields = _owner._parse_v2_stamp(stamp)
        if fields is None:
            description = "unknown (unrecognised ownership stamp)"
        elif not fields:
            description = "unknown (incomplete ownership stamp)"
        else:
            holder = {
                "host": fields["host"],
                "ns": fields["ns"],
                "pid": fields["pid"],
                "start": fields["start"],
            }
            pid_state = _owner._describe_stamped_pid(fields)
            description = (
                f"host={fields['host']} ns={fields['ns']} "
                f"pid={fields['pid']} start={fields['start']}"
            )

    age = _file_age_s(path)
    stale = _owner.stamped_file_is_stale(path)
    held_for = f"{age:.0f}s" if age is not None else "unknown"
    return {
        "name": name,
        "path": path,
        "mechanism": MECHANISM_EXISTENCE,
        "state": STATE_HELD,
        "holder": holder,
        "holder_description": description,
        "pid_state": pid_state,
        "held_seconds": age,
        "staleness": STALENESS_STALE if stale else STALENESS_NOT_STALE,
        "detail": f"held {held_for} by {description}" + (f", pid {pid_state}" if pid_state else ""),
    }


def _snapshot_gc_worker_lock_path() -> str:
    """The snapshot-GC worker lock's per-host path, derived read-only.

    Imported lazily so the census does not pull the snapshot subsystem in at module
    import; the seam (:func:`rebar._snapshot.gc_trigger.worker_lock_probe_path`) creates
    nothing — a diagnostic must not materialise a store that does not exist yet."""
    from rebar._snapshot import gc_trigger as _gc_trigger

    return str(_gc_trigger.worker_lock_probe_path())


def scan_locks(tracker: str) -> list[dict[str, Any]]:
    """Report every lock leg for *tracker*'s store. Read-only; writes nothing.

    The snapshot-GC leg is the one exception to "for *tracker*": the snapshot store is
    per-host (every checkout resolves the same root), so that row ignores *tracker*.

    Never raises: a diagnostic that fails because a lock vanished mid-read (the holder
    released) would be worse than the missing line it replaces, so each leg degrades to
    an ``unknown`` row rather than aborting the whole ``doctor`` run.
    """
    canonical = _lock.canonical_tracker(tracker)
    rebar_dir = StorePaths(canonical).rebar_dir
    legs: list[tuple[str, Any]] = [
        (
            LEG_TICKETS_FCNTL,
            lambda: _fcntl_report(
                LEG_TICKETS_FCNTL,
                os.path.join(canonical, _lock.WRITE_LOCK_NAME),
                note=(
                    "the tickets write lock's kernel leg; a holder is named by the "
                    f"{LEG_TICKETS_MKDIR} row, which carries the ownership stamp"
                ),
            ),
        ),
        (
            LEG_TICKETS_MKDIR,
            lambda: _mkdir_report(
                LEG_TICKETS_MKDIR, os.path.join(canonical, _lock.MKDIR_LOCK_NAME)
            ),
        ),
        (
            LEG_HLC,
            lambda: _fcntl_report(
                LEG_HLC,
                os.path.join(rebar_dir, _HLC_LOCK_NAME),
                note="the clock's read-modify-write lock, held only for one stamp",
            ),
        ),
        (
            LEG_COMPACT_WORKER,
            lambda: _existence_report(
                LEG_COMPACT_WORKER,
                os.path.join(rebar_dir, _COMPACT_WORKER_LOCK_NAME),
                note="no detached compaction worker holds the compact-worker lock",
            ),
        ),
        (
            LEG_SNAPSHOT_GC_WORKER,
            lambda: _existence_report(
                LEG_SNAPSHOT_GC_WORKER,
                str(_snapshot_gc_worker_lock_path()),
                note=(
                    "no detached snapshot-GC worker holds the snapshot store's worker "
                    "lock (per-host store, not this tracker; the store may not exist yet)"
                ),
            ),
        ),
    ]

    reports: list[dict[str, Any]] = []
    for name, build in legs:
        try:
            reports.append(build())
        except Exception as exc:  # noqa: BLE001 — diagnostics are best-effort
            reports.append(
                {
                    "name": name,
                    "path": "",
                    "mechanism": STATE_UNKNOWN,
                    "state": STATE_UNKNOWN,
                    "holder": None,
                    "holder_description": None,
                    "pid_state": None,
                    "held_seconds": None,
                    "staleness": STALENESS_NOT_ASSESSABLE,
                    "detail": f"could not be inspected: {type(exc).__name__}: {exc}",
                }
            )
    return reports


def lock_findings(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The subset of *reports* that is a FINDING rather than information.

    Only a lock the shared staleness function calls stale qualifies: a held lock with a
    live holder is a store under contention, which is normal operation and must not fail
    a CI gate keyed on doctor's exit code.
    """
    return [
        {
            "kind": KIND_STALE_LOCK,
            "lock": report["name"],
            "path": report["path"],
            "detail": report["detail"],
            "advice": _STALE_ADVICE,
        }
        for report in reports
        if report["staleness"] == STALENESS_STALE
    ]


def render_text(reports: list[dict[str, Any]], findings: list[dict[str, Any]]) -> list[str]:
    """Render the lock section as text lines (the caller prints them).

    Every leg gets a line, including the free ones: "the lock is free" is the answer an
    operator most often needs, and omitting it would leave them unable to tell a healthy
    store from a check that did not run.
    """
    lines = ["doctor: locks"]
    for report in reports:
        suffix = f" — {report['detail']}" if report.get("detail") else ""
        lines.append(f"  {report['name']}: {report['state']}{suffix}")
    for finding in findings:
        lines.append(
            f"{KIND_STALE_LOCK}: {finding['lock']} — {finding['detail']} [{finding['advice']}]"
        )
    return lines
