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

Refusal-without-proof (bug ``yaw-gravel-linen``) is unchanged and deliberate: a stamp
from a *different* host identity is still never reclaimed. See
:func:`_mkdir_lock_is_stale` for the full decision table and :func:`_acquire_mkdir`
for why the held fcntl leg is positive proof of a dead same-host owner.
"""

from __future__ import annotations

import errno
import fcntl
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

from rebar._store.compat import check_store_compat

WRITE_LOCK_NAME = ".ticket-write.lock"
MKDIR_LOCK_NAME = ".ticket-write.lock.d"
# Ownership stamp written inside the mkdir lock dir so a future acquirer can detect
# (and reclaim) a lock orphaned by a process that died before releasing it. Lives
# INSIDE .ticket-write.lock.d/, which is gitignored, so it never surfaces untracked.
_MKDIR_OWNER_FILE = "owner"

# Bash parity: FLOCK_STAGE_COMMIT_TIMEOUT (default 30s) per attempt × max_retries(2).
_DEFAULT_TIMEOUT = 30
_DEFAULT_ATTEMPTS = 2


# Exceptions carry (returncode, full stderr text); callers surface the message
# exactly once — mirroring the subprocess path where bash printed to stderr and the
# seam captured it into CommandError. We do NOT print here to avoid double-emit.


class LockTimeout(Exception):
    """Could not acquire the write lock within the budget (bash exit 1, stderr
    ``flock: could not acquire lock after Ns``)."""

    returncode = 1

    def __init__(self, total_wait: int) -> None:
        self.total_wait = total_wait
        super().__init__(f"flock: could not acquire lock after {total_wait}s")


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
            time.sleep(0.05)


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
       window between ``mkdir`` and the stamp write).
    2. Not a v2 stamp → exactly the legacy behaviour (:func:`_legacy_stamp_is_stale`).
    3. v2 stamp with missing/malformed fields → False.
    4. v2 stamp from a different :func:`_host_identity` → False, **always**. This is the
       genuine foreign-host / shared-filesystem case: we cannot observe that host's
       processes and our own locks prove nothing about its kernel, so no pid, no
       namespace and no ``fcntl_held`` can license a reclaim (bug yaw-gravel-linen).
    5. Same host and the pid namespaces are comparable (stamped ``ns`` equals ours,
       including both being unknown) → probe the pid, qualified by start time. Dead pid
       ⇒ stale. Live pid whose start time is known on both sides and differs ⇒ the
       number was recycled by an unrelated process and the true owner is gone ⇒ stale.
       Anything else ⇒ False: a live owner is NEVER reclaimed.
    6. Same host, namespaces NOT comparable (different, or exactly one unknown) — the
       container-recreate case, where the stamped pid is not probeable at all → stale
       iff *fcntl_held*, else False.
    """
    try:
        with open(os.path.join(lock_dir, _MKDIR_OWNER_FILE), encoding="utf-8") as fh:
            stamp = fh.read().strip()
    except OSError:
        return False

    fields = _parse_v2_stamp(stamp)
    if fields is None:
        return _legacy_stamp_is_stale(stamp)
    if not fields:
        return False

    if fields["host"] != _host_identity():
        return False

    stamped_ns = None if fields["ns"] == _STAMP_UNKNOWN else fields["ns"]
    if stamped_ns != _read_pid_namespace_id():
        # Same host, different (or unknowable) pid namespace: the stamped pid number is
        # meaningless to us, so the fcntl proof is the only evidence available.
        return fcntl_held

    try:
        pid = int(fields["pid"])
    except ValueError:
        return False
    if not _pid_alive(pid):
        return True
    stamped_start = None if fields["start"] == _STAMP_UNKNOWN else fields["start"]
    current_start = _process_start_time(pid)
    if stamped_start is not None and current_start is not None and stamped_start != current_start:
        # The pid is live but it is a DIFFERENT process wearing a recycled number.
        return True
    return False


def _reclaim_mkdir_lock(lock_dir: str) -> None:
    """Remove a provably-stale mkdir lock (owner stamp + dir). Best-effort: a failure
    just leaves the next acquirer to wait/retry — never a correctness hazard."""
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
            time.sleep(0.1)
        except OSError as exc:  # pragma: no cover - unexpected fs error
            if exc.errno == errno.EEXIST:
                if _mkdir_lock_is_stale(lock_dir, fcntl_held=True):
                    _reclaim_mkdir_lock(lock_dir)
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)
            else:
                raise


class LockHandle:
    """A held write lock; call :meth:`release` to drop it (mkdir leg then fcntl).

    The manual form for the ``ticket_txn`` critical section, whose many exit points
    release explicitly rather than via a ``with`` block. :func:`write_lock` wraps it.
    """

    __slots__ = ("_fd", "_lock_dir", "_have_mkdir", "_released")

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


def acquire(
    tracker: str | os.PathLike,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    attempts: int = _DEFAULT_ATTEMPTS,
    dual_window: bool = True,
) -> LockHandle:
    """Acquire the exclusive tickets write lock; return a :class:`LockHandle` (I5).

    Budget = ``timeout × attempts`` seconds (bash ``flock_timeout × max_retries``;
    60s for the write path, 30s for ``ticket_txn`` via ``attempts=1``). fcntl first,
    then (when ``dual_window``) the mkdir leg. Raises :class:`LockTimeout` if either
    leg cannot be taken in budget."""
    tracker = canonical_tracker(tracker)
    # Story 21dd: fail CLOSED on a store whose committed .store-compat.json this rebar
    # cannot interpret, BEFORE any lock-held write. This single insertion at the write-
    # lock chokepoint covers write_lock() and every direct acquire() caller (txn,
    # compact, fsck repair). Reads never acquire the write lock, so they stay available.
    check_store_compat(tracker)
    total_wait = timeout * attempts
    lock_path = os.path.join(tracker, WRITE_LOCK_NAME)
    lock_dir = os.path.join(tracker, MKDIR_LOCK_NAME)
    deadline = time.monotonic() + total_wait

    fd = _acquire_fcntl(lock_path, deadline)
    if fd == -1:
        raise LockTimeout(total_wait)
    if dual_window:
        if not _acquire_mkdir(lock_dir, deadline):
            os.close(fd)
            raise LockTimeout(total_wait)
        return LockHandle(fd, lock_dir, True)
    return LockHandle(fd, lock_dir, False)


@contextmanager
def write_lock(
    tracker: str | os.PathLike,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    attempts: int = _DEFAULT_ATTEMPTS,
    dual_window: bool = True,
) -> Iterator[None]:
    """Hold the exclusive tickets write lock for the duration of the ``with`` block
    (I5). Thin wrapper over :func:`acquire`/:meth:`LockHandle.release`."""
    handle = acquire(tracker, timeout=timeout, attempts=attempts, dual_window=dual_window)
    try:
        yield
    finally:
        handle.release()
