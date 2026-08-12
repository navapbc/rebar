"""The ONE write lock for the tickets store.

Unifies the three historical lock acquisitions — bash ``_flock_stage_commit``
(``ticket-lib.sh``), ``ticket_txn.py``, and ``event_append.write_lock`` — into a
single implementation so the whole system holds ONE lock (invariant I5).

**The dual-window contract (permanent).** By default the lock acquires BOTH
mechanisms in a fixed order — ``fcntl.flock(LOCK_EX)`` on ``.ticket-write.lock``
FIRST, then the mkdir lock ``.ticket-write.lock.d`` — releasing in reverse (mkdir
leg in a ``finally``). This is a **standing, intentional contract**, not migration
residue: the fcntl leg is the fast kernel-backed lock (auto-released if the holder
dies) and the mkdir leg is the **portable second window** — ``mkdir`` is atomic on
POSIX, so mutual exclusion holds even where util-linux ``flock`` is absent (default
macOS), and the mkdir stamp is what backs the foreign-host / shared-filesystem
reclamation logic (``_mkdir_lock_is_stale``). Because neither leg waits on the other
across processes there is no hold-and-wait cycle ⇒ deadlock-free. The dual acquisition
is also what closed the historical ``stiff-mop-lane`` gap, and it remains the durable
mechanism the writer-storm gate depends on (``the mkdir leg is always taken``).

``dual_window=True`` is the permanent default. ``dual_window=False`` remains an
fcntl-only escape hatch for callers that deliberately want the single kernel leg
(e.g. a caller certain of a local ``flock``-capable filesystem); it is an opt-out,
not a migration end-state.

**Host identity is the boot id, not the hostname (bug castoff-tigerseye-ammonite).**
The mkdir ownership stamp used to be ``<hostname>:<pid>``, and reclamation required
``host == socket.gethostname()``. Inside a container ``socket.gethostname()`` is the
*container id*, which the container runtime re-rolls on every recreate — so a lock
orphaned by the SAME physical host's previous container looked like a foreign-host
(shared-filesystem) owner and was refused forever, with no age ceiling. Every later
boot then burned the full lock budget. The stamp is now a colon-free **v2** line::

    rebar-lock v2 host=<host-identity> ns=<pid-ns-id> pid=<pid> start=<start-time>

``host`` is ``boot-<contents of /proc/sys/kernel/random/boot_id>`` when the kernel
exposes one (stable across container recreates on one boot, distinct across real
hosts) and ``name-<hostname>`` otherwise (macOS and other non-Linux kernels, where
the hostname is the best available identity and containers are not the failure mode).
``ns`` is the pid-namespace inode, ``pid``/``start`` identify the owning process so a
recycled pid number cannot be mistaken for a live owner. The line deliberately
contains **no colon**: an older rebar parsing it with the legacy
``host, sep, pid = stamp.partition(":")`` finds no separator and declines to act,
rather than mis-deriving "this host + a dead pid" (forward compatibility).

That stamp is also what a timed-out waiter REPORTS: :class:`LockTimeout` renders it via
:func:`describe_lock_holder`, so ``flock: could not acquire lock after 60s`` now names
the host, pid, start time, hold age and pid liveness of whoever is holding the lock (bug
7084 — before this, a starved writer could only infer the holder from the tracker's
commit timeline). Reporting reuses the EXISTING v2 fields; it adds none, and it never
influences reclamation.

Refusal-without-proof (bug ``yaw-gravel-linen``) is unchanged and deliberate: a stamp
from a *different* host identity is still never reclaimed. See
:func:`_mkdir_lock_is_stale` for the full decision table and :func:`_acquire_mkdir`
for why the held fcntl leg is positive proof of a dead same-host owner.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

from rebar._store.compat import check_store_compat
from rebar._store.gitutil import _backoff_sleep, _jitter

logger = logging.getLogger(__name__)

WRITE_LOCK_NAME = ".ticket-write.lock"
MKDIR_LOCK_NAME = ".ticket-write.lock.d"
# Ownership stamp written inside the mkdir lock dir so a future acquirer can detect
# (and reclaim) a lock orphaned by a process that died before releasing it. Lives
# INSIDE .ticket-write.lock.d/, which is gitignored, so it never surfaces untracked.
_MKDIR_OWNER_FILE = "owner"

# Bash parity: FLOCK_STAGE_COMMIT_TIMEOUT (default 30s) per attempt × max_retries(2).
_DEFAULT_TIMEOUT = 30
_DEFAULT_ATTEMPTS = 2

# Bounded retry PASSES for an expired budget (bug royal-weariless-zebrafish). ``attempts``
# above is NOT a retry loop — it only multiplies ONE deadline — so a write whose budget expired
# was DISCARDED. Default 0 keeps every existing caller bit-for-bit unchanged; the write path
# opts in via :func:`write_path_retries`. Measured holders of 88s/103s/163s all exceed the 60s
# budget yet sit inside 3 passes. NOT given to compaction/fsck/doctor: retrying there would undo
# 7084's stand-aside and re-create the long holder that causes this contention.
_DEFAULT_RETRIES = 0
_WRITE_PATH_RETRIES = 2
_MAX_RETRIES = 10
_RETRY_BACKOFF_BASE_S = 0.5
_RETRY_BACKOFF_CAP_S = 2.0

# Wall-clock backstop for the refuse-without-proof branches of _mkdir_lock_is_stale (bug
# yaw-gravel-linen). Those branches correctly decline to reclaim a lock whose owner MIGHT be
# live, but with no upper bound in time an absent/foreign/malformed/unprobeable stamp wedges
# the store FOREVER — the CLASS behind the 2026-07-31 incident, of which castoff-tigerseye-
# ammonite fixed only one instance. Honour such a stamp until this ceiling, then reclaim, so
# the store can never wedge permanently (9305 rec #1). git gc's gc.pid uses 12h as its
# "very generous" bound (builtin/gc.c); rebar holds the write lock only for a single event
# append (seconds), so 1h is ~1000x margin over normal hold time yet bounds the wedge. This
# is a BACKSTOP, never a timer on its own: it is applied ONLY where there is no positive
# liveness signal — a live-pid probe is never overridden by age ("never on a timer alone").
_MKDIR_LOCK_STALE_CEILING_S = 3600


# Exceptions carry (returncode, full stderr text); callers surface the message
# exactly once — mirroring the subprocess path where bash printed to stderr and the
# seam captured it into CommandError. We do NOT print here to avoid double-emit.


class LockTimeout(Exception):
    """Could not acquire the write lock within the budget (bash exit 1, stderr
    ``flock: could not acquire lock after Ns``).

    The message names the HOLDER when one can be read (bug 7084)::

        flock: could not acquire lock after 60s
            (holder: host=name-h pid=4321 start=93117 held=48s pid=live)

    That suffix is :func:`describe_lock_holder`'s rendering of the ownership stamp the
    mkdir leg ALREADY writes — no new machinery, and no new fields. Before it, a starved
    writer could not say what had blocked it: the 48s ``compact-on-close`` holder on 7084
    was identified afterwards by inferring from the tracker's commit timeline. The
    bash-parity prefix is unchanged, so anything keyed on it still matches, and a stamp
    that is absent / unreadable / in a shape the reader declines yields an explicit
    ``unknown (<why>)`` rather than a guess or a half-filled line."""

    returncode = 1

    def __init__(self, total_wait: int, holder: str | None = None) -> None:
        self.total_wait = total_wait
        self.holder = holder
        message = f"flock: could not acquire lock after {total_wait}s"
        if holder:
            message += f" (holder: {holder})"
        super().__init__(message)


class RebaseGuard(Exception):
    """Tracker is mid rebase/merge — refuse the write (bug 637b, bash exit 75). The
    message is the exact 3-line bash stderr."""

    returncode = 75

    def __init__(self, kind: str, tracker: str) -> None:
        self.kind = kind
        self.tracker = tracker
        super().__init__(
            f"Error: ticket write blocked — tracker is in {kind} recovery state.\n"
            f"  tracker: {tracker}\n"
            f'  Run: rebar fsck-recover --tracker-dir "{tracker}" '
            "(or ticket-fsck-recover.sh from the rebar engine dir)"
        )


def canonical_tracker(tracker: str | os.PathLike) -> str:
    """Resolve to a canonical path (bash ``cd "$1" && pwd -P``) so symlinked and
    real-path callers contend on the SAME lock file."""
    return os.path.realpath(str(tracker))


def _gitdir(tracker: str) -> str | None:
    """Resolve the tracker's git directory (handles the linked-worktree ``.git``
    file), mirroring ``_check_no_rebase_in_progress``."""
    git_path = os.path.join(tracker, ".git")
    if os.path.isfile(git_path):
        try:
            with open(git_path, encoding="utf-8") as f:
                line = f.read().strip()
        except OSError:
            return None
        gd = line[len("gitdir:") :].strip() if line.startswith("gitdir:") else ""
        if gd and not os.path.isabs(gd):
            gd = os.path.join(tracker, gd)
        return gd or None
    if os.path.isdir(git_path):
        return git_path
    return None


def check_no_rebase_in_progress(tracker: str) -> None:
    """Raise :class:`RebaseGuard` (exit 75) if the tracker is in a rebase/merge
    recovery state — committing then would silently abandon pending picks (637b).
    Emits the exact bash stderr. A gitdir that cannot be resolved does NOT block
    (the downstream git command surfaces its own error), matching bash."""
    gitdir = _gitdir(tracker)
    if gitdir is None:
        return
    kind = ""
    if os.path.isdir(os.path.join(gitdir, "rebase-merge")):
        kind = "rebase-merge"
    elif os.path.isdir(os.path.join(gitdir, "rebase-apply")):
        kind = "rebase-apply"
    elif os.path.isfile(os.path.join(gitdir, "REBASE_HEAD")):
        kind = "REBASE_HEAD"
    elif os.path.isfile(os.path.join(gitdir, "MERGE_HEAD")):
        kind = "MERGE_HEAD"
    if kind:
        raise RebaseGuard(kind, tracker)


def _acquire_fcntl(lock_path: str, deadline: float) -> int:
    """Poll ``fcntl.flock(LOCK_EX|LOCK_NB)`` until acquired or ``deadline``. Returns
    the held fd (caller closes to release). Raises :class:`LockTimeout`-signal via
    returning -1 on timeout (caller maps to the right total_wait)."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            # Only genuine contention (the lock is held elsewhere) is waited out; any other
            # errno (ENOLCK/EIO/EBADF/…) is a real fault that must surface with its identity
            # rather than be masked as a spurious 30-60s LockTimeout. (EINTR does not reach
            # here: PEP 475 retries the interrupted syscall at the C level.)
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                return -1
            # Jittered so many competing acquirers de-synchronize (9305 research rec #4).
            time.sleep(_jitter(0.05))


_STAMP_V2_PREFIX = "rebar-lock v2"
# Placeholder for a field this platform cannot supply (e.g. no /proc). Explicit so a
# reader can tell "unknown" apart from "missing/malformed" (bug castoff-tigerseye-ammonite).
_STAMP_UNKNOWN = "-"

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
    castoff-tigerseye-ammonite)."""
    pid = os.getpid()
    ns = _read_pid_namespace_id() or _STAMP_UNKNOWN
    start = _process_start_time(pid) or _STAMP_UNKNOWN
    return f"{_STAMP_V2_PREFIX} host={_host_identity()} ns={ns} pid={pid} start={start}"


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


def write_lock_is_busy(tracker: str | os.PathLike) -> bool:
    """Whether *tracker*'s write lock is held RIGHT NOW, answered without waiting (bug
    7084 / remediation R3).

    A single immediate probe of the SAME two legs :func:`acquire` takes, in the SAME
    order: ``fcntl.flock(LOCK_EX|LOCK_NB)`` on ``.ticket-write.lock``, then ``mkdir`` on
    ``.ticket-write.lock.d``. No new mechanism — this is what ``_acquire_fcntl`` /
    ``_acquire_mkdir`` already poll, with a zero deadline. Both legs must be probed: the
    mkdir leg is the portable window a bash-era or ``flock``-less writer holds, so an
    fcntl-only probe would report a busy store as free. The fcntl leg is released whether
    or not the mkdir leg is taken (the fd is closed in a ``finally``), and a mkdir probe
    that succeeds removes the dir again immediately — this function NEVER leaves a lock
    behind and NEVER returns holding anything.

    Deliberately ADVISORY and inherently racy: the answer describes an instant, and a
    holder can arrive one microsecond later. It is only ever used to let an OPTIONAL
    piece of work stand aside (compact-on-close), never to decide whether a mutation is
    safe — mutual exclusion still comes from :func:`acquire` alone. Fails OPEN: any
    unexpected error reports "not busy", so a probe failure degrades to today's
    behaviour (attempt the work and let the real acquire arbitrate) rather than
    suppressing work on a store that is actually free."""
    tracker = canonical_tracker(tracker)
    lock_path = os.path.join(tracker, WRITE_LOCK_NAME)
    lock_dir = os.path.join(tracker, MKDIR_LOCK_NAME)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # Genuine contention answers the question; any other errno is a real fault
            # that must not masquerade as a busy lock (it would silently suppress work).
            return exc.errno in (errno.EAGAIN, errno.EACCES)
        try:
            os.mkdir(lock_dir)
        except FileExistsError:
            return True
        except OSError as exc:
            return exc.errno == errno.EEXIST
        try:
            os.rmdir(lock_dir)  # taken only to answer the probe — give it straight back
        except OSError:
            pass
        return False
    finally:
        os.close(fd)  # closing the fd releases the fcntl leg


def describe_lock_holder(tracker: str | os.PathLike) -> str:
    """Describe whoever currently holds *tracker*'s write lock, for a
    :class:`LockTimeout` message (bug 7084 / remediation R5).

    Reads the ownership stamp the mkdir leg ALREADY writes (:func:`_owner_stamp`) and
    renders its existing fields — ``host``, ``pid``, ``start`` — plus the lock dir's age
    and a liveness verdict for the stamped pid. Together those answer the two questions a
    starved writer actually has: WHICH process is holding it, and is that process still
    alive (a live holder ⇒ wait or investigate that operation; a dead one ⇒ a stale stamp
    the reclamation path will clear). No new stamp fields and no new mechanism: this only
    surfaces what was already recorded and then thrown away.

    ALWAYS returns a phrase and NEVER raises — a failure to name the holder must not
    become a second failure on top of the timeout being reported, and the lock dir can
    vanish mid-read at any moment (the holder released). Every case the stamp cannot
    answer is stated plainly as ``unknown (<why>)`` rather than guessed at or emitted as a
    partial line:

    * no lock dir / no owner file / unreadable → the fcntl-only (``dual_window=False``)
      holder, a bash-era lock, or the window between ``mkdir`` and the stamp write;
    * a shape :func:`_parse_v2_stamp` declines (the forward-compat path a NEWER rebar's
      stamp would take here) → ``unrecognised``;
    * a v2 stamp missing required fields (a torn mid-write read) → ``incomplete``.
    """
    try:
        lock_dir = os.path.join(canonical_tracker(tracker), MKDIR_LOCK_NAME)
        try:
            with open(os.path.join(lock_dir, _MKDIR_OWNER_FILE), encoding="utf-8") as fh:
                stamp = fh.read().strip()
        except OSError:
            return "unknown (no ownership stamp)"
        fields = _parse_v2_stamp(stamp)
        if fields is None:
            return "unknown (unrecognised ownership stamp)"
        if not fields:
            return "unknown (incomplete ownership stamp)"
        parts = [f"host={fields['host']}", f"pid={fields['pid']}"]
        if fields["start"] != _STAMP_UNKNOWN:
            parts.append(f"start={fields['start']}")
        age = _mkdir_lock_age_s(lock_dir)
        if age is not None:
            parts.append(f"held={age:.0f}s")
        parts.append(f"pid_state={_describe_stamped_pid(fields)}")
        return " ".join(parts)
    except Exception:  # noqa: BLE001 — diagnostics are best-effort; never raise
        return "unknown (ownership stamp could not be read)"


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
    """Wall-clock age of *lock_dir* in seconds, or ``None`` if it cannot be stat'd.

    The ownership stamp is written once at acquisition and never refreshed (there is no
    heartbeat), so the lock dir's mtime is the acquisition time — the quantity the stale
    ceiling is measured against."""
    try:
        return time.time() - os.stat(lock_dir).st_mtime
    except OSError:
        return None


def _mkdir_lock_age_exceeds_ceiling(lock_dir: str) -> bool:
    """Whether *lock_dir* is older than :data:`_MKDIR_LOCK_STALE_CEILING_S`.

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

    Set *fcntl_held* only when the caller already holds the exclusive ``fcntl.flock``
    leg for this same tracker — see :func:`_acquire_mkdir` for why that is proof rather
    than a hint. The decision table:

    1. Owner file absent or unreadable → False (a bash-style lock, or one seen in the
       window between ``mkdir`` and the stamp write) — UNLESS the dir has out-aged
       :data:`_MKDIR_LOCK_STALE_CEILING_S`, the wall-clock backstop that stops an
       unprovable stamp wedging the store forever (9305 rec #1).
    2. Not a v2 stamp → exactly the legacy behaviour (:func:`_legacy_stamp_is_stale`).
    3. v2 stamp with missing/malformed fields → False, or reclaim past the age ceiling.
    4. v2 stamp from a different :func:`_host_identity` → the genuine foreign-host /
       shared-filesystem case: we cannot observe that host's processes and our own locks
       prove nothing about its kernel, so no pid, no namespace and no ``fcntl_held`` can
       license a reclaim (bug yaw-gravel-linen) — refused until the age ceiling, then
       reclaimed so a shared-filesystem orphan cannot wedge forever.
    5. Same host and the pid namespaces are comparable (stamped ``ns`` equals ours,
       including both being unknown) → probe the pid, qualified by start time. Dead pid
       ⇒ stale. Live pid whose start time is known on both sides and differs ⇒ the
       number was recycled by an unrelated process and the true owner is gone ⇒ stale.
       Anything else ⇒ False: a live owner is NEVER reclaimed — the ceiling does NOT
       apply here, because a live-pid probe is a positive liveness signal and breaking
       a lock on a timer alone over a live owner is forbidden ("never on a timer alone").
    6. Same host, namespaces NOT comparable (different, or exactly one unknown) — the
       container-recreate case, where the stamped pid is not probeable at all → stale
       iff *fcntl_held*, else refused until the age ceiling.

    Non-numeric pid on an otherwise same-host/same-namespace stamp is likewise a
    no-liveness-signal refusal and reclaims past the ceiling.
    """
    try:
        with open(os.path.join(lock_dir, _MKDIR_OWNER_FILE), encoding="utf-8") as fh:
            stamp = fh.read().strip()
    except OSError:
        return _mkdir_lock_age_exceeds_ceiling(lock_dir)

    fields = _parse_v2_stamp(stamp)
    if fields is None:
        return _legacy_stamp_is_stale(stamp)
    if not fields:
        return _mkdir_lock_age_exceeds_ceiling(lock_dir)

    if fields["host"] != _host_identity():
        return _mkdir_lock_age_exceeds_ceiling(lock_dir)

    stamped_ns = None if fields["ns"] == _STAMP_UNKNOWN else fields["ns"]
    if stamped_ns != _read_pid_namespace_id():
        # Same host, different (or unknowable) pid namespace: the stamped pid number is
        # meaningless to us, so the fcntl proof is the only positive evidence — and, short
        # of it, the age ceiling backstops the wedge.
        return fcntl_held or _mkdir_lock_age_exceeds_ceiling(lock_dir)

    try:
        pid = int(fields["pid"])
    except ValueError:
        return _mkdir_lock_age_exceeds_ceiling(lock_dir)
    if not _pid_alive(pid):
        return True
    stamped_start = None if fields["start"] == _STAMP_UNKNOWN else fields["start"]
    current_start = _process_start_time(pid)
    if stamped_start is not None and current_start is not None and stamped_start != current_start:
        # The pid is live but it is a DIFFERENT process wearing a recycled number.
        return True
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


def _acquire_mkdir(lock_dir: str, deadline: float) -> bool:
    """Poll ``mkdir`` (atomic on POSIX) until acquired or ``deadline``.

    On contention, reclaim a provably-stale lock (see :func:`_mkdir_lock_is_stale`).

    **Precondition (load bearing, hence ``fcntl_held=True`` below):** this is only ever
    reached from :func:`acquire` AFTER the exclusive ``fcntl.flock(LOCK_EX)`` leg on the
    same tracker's ``.ticket-write.lock`` has been taken, and the dual-window contract
    holds that leg for the entire lifetime of the mkdir leg (release order is mkdir then
    fcntl). That makes reclamation race-safe — no other Python acquirer is between mkdir
    and release — and, for a same-host owner in an unprobeable pid namespace, it is
    positive proof of death: ``flock`` is kernel-mediated on the inode, so it is shared
    across pid/mount namespaces on ONE kernel and the kernel drops it when its holder
    dies. A v2 stamp is only ever written by an acquirer that took the fcntl leg first,
    and the stamp's boot id says it was the same kernel; if that owner were still alive
    it would still hold this fcntl lock and we could not be here. This is a STRONGER
    proof than the pid probe it stands in for, not a weakening of it — and it does not
    extend to a different boot id, where our hold says nothing about the other kernel,
    which is why the foreign-host case stays refused (bugs castoff-tigerseye-ammonite,
    yaw-gravel-linen)."""
    while True:
        try:
            os.mkdir(lock_dir)
            # Stamp ownership so a later acquirer can reclaim this lock if we die
            # before releasing. Best-effort: a failed stamp only forfeits early
            # reclamation of our own lock (no correctness impact — we hold it).
            try:
                with open(os.path.join(lock_dir, _MKDIR_OWNER_FILE), "w", encoding="utf-8") as fh:
                    fh.write(_owner_stamp())
            except OSError:
                pass
            return True
        except FileExistsError:
            if _mkdir_lock_is_stale(lock_dir, fcntl_held=True):
                _reclaim_mkdir_lock(lock_dir)
            if time.monotonic() >= deadline:
                return False
            # Jittered so many competing acquirers de-synchronize (9305 research rec #4).
            time.sleep(_jitter(0.1))
        except OSError as exc:  # pragma: no cover - unexpected fs error
            if exc.errno == errno.EEXIST:
                if _mkdir_lock_is_stale(lock_dir, fcntl_held=True):
                    _reclaim_mkdir_lock(lock_dir)
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_jitter(0.1))
            else:
                raise


class LockHandle:
    """A held write lock; call :meth:`release` to drop it (mkdir leg then fcntl).

    The manual form for the ``ticket_txn`` critical section, whose many exit points
    release explicitly rather than via a ``with`` block. :func:`write_lock` wraps it.
    """

    __slots__ = ("_fd", "_have_mkdir", "_lock_dir", "_released")

    def __init__(self, fd: int, lock_dir: str, have_mkdir: bool) -> None:
        self._fd = fd
        self._lock_dir = lock_dir
        self._have_mkdir = have_mkdir
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._have_mkdir:
            # Remove our ownership stamp before rmdir — the dir is no longer empty
            # now that acquire stamps it (bug yaw-gravel-linen).
            try:
                os.remove(os.path.join(self._lock_dir, _MKDIR_OWNER_FILE))
            except OSError:
                pass
            try:
                os.rmdir(self._lock_dir)
            except OSError:
                pass
        try:
            os.close(self._fd)  # closing the fd releases the fcntl lock
        except OSError:
            pass


def write_path_retries() -> int:
    """Retry passes for the canonical write path: ``REBAR_LOCK_RETRIES`` (default 2),
    clamped to ``[0, _MAX_RETRIES]``. ``REBAR_LOCK_RETRIES=0`` restores the historical
    fail-fast-on-one-budget behaviour for CI/ops. An unparseable value falls back to the
    default rather than raising — a malformed knob must not break every write.

    The name is spelled as a LITERAL here, not lifted to a constant: ``gen_env_registry``
    only resolves string-literal keys, so a constant would ship this var undocumented."""
    raw = os.environ.get("REBAR_LOCK_RETRIES")
    if raw is None or not raw.strip():
        return _WRITE_PATH_RETRIES
    try:
        value = int(raw.strip())
    except ValueError:
        return _WRITE_PATH_RETRIES
    return max(0, min(value, _MAX_RETRIES))


def _retry_backoff_s(attempt: int) -> float:
    """Nominal (pre-jitter) gap before retry *attempt* (1-based): exponential with cap."""
    return min(_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)), _RETRY_BACKOFF_CAP_S)


def acquire(
    tracker: str | os.PathLike,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    attempts: int = _DEFAULT_ATTEMPTS,
    dual_window: bool = True,
    retries: int = _DEFAULT_RETRIES,
) -> LockHandle:
    """Acquire the exclusive tickets write lock; return a :class:`LockHandle` (I5).

    One PASS costs ``timeout × attempts`` seconds (bash ``flock_timeout × max_retries``).
    ``retries`` extra passes follow, separated by a jittered backoff, so a holder outliving
    a single budget costs LATENCY instead of a DISCARDED write (royal-weariless-zebrafish).
    ``retries=0`` (the default) is exactly the historical single-budget behaviour.

    The retry lives HERE, strictly inside acquisition, and that is what makes it safe: the
    caller's write body has not run when a pass expires, and once this returns the loop is
    finished and cannot re-enter — so nothing is replayed and no append is duplicated.
    Retrying a layer ABOVE the lock would not have that property.

    Raises :class:`LockTimeout` once every pass is spent, naming the CUMULATIVE wait."""
    tracker = canonical_tracker(tracker)
    # Story 21dd: fail CLOSED on a store whose committed .store-compat.json this rebar
    # cannot interpret, BEFORE any lock-held write. This single insertion at the write-
    # lock chokepoint covers write_lock() and every direct acquire() caller (txn,
    # compact, fsck repair). Reads never acquire the write lock, so they stay available.
    check_store_compat(tracker)
    total_wait = timeout * attempts
    started = time.monotonic()
    for spent in range(retries + 1):
        try:
            return _acquire_once(tracker, total_wait, dual_window)
        except LockTimeout as exc:
            if spent == retries:
                if retries == 0:
                    raise
                raise LockTimeout(int(time.monotonic() - started), exc.holder) from None
            logger.warning(
                "write lock still held after %ss (pass %d/%d) — retrying; holder: %s",
                total_wait,
                spent + 1,
                retries + 1,
                exc.holder or "unknown",
            )
            _backoff_sleep(_jitter(_retry_backoff_s(spent + 1)))
    raise AssertionError("unreachable")  # pragma: no cover - loop always returns or raises


def _acquire_once(tracker: str, total_wait: int, dual_window: bool) -> LockHandle:
    """ONE acquisition pass against a ``total_wait``-second deadline: fcntl leg first,
    then (when ``dual_window``) the mkdir leg. Raises :class:`LockTimeout` if either leg
    cannot be taken in budget. *tracker* is already canonical."""
    lock_path = os.path.join(tracker, WRITE_LOCK_NAME)
    lock_dir = os.path.join(tracker, MKDIR_LOCK_NAME)
    deadline = time.monotonic() + total_wait

    fd = _acquire_fcntl(lock_path, deadline)
    if fd == -1:
        # Name the holder from the stamp it already wrote (bug 7084) — a timed-out waiter
        # that cannot say WHO blocked it costs an operator a commit-timeline inference.
        raise LockTimeout(total_wait, describe_lock_holder(tracker))
    if dual_window:
        if not _acquire_mkdir(lock_dir, deadline):
            holder = describe_lock_holder(tracker)  # read before releasing our fcntl leg
            os.close(fd)
            raise LockTimeout(total_wait, holder)
        return LockHandle(fd, lock_dir, True)
    return LockHandle(fd, lock_dir, False)


@contextmanager
def write_lock(
    tracker: str | os.PathLike,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    attempts: int = _DEFAULT_ATTEMPTS,
    dual_window: bool = True,
    retries: int = _DEFAULT_RETRIES,
) -> Iterator[None]:
    """Hold the exclusive tickets write lock for the duration of the ``with`` block
    (I5). Thin wrapper over :func:`acquire`/:meth:`LockHandle.release`."""
    handle = acquire(
        tracker, timeout=timeout, attempts=attempts, dual_window=dual_window, retries=retries
    )
    try:
        yield
    finally:
        handle.release()
