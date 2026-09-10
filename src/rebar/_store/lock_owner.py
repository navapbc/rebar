"""Determine whether stamped lock owners are provably gone.

This module owns v2 and legacy stamp parsing, host and PID identity, and conservative
staleness decisions. :mod:`rebar._store.lock` owns acquisition and imports this module.
This module never imports ``lock``. :func:`_stamp_is_stale` serves mkdir and stamped file
locks.
"""

from __future__ import annotations

import contextvars
import logging
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Ownership stamp written inside the mkdir lock dir so a future acquirer can detect
# (and reclaim) a lock orphaned by a process that died before releasing it. Lives
# INSIDE .ticket-write.lock.d/, which is gitignored, so it never surfaces untracked.
_MKDIR_OWNER_FILE = "owner"

# One-hour backstop for decisions without proof of liveness. Rebar normally holds this lock
# for seconds. Positive liveness always overrides the ceiling.
_MKDIR_LOCK_STALE_CEILING_S = 3600

# Colon-free v2 prefix that makes legacy ``partition(":")`` readers refuse the stamp.
_STAMP_V2_PREFIX = "rebar-lock v2"
# Placeholder for a field this platform cannot supply (e.g. no /proc). Explicit so a
# reader can tell "unknown" apart from "missing/malformed" (bug castoff-tigerseye-ammonite).
_STAMP_UNKNOWN = "-"

# Ambient descriptive ``op=<label>`` stamp field. A context variable reaches nested compaction
# acquisitions without changing acquisition APIs. Reclamation never reads the label.
_operation_label: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rebar_lock_operation_label", default=None
)

# Operator remedies for optional work whose interruption preserves events.
_INTERRUPTIBLE_REMEDIES: dict[str, str] = {
    "compact-sweep": (
        "this holder is a compaction sweep (optional housekeeping) and is safe to interrupt: "
        "terminating it loses no data — the events stay live and the next trigger re-folds them"
    ),
}


def _sanitize_op_label(label: str) -> str:
    """Reduce *label* to a single colon-free ``key=value`` token value.

    The v2 stamp line must stay colon-free (an older rebar's legacy ``partition(":")`` parse
    relies on it) and each field is one whitespace-delimited ``key=value`` token, so any ``:``,
    ``=`` or whitespace in the raw label would break the format — each is replaced with ``-``.
    """
    return "".join("-" if (ch in ":=" or ch.isspace()) else ch for ch in label)


@contextmanager
def operation_label(label: str) -> Iterator[None]:
    """Tag lock acquisitions made in this context with an ``op=<label>`` stamp field.

    Ambient and descriptive: set on enter, reset on exit, so an ordinary writer outside the
    context stamps exactly as before. Used by the compaction sweep to name itself in the
    ownership stamp a blocked writer's :class:`LockTimeout` renders."""
    token = _operation_label.set(_sanitize_op_label(label) if label else None)
    try:
        yield
    finally:
        _operation_label.reset(token)


def interruptible_remedy(holder: str) -> str | None:
    """The safe-to-interrupt remedy text if *holder* names a known interruptible operation.

    *holder* is a rendered holder description (from :func:`describe_lock_holder`); this reads
    its ``op=<label>`` token, if any, and returns the mapped remedy — else ``None``. Descriptive
    only: it never influences reclamation."""
    for token in holder.split():
        key, sep, value = token.partition("=")
        if sep and key == "op":
            return _INTERRUPTIBLE_REMEDIES.get(value)
    return None


_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_PID_NS_PATH = "/proc/self/ns/pid"


def _read_boot_id() -> str | None:
    """Return the current kernel boot ID, or ``None`` when unavailable.

    The value is shared by containers on one boot. This function never raises."""
    try:
        with open(_BOOT_ID_PATH, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except (OSError, ValueError):  # never raise: identity is best-effort
        return None


def _read_pid_namespace_id() -> str | None:
    """Return this process's PID namespace inode, or ``None`` when unavailable.

    Matching identifiers permit meaningful PID probes. This function never raises."""
    try:
        return str(os.stat(_PID_NS_PATH).st_ino)
    except (OSError, ValueError):  # never raise: identity is best-effort
        return None


def _process_start_time(pid: int) -> str | None:
    """Return Linux stat field 22 for *pid*, or ``None`` when unavailable.

    The value distinguishes recycled PIDs. Parsing starts after the final ``)`` because the
    command field may contain spaces or parentheses. This function never raises."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except (OSError, ValueError):  # never raise: identity is best-effort
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 1 :].split()
    # fields[0] is stat field 3 (state), so stat field 22 is fields[19].
    if len(fields) < 20:
        return None
    return fields[19] or None


def _host_identity() -> str:
    """Return a colon-free host identity for the v2 stamp.

    A boot ID identifies container recreations on one kernel. The hostname is the fallback
    when no boot ID is available."""
    boot_id = _read_boot_id()
    raw = f"boot-{boot_id}" if boot_id else f"name-{socket.gethostname()}"
    return raw.replace(":", "_")


def _owner_stamp() -> str:
    """Build a colon-free v2 ownership stamp.

    Format::

        rebar-lock v2 host=<host-identity> ns=<pid-ns-id> pid=<pid> start=<start-time>

    Unknown namespace or start values use ``-``. Legacy readers refuse this colon-free form.
    An active :func:`operation_label` adds an optional descriptive field that staleness logic
    ignores."""
    pid = os.getpid()
    ns = _read_pid_namespace_id() or _STAMP_UNKNOWN
    start = _process_start_time(pid) or _STAMP_UNKNOWN
    stamp = f"{_STAMP_V2_PREFIX} host={_host_identity()} ns={ns} pid={pid} start={start}"
    label = _operation_label.get()
    if label:
        stamp += f" op={label}"
    return stamp


def _parse_v2_stamp(stamp: str) -> dict[str, str] | None:
    """Parse a v2 owner stamp into its fields, or ``None`` if *stamp* is not v2.

    A v2 stamp missing any required field (a torn mid-write read, say) parses to an
    empty mapping — distinguishable from ``None`` so the caller refuses rather than
    falling back to the legacy colon parse."""
    if not stamp.startswith(_STAMP_V2_PREFIX):
        return None
    fields: dict[str, str] = {}
    for token in stamp[len(_STAMP_V2_PREFIX) :].split():
        key, sep, value = token.partition("=")
        if sep and key and value:
            fields[key] = value
    if not {"host", "ns", "pid", "start"} <= fields.keys():
        return {}
    return fields


def _describe_stamped_pid(fields: dict[str, str]) -> str:
    """Liveness verdict for a stamp's ``pid``.

    ``live`` is reserved for a pid whose current process start time corroborates the
    stamp. A bare pid-number hit is not proof of ownership: pids recycle, and on
    platforms without ``/proc`` the start-time discriminator is unavailable. Those cases
    are reported as unverified/not-owner rather than as a live holder. This is
    DESCRIPTIVE ONLY for the write lock; staging cleanup also consumes the string and
    treats only ``not-running`` / ``not-owner`` as positive abandonment proof.
    """
    if fields["host"] != _host_identity():
        return "unprobeable (foreign host)"
    stamped_ns = None if fields["ns"] == _STAMP_UNKNOWN else fields["ns"]
    if stamped_ns != _read_pid_namespace_id():
        return "unprobeable (other pid namespace)"
    try:
        pid = int(fields["pid"])
    except ValueError:
        return "unprobeable (malformed pid)"
    if not _pid_alive(pid):
        return "not-running"
    if pid == os.getpid():
        return "live"
    stamped_start = None if fields["start"] == _STAMP_UNKNOWN else fields["start"]
    current_start = _process_start_time(pid)
    if current_start is None:
        return "unverified-live (start unknown)"
    if stamped_start is None:
        return "unverified-live (stamp start unknown)"
    if stamped_start != current_start:
        return "not-owner (recycled pid)"
    return "live"


def _mkdir_lock_age_s(lock_dir: str) -> float | None:
    """Wall-clock age of the lock artifact at *lock_dir* in seconds, or ``None`` if it
    cannot be stat'd. Named for its first caller; ``os.stat`` is indifferent to whether
    the artifact is a directory (the mkdir leg) or a file (a stamped single-file lock).

    The ownership stamp is written once at acquisition and never refreshed (there is no
    heartbeat), so the lock dir's mtime is the acquisition time — the quantity the stale
    ceiling is measured against."""
    try:
        return time.time() - os.stat(lock_dir).st_mtime
    except OSError:
        return None


def _mkdir_lock_age_exceeds_ceiling(lock_dir: str) -> bool:
    """Whether the lock artifact at *lock_dir* is older than
    :data:`_MKDIR_LOCK_STALE_CEILING_S`. Dir or file alike, as above.

    Fail-closed: an unreadable mtime returns False (keep refusing), so a stat error can
    never itself license a reclaim. Applied ONLY on refuse-without-proof branches that
    carry no positive liveness signal — never to override a live-pid probe."""
    age = _mkdir_lock_age_s(lock_dir)
    return age is not None and age > _MKDIR_LOCK_STALE_CEILING_S


def _signal_zero_probes_liveness() -> bool:
    """Whether ``os.kill(pid, 0)`` PROBES a process here rather than killing it.

    True on POSIX, where signal 0 is the documented existence check. FALSE on Windows:
    ``os.kill`` there only understands ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT``, and for any
    other value — 0 included — it opens the process and calls ``TerminateProcess``. A
    liveness probe would therefore KILL the very holder it was asking about, and since
    this probe runs on mkdir-lock contention it would kill a live writer mid-write (story
    ``friendless-alabaster-cub``). A function, not a constant, so the platform seam is
    drivable in a test on either host."""
    return os.name == "posix"


def _pid_alive(pid: int) -> bool:
    """Whether *pid* is a live process. ``os.kill(pid, 0)`` probes existence without
    signalling. A PermissionError means the pid exists but is owned by another user
    (alive); any other error is treated as alive (conservative — never reclaim on
    uncertainty).

    Where signal 0 is not a probe (:func:`_signal_zero_probes_liveness`), there is no
    liveness SIGNAL at all, so the answer is the conservative one — "alive" — and the
    decision falls through to the branches that need positive proof: the caller's held
    exclusive leg, or the wall-clock age ceiling. That NARROWS reclamation rather than
    weakening it: nothing is ever reclaimed on less evidence than before, a dead owner is
    still reclaimed via the exclusive-leg proof in :func:`_stamp_is_stale`, and the
    ceiling still stops an unprovable stamp wedging the store forever."""
    if not _signal_zero_probes_liveness():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _legacy_stamp_is_stale(stamp: str) -> bool:
    """Return whether a legacy ``<hostname>:<pid>`` stamp proves its owner dead.

    Reclamation requires a matching hostname, numeric PID, and failed liveness probe.
    Malformed or foreign stamps remain held."""
    host, sep, pid_s = stamp.partition(":")
    if not sep or host != socket.gethostname():
        return False
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    return not _pid_alive(pid)


def _mkdir_lock_is_stale(lock_dir: str, *, fcntl_held: bool = False) -> bool:
    """Return whether a mkdir lock is provably orphaned.

    Unreadable owner files use the age ceiling. Non-v2 stamps use legacy adjudication.
    *fcntl_held* means the caller holds this tracker's platform-exclusive kernel leg, which
    proves that a same-host prior owner is gone."""
    try:
        with open(os.path.join(lock_dir, _MKDIR_OWNER_FILE), encoding="utf-8") as fh:
            stamp = fh.read().strip()
    except OSError:
        return _mkdir_lock_age_exceeds_ceiling(lock_dir)
    return _stamp_is_stale(stamp, lock_dir, fcntl_held=fcntl_held, unrecognised_via_ceiling=False)


def stamped_file_is_stale(path: str) -> bool:
    """Return whether a stamped single-file lock is provably orphaned.

    This applies the shared decision table to ``.rebar/enrich-drain.lock``. The file has no
    kernel leg or legacy stamp dialect, so unreadable or unrecognized content uses the age
    ceiling. A vanished file reports not stale."""
    try:
        with open(path, encoding="utf-8") as fh:
            stamp = fh.read().strip()
    except OSError:
        return _mkdir_lock_age_exceeds_ceiling(path)
    return _stamp_is_stale(stamp, path, fcntl_held=False, unrecognised_via_ceiling=True)


def _stamp_is_stale(
    stamp: str,
    artifact_path: str,
    *,
    fcntl_held: bool,
    unrecognised_via_ceiling: bool,
) -> bool:
    """Return whether *stamp* proves that its holder is gone.

    Callers apply the age ceiling when no stamp can be read. Unrecognized stamps use either
    legacy adjudication or the ceiling according to *unrecognised_via_ceiling*. Invalid v2 and
    foreign-host stamps use the ceiling. A different PID namespace requires *fcntl_held* or
    the ceiling. Within a comparable namespace, a dead PID, a different PID under the held
    kernel leg, or a mismatched known start time proves staleness. An unavailable start time
    uses the ceiling. A corroborated start time remains held regardless of age."""

    fields = _parse_v2_stamp(stamp)
    if fields is None:
        if unrecognised_via_ceiling:
            return _mkdir_lock_age_exceeds_ceiling(artifact_path)
        return _legacy_stamp_is_stale(stamp)
    if not fields:
        return _mkdir_lock_age_exceeds_ceiling(artifact_path)

    if fields["host"] != _host_identity():
        return _mkdir_lock_age_exceeds_ceiling(artifact_path)

    stamped_ns = None if fields["ns"] == _STAMP_UNKNOWN else fields["ns"]
    if stamped_ns != _read_pid_namespace_id():
        # Another PID namespace is not probeable. The held kernel leg proves abandonment on
        # this host. Otherwise only the age ceiling permits reclamation.
        return fcntl_held or _mkdir_lock_age_exceeds_ceiling(artifact_path)

    try:
        pid = int(fields["pid"])
    except ValueError:
        return _mkdir_lock_age_exceeds_ceiling(artifact_path)
    if not _pid_alive(pid):
        return True
    stamped_start = None if fields["start"] == _STAMP_UNKNOWN else fields["start"]
    current_start = _process_start_time(pid)
    if fcntl_held and pid != os.getpid():
        # Holding the kernel leg proves a different process no longer owns this lock. The
        # same-process case stays conservative because POSIX locks are process scoped.
        return True
    if current_start is None:
        # A running PID without a start time does not prove ownership, so the ceiling applies.
        return _mkdir_lock_age_exceeds_ceiling(artifact_path)
    if stamped_start is not None and stamped_start != current_start:
        # The pid is live but it is a DIFFERENT process wearing a recycled number.
        return True
    # A live pid whose start time CORROBORATES the stamp: a real owner, never broken on a
    # timer alone. Also the stamped-unknown/current-known case, left refusing as before.
    return False


def _reclaim_mkdir_lock(lock_dir: str) -> None:
    """Remove a provably-stale (or aged-out) mkdir lock (owner stamp + dir). Best-effort:
    a failure just leaves the next acquirer to wait/retry — never a correctness hazard.

    Reclaiming is noteworthy — it breaks another acquirer's lock — so disclose the holder
    stamp and the dir age at WARNING before removing (research rec #8, "report the
    holder"), turning a silent wedge into an attributable event. This runs on the reclaim
    path only, not the poll hot loop, so it does not spam."""
    stamp = "<unreadable>"
    try:
        with open(os.path.join(lock_dir, _MKDIR_OWNER_FILE), encoding="utf-8") as fh:
            stamp = fh.read().strip() or "<empty>"
    except OSError:
        pass
    age = _mkdir_lock_age_s(lock_dir)
    age_s = f"{age:.0f}s" if age is not None else "unknown"
    logger.warning("reclaiming stale write lock %s: holder=%r age=%s", lock_dir, stamp, age_s)
    try:
        os.remove(os.path.join(lock_dir, _MKDIR_OWNER_FILE))
    except OSError:
        pass
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass
