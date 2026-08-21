"""``doctor`` — read-only health report for the store's advisory locks.

A writer starved on the tickets write lock had no diagnostic surface: the only way to
learn WHO held it, whether that holder was still alive, and how long it had been held
was to ``ls`` ``.ticket-write.lock.d`` and ``cat`` its owner file by hand (ticket
metaphoric-fleeting-nutcracker). :class:`~rebar._store.lock.LockTimeout` already names
the holder, but only to the process that timed out — an operator watching from outside
saw nothing. This module answers the same questions on demand.

**It reuses; it does not re-derive.** Every fact here comes from machinery that already
exists:

* held/free is the immediate, zero-deadline probe
  :func:`rebar._store.lock.write_lock_is_busy` performs, applied per leg so the report
  can say WHICH leg is held rather than only that the pair is busy;
* the owner identity is the v2 stamp the mkdir leg already writes, decoded by
  :func:`rebar._store.lock_owner._parse_v2_stamp`;
* holder liveness is :func:`rebar._store.lock_owner._describe_stamped_pid`, which is
  same-host/same-namespace only and answers ``unprobeable (…)`` anywhere else — a pid
  number from another host or pid namespace names a different process, or nothing;
* hold age is :func:`rebar._store.lock_owner._mkdir_lock_age_s` (the lock dir's mtime is
  the acquisition time — the stamp is never refreshed);
* staleness is :func:`rebar._store.lock_owner._mkdir_lock_is_stale` for the mkdir leg and
  :func:`rebar._store.lock_owner.stamped_file_is_stale` for the file-shaped drain lock —
  two readers of ONE table and NOTHING ELSE. That table lives in
  :func:`rebar._store.lock_owner._stamp_is_stale` — the pid-recycle qualification, the
  refuse-without-proof rule (bug yaw-gravel-linen) and the wall-clock ceiling (bug
  larval-tribal-tigermoth). A second staleness heuristic here would be a fork that
  drifts from the one the lock actually obeys, so there is none: this module asks those
  functions and reports their answer. ``fcntl_held=False`` because doctor holds nothing.

**Read-only, absolutely.** Nothing here reclaims, removes, or rewrites a lock, and
``doctor --repair`` does not either (repair iterates the link-finding list, which lock
results never join). A stale lock is REPORTED with the advice that the next acquirer
reclaims it automatically; breaking someone's lock stays the acquire path's job, where
the exclusive fcntl leg makes it race-safe.

**What is a finding.** A held lock with a live holder is INFO — that is a working store
under contention, not a fault. A lock the shared staleness function calls stale is a
FINDING, because no live process claims it. Findings feed doctor's existing exit rule
(exit 1 while anything is outstanding).

Five legs are reported. The two tickets-store legs are the dual-window contract
(:mod:`rebar._store.lock`): ``.ticket-write.lock`` (fcntl) and ``.ticket-write.lock.d``
(mkdir, the leg that carries the ownership stamp). ``.rebar/hlc.lock`` is the clock's
short RMW lock (:mod:`rebar._store.hlc`) and ``.rebar/enrich-drain.lock`` is the drain
lock (:mod:`rebar.llm.enrich_drain`). The three fcntl-family legs are kernel-mediated,
so the kernel drops them when their holder dies and "stale" is not a state they can
reach; the drain lock is an ``O_EXCL`` file whose CONTENTS are its owner stamp, so it
carries a holder and a real staleness verdict from the same shared function (bug
knavish-stimulated-bluebottle; before it the drain lock was unstamped and this row
could only report ``not-assessable``). ``.rebar/compact-worker.lock`` is the compaction
trigger's worker lock (:mod:`rebar._commands.compact_trigger`, which reuses the drain's
stamped-lock machinery), the same existence-plus-stamp shape — and the lock most likely
to be held by a DETACHED background process, which is exactly the holder an operator
cannot find with ``ps``.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from typing import Any

from rebar._store import lock as _lock
from rebar._store import lock_owner as _owner

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
LEG_ENRICH_DRAIN = "enrich-drain"
LEG_COMPACT_WORKER = "compact-worker"

_HLC_LOCK_NAME = "hlc.lock"
_DRAIN_LOCK_NAME = "enrich-drain.lock"
_COMPACT_WORKER_LOCK_NAME = "compact-worker.lock"

_STALE_ADVICE = (
    "no live process claims this lock; the next writer's acquire reclaims it "
    "automatically — doctor never removes a lock"
)


def _rebar_dir(tracker: str) -> str:
    """The repo's ``.rebar`` directory, derived from *tracker* the way the drain lock
    derives it: the tracker is ``<repo>/.tickets-tracker``, so ``.rebar`` is its sibling
    (:func:`rebar.llm.enrich_drain._rebar_dir`). Derived rather than imported so this
    module does not reach into the LLM package for a path join."""
    return os.path.join(os.path.dirname(tracker), ".rebar")


def _probe_fcntl(path: str) -> str:
    """Held/free for an fcntl lock file, answered without waiting.

    Exactly :func:`rebar._store.lock._acquire_fcntl`'s probe with a zero deadline —
    ``flock(LOCK_EX|LOCK_NB)`` — and the same errno discrimination
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
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                return STATE_HELD
            return STATE_UNKNOWN
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return STATE_FREE
    finally:
        os.close(fd)  # closing the fd releases the leg even if LOCK_UN failed


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
    """A report row for the tickets store's mkdir leg — the only leg that names its
    holder.

    Decodes the v2 ownership stamp into ``host``/``ns``/``pid``/``start``, adds the
    holder-liveness verdict and the hold age, and asks
    :func:`rebar._store.lock_owner._mkdir_lock_is_stale` — the sole staleness authority —
    whether the lock is reclaimable. A stamp that is absent, unrecognised or torn
    mid-write yields an explicit ``unknown (…)`` description instead of a guess, matching
    :func:`rebar._store.lock.describe_lock_holder`; staleness is still the shared
    function's answer, because its refuse-without-proof branches already cover exactly
    those shapes (they are only reclaimable past its own wall-clock ceiling).
    """
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
    """A report row for an ``O_EXCL`` existence lock: held iff the file is there.

    The lock file carries the v2 ownership stamp as its own contents (bug
    knavish-stimulated-bluebottle gave the drain lock the discipline the mkdir leg
    already had), so this decodes host/ns/pid/start, adds the same same-host-only
    liveness verdict, and asks :func:`rebar._store.lock_owner.stamped_file_is_stale` —
    the shared decision table, reached by a second reader rather than a second heuristic.
    A lock written by a rebar predating the stamp reads as unstamped and is adjudicated
    by that table's wall-clock ceiling, which is exactly how such an orphan gets
    reclaimed; doctor still only reports it.
    """
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


def scan_locks(tracker: str) -> list[dict[str, Any]]:
    """Report every lock leg for *tracker*'s store. Read-only; writes nothing.

    Never raises: a diagnostic that fails because a lock vanished mid-read (the holder
    released) would be worse than the missing line it replaces, so each leg degrades to
    an ``unknown`` row rather than aborting the whole ``doctor`` run.
    """
    canonical = _lock.canonical_tracker(tracker)
    rebar_dir = _rebar_dir(canonical)
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
            LEG_ENRICH_DRAIN,
            lambda: _existence_report(
                LEG_ENRICH_DRAIN,
                os.path.join(rebar_dir, _DRAIN_LOCK_NAME),
                note="no drain process holds the enrichment drain lock",
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
