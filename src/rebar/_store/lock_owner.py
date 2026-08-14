"""Ownership stamps and staleness adjudication for rebar's stamped locks.

Split out of :mod:`rebar._store.lock` (bug larval-tribal-tigermoth), which had reached the
800-LOC module cap. The seam is the existing call graph, not an arbitrary cut: everything
here answers ONE question — "who owns this mkdir lock, and is that owner provably gone?" —
and :mod:`rebar._store.lock` keeps the acquisition machinery (the fcntl leg,
:func:`~rebar._store.lock._acquire_mkdir`, ``LockHandle``, ``acquire``/``write_lock``).
The dependency runs one way, ``lock`` -> ``lock_owner``; nothing here imports ``lock``.

The v2 stamp shape, the boot-id host identity, and the refusal-without-proof rule are
documented in :mod:`rebar._store.lock`'s module docstring, which remains the narrative home
for the dual-window lock contract. :func:`_stamp_is_stale` below carries the full
decision table; :func:`_mkdir_lock_is_stale` and :func:`stamped_file_is_stale` are the
two readers that feed it, for the mkdir lock dir and for a single-file lock respectively
(the latter added by bug knavish-stimulated-bluebottle for the enrichment drain lock).
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

# Wall-clock backstop for the refuse-without-proof branches of _mkdir_lock_is_stale (bug
# yaw-gravel-linen). Those branches correctly decline to reclaim a lock whose owner MIGHT be
# live, but with no upper bound in time an absent/foreign/malformed/unprobeable stamp wedges
# the store FOREVER — the CLASS behind the 2026-07-31 incident, of which castoff-tigerseye-
# ammonite fixed only one instance. Honour such a stamp until this ceiling, then reclaim, so
# the store can never wedge permanently (9305 rec #1). git gc's gc.pid uses 12h as its
# "very generous" bound (builtin/gc.c); rebar holds the write lock only for a single event
# append (seconds), so 1h is ~1000x margin over normal hold time yet bounds the wedge. This
# is a BACKSTOP, never a timer on its own: it is applied ONLY where there is no positive
# liveness signal that is actually PROOF — see _mkdir_lock_is_stale for the one branch where
# a live-pid probe is unqualified and therefore does not override it.
_MKDIR_LOCK_STALE_CEILING_S = 3600

# The v2 owner-stamp line prefix. Deliberately colon-free so an OLDER rebar parsing it with
# the legacy ``host, sep, pid = stamp.partition(":")`` finds no separator and declines to act,
# rather than mis-deriving "this host + a dead pid" (forward compatibility).
_STAMP_V2_PREFIX = "rebar-lock v2"
# Placeholder for a field this platform cannot supply (e.g. no /proc). Explicit so a
# reader can tell "unknown" apart from "missing/malformed" (bug castoff-tigerseye-ammonite).
_STAMP_UNKNOWN = "-"

# Ambient operation label for the v2 stamp (camerashy-erectable-frog). A holder that is
# OPTIONAL housekeeping — a compaction sweep — is safe to interrupt, but a blocked writer
# timing out on the store lock saw only a bare pid and could not tell the sweep apart from a
# mystery hang mid-write (whose safe response is the opposite: wait). This contextvar lets the
# sweep TAG its lock acquisitions descriptively: `operation_label(...)` sets it, `_owner_stamp`
# reads it and appends an `op=<label>` field. The carrier is a contextvar, not a threaded
# `acquire` parameter, because the label is read synchronously in the SAME process/thread that
# later calls `lock.acquire` (the sweep folds inline), so it is in scope at the acquisition —
# including the per-ticket `lock.acquire` inside `compact_txn._compact_locked`, which need not
# be modified for the label to reach the stamp. The label is DESCRIPTIVE ONLY: staleness and
# reclamation never read it.
_operation_label: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rebar_lock_operation_label", default=None
)

# Known interruptible operation labels → the remedy an operator can act on without a code
# search. The compaction sweep's own guarantee (compact_trigger.run_sweep) is that a skipped
# sweep is a no-op for correctness — the events stay live and the next trigger re-folds them —
# so terminating it loses no data. The phrasing "safe to interrupt" / "loses no data" is what a
# LockTimeout surfaces when the holder is labelled with one of these operations.
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
    """This kernel boot's id (``/proc/sys/kernel/random/boot_id``), stripped.

    Stable for the life of one booted kernel and shared by every container on it —
    which is exactly the identity the hostname failed to provide (a container runtime
    re-rolls the hostname on each recreate; bug castoff-tigerseye-ammonite). Returns
    ``None`` where the kernel does not expose it (macOS, non-Linux). Never raises."""
    try:
        with open(_BOOT_ID_PATH, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except (OSError, ValueError):  # never raise: identity is best-effort
        return None


def _read_pid_namespace_id() -> str | None:
    """Identifier for this process's pid namespace (the inode of ``/proc/self/ns/pid``).

    Two processes sharing this value can probe each other's pids; across different
    values ``os.kill(pid, 0)`` is meaningless (the number names a different process, or
    nothing). Returns ``None`` where unavailable. Never raises."""
    try:
        return str(os.stat(_PID_NS_PATH).st_ino)
    except (OSError, ValueError):  # never raise: identity is best-effort
        return None


def _process_start_time(pid: int) -> str | None:
    """*pid*'s start time (field 22 of ``/proc/<pid>/stat``), or ``None`` if unknown.

    Qualifies the pid probe: a pid number can be recycled, so "alive" alone does not
    mean the stamped owner is alive. The comm field (field 2) is parenthesised and may
    itself contain spaces and parens, so parsing starts after its LAST ``')'``; field 22
    is then the 20th whitespace-separated field of the remainder. Never raises."""
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
    """The identity of the *physical host* this process runs on.

    ``boot-<boot id>`` when the kernel exposes one, else ``name-<hostname>``. The boot
    id is what makes a container recreate recognisable as the same host (bug
    castoff-tigerseye-ammonite) while still distinguishing genuinely different hosts
    sharing a filesystem (bug yaw-gravel-linen). Guaranteed colon-free so the v2 stamp
    stays unparseable to a legacy reader."""
    boot_id = _read_boot_id()
    raw = f"boot-{boot_id}" if boot_id else f"name-{socket.gethostname()}"
    return raw.replace(":", "_")


def _owner_stamp() -> str:
    """Identity written into a freshly-acquired mkdir lock (v2, colon-free)::

        rebar-lock v2 host=<host-identity> ns=<pid-ns-id> pid=<pid> start=<start-time>

    Unknown ``ns``/``start`` are written as ``-``. The absence of any ``:`` is load
    bearing: an older rebar splits the stamp on ``:``, finds no separator, and refuses
    to reclaim — instead of decoding a bogus host/pid pair (bug
    castoff-tigerseye-ammonite).

    When an :func:`operation_label` context is active, an extra colon-free ``op=<label>``
    field is appended (camerashy-erectable-frog) so a blocked writer's timeout can name the
    holder — e.g. a compaction sweep — rather than only its pid. The field is optional and
    descriptive: an older reader ignores it (unknown fields are tolerated) and staleness logic
    never reads it."""
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
    """Liveness verdict for a stamp's ``pid``: ``live``, ``not-running``, or
    ``unprobeable`` — the distinction between "a process really is holding this" and "the
    stamp is stale".

    Probing is only meaningful under exactly the conditions :func:`_mkdir_lock_is_stale`
    already requires: the same :func:`_host_identity` AND a comparable pid namespace.
    Anywhere else the number names a different process, or nothing, so this reports
    ``unprobeable`` rather than a verdict — mirroring that function's refusal-without-proof
    rule (bug yaw-gravel-linen) so a message can never suggest a reclaim the lock itself
    would refuse. This is DESCRIPTIVE ONLY: nothing here feeds a reclamation decision."""
    if fields["host"] != _host_identity():
        return "unprobeable (foreign host)"
    stamped_ns = None if fields["ns"] == _STAMP_UNKNOWN else fields["ns"]
    if stamped_ns != _read_pid_namespace_id():
        return "unprobeable (other pid namespace)"
    try:
        pid = int(fields["pid"])
    except ValueError:
        return "unprobeable (malformed pid)"
    return "live" if _pid_alive(pid) else "not-running"


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


def _pid_alive(pid: int) -> bool:
    """Whether *pid* is a live process. ``os.kill(pid, 0)`` probes existence without
    signalling. A PermissionError means the pid exists but is owned by another user
    (alive); any other error is treated as alive (conservative — never reclaim on
    uncertainty)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _legacy_stamp_is_stale(stamp: str) -> bool:
    """Pre-v2 ``<hostname>:<pid>`` stamp: reclaimable only when the hostname matches
    ours AND the pid parses AND that pid is dead. Every malformed shape (empty, no
    colon, empty host, empty pid, non-numeric pid, extra colons, a torn mid-write read)
    and every foreign hostname returns False — never reclaim on anything short of proof
    (bug yaw-gravel-linen). Unchanged from the original implementation; kept as the
    fallback so locks stamped by an older rebar are still handled exactly as before."""
    host, sep, pid_s = stamp.partition(":")
    if not sep or host != socket.gethostname():
        return False
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    return not _pid_alive(pid)


def _mkdir_lock_is_stale(lock_dir: str, *, fcntl_held: bool = False) -> bool:
    """Whether a held mkdir lock is provably orphaned and may be reclaimed.

    Reads the stamp out of *lock_dir*'s owner file and hands it to
    :func:`_stamp_is_stale`, which carries the decision table. An unreadable owner file
    is table row 1 (refuse, subject to the ceiling). ``unrecognised_via_ceiling=False``
    keeps this leg's legacy routing: a non-v2 stamp is adjudicated by
    :func:`_legacy_stamp_is_stale`, because the mkdir lock genuinely has a pre-v2
    ``<host>:<pid>`` dialect still in the wild.

    Set *fcntl_held* only when the caller already holds the exclusive ``fcntl.flock``
    leg for this same tracker — see :func:`_acquire_mkdir` for why that is proof rather
    than a hint.
    """
    try:
        with open(os.path.join(lock_dir, _MKDIR_OWNER_FILE), encoding="utf-8") as fh:
            stamp = fh.read().strip()
    except OSError:
        return _mkdir_lock_age_exceeds_ceiling(lock_dir)
    return _stamp_is_stale(stamp, lock_dir, fcntl_held=fcntl_held, unrecognised_via_ceiling=False)


def stamped_file_is_stale(path: str) -> bool:
    """Whether a stamped single-FILE lock at *path* is provably orphaned.

    The file-shaped sibling of :func:`_mkdir_lock_is_stale`, for locks whose ownership
    stamp is the file's own contents rather than a separate owner file inside a lock dir
    — today ``.rebar/enrich-drain.lock`` (bug knavish-stimulated-bluebottle). It answers
    with the SAME decision table, so the pid-recycle qualification, the
    refuse-without-proof branches and the wall-clock ceiling are inherited rather than
    forked into a second heuristic.

    Two arguments are fixed for this shape. ``fcntl_held=False``: a file lock has no
    kernel leg to hold, so there is never that proof. ``unrecognised_via_ceiling=True``:
    unlike the mkdir lock, this shape has no legacy ``<host>:<pid>`` dialect — it was
    unstamped entirely before the fix — so unrecognised content means "an orphan from
    before stamping", and routing it through the ceiling is what lets an already-leaked
    lock be reclaimed instead of wedging forever. A vanished file reports not-stale; the
    caller simply retries the create.
    """
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
    """Whether the holder named by *stamp* is provably gone. THE staleness authority.

    *artifact_path* is the lock artifact (dir or file) whose mtime measures the hold age
    for the ceiling; *fcntl_held* is the caller's exclusive-fcntl-leg proof (see
    :func:`_mkdir_lock_is_stale`); *unrecognised_via_ceiling* selects the routing for a
    stamp that is not v2, and is the ONLY thing it selects. The decision table:

    1. Owner file absent or unreadable → False (a bash-style lock, or one seen in the
       window between ``mkdir`` and the stamp write) — UNLESS the dir has out-aged
       :data:`_MKDIR_LOCK_STALE_CEILING_S`, the wall-clock backstop that stops an
       unprovable stamp wedging the store forever (9305 rec #1). Handled by the callers
       above, which cannot produce a stamp to pass here.
    2. Not a v2 stamp → with *unrecognised_via_ceiling* False, exactly the legacy
       behaviour (:func:`_legacy_stamp_is_stale`); with it True, no proof of ownership,
       so the age ceiling decides (the row-1 treatment).
    3. v2 stamp with missing/malformed fields → False, or reclaim past the age ceiling.
    4. v2 stamp from a different :func:`_host_identity` → the genuine foreign-host /
       shared-filesystem case: we cannot observe that host's processes and our own locks
       prove nothing about its kernel, so no pid, no namespace and no ``fcntl_held`` can
       license a reclaim (bug yaw-gravel-linen) — refused until the age ceiling, then
       reclaimed so a shared-filesystem orphan cannot wedge forever.
    5. Same host and the pid namespaces are comparable (stamped ``ns`` equals ours,
       including both being unknown) → probe the pid, QUALIFIED BY START TIME. Dead pid
       ⇒ stale. Live pid whose start time is known on both sides and differs ⇒ the
       number was recycled by an unrelated process and the true owner is gone ⇒ stale.
       Live pid whose start time is CORROBORATED (both known and equal) ⇒ False, and the
       ceiling does NOT apply: that is a positive liveness signal and breaking a lock on a
       timer alone over a live owner is forbidden ("never on a timer alone"). Live pid whose
       current start time is UNAVAILABLE (no ``/proc``: every macOS probe) ⇒ the verdict is
       unqualified — a bare pid-NUMBER match, not proof of the stamped owner — so this is a
       refuse-without-proof branch and carries the ceiling like the others (bug
       larval-tribal-tigermoth; before it, that case wedged the store with NO bound at all).
    6. Same host, namespaces NOT comparable (different, or exactly one unknown) — the
       container-recreate case, where the stamped pid is not probeable at all → stale
       iff *fcntl_held*, else refused until the age ceiling.

    Non-numeric pid on an otherwise same-host/same-namespace stamp is likewise a
    no-liveness-signal refusal and reclaims past the ceiling.
    """

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
        # Same host, different (or unknowable) pid namespace: the stamped pid number is
        # meaningless to us, so the fcntl proof is the only positive evidence — and, short
        # of it, the age ceiling backstops the wedge.
        return fcntl_held or _mkdir_lock_age_exceeds_ceiling(artifact_path)

    try:
        pid = int(fields["pid"])
    except ValueError:
        return _mkdir_lock_age_exceeds_ceiling(artifact_path)
    if not _pid_alive(pid):
        return True
    stamped_start = None if fields["start"] == _STAMP_UNKNOWN else fields["start"]
    current_start = _process_start_time(pid)
    if current_start is None:
        # UNQUALIFIED live-pid verdict (bug larval-tribal-tigermoth). `_process_start_time`
        # reads /proc, so on a platform without it (macOS) it ALWAYS returns None — the
        # recycled-pid discriminator below is then unsatisfiable BY CONSTRUCTION and this
        # branch used to `return False` forever. "Live pid" there is not proof the stamped
        # owner lives; it is a bare pid-NUMBER match, and pids recycle (kern.maxproc 16000).
        # Without a bound that wedges the store PERMANENTLY. So this is a refuse-without-proof
        # branch like every other, and takes the same ceiling backstop.
        #
        # This cannot break a genuinely-live owner. `_mkdir_lock_is_stale` is only reached
        # from `_acquire_mkdir`, which by its stated precondition runs only AFTER this
        # acquirer took the exclusive fcntl leg — held for the ENTIRE lifetime of the mkdir
        # leg. A live owner inside its own hold would still hold that fcntl leg, so we could
        # not have acquired it and could not be here. Arriving here is itself evidence the
        # stamped owner let go: the ceiling only ever fires on an orphan.
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
