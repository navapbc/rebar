"""The ONE write lock for the tickets store.

Unifies the three historical lock acquisitions — bash ``_flock_stage_commit``
(``ticket-lib.sh``), ``ticket_txn.py``, and ``event_append.write_lock`` — into a
single implementation so the whole system holds ONE lock (invariant I5).

**The dual-window interop rule (docs/bash-migration.md §6).** While any bash core
may still run (the whole Tier D window), the Python lock acquires BOTH mechanisms
in a fixed order — ``fcntl.flock(LOCK_EX)`` on ``.ticket-write.lock`` FIRST, then
the mkdir lock ``.ticket-write.lock.d`` — releasing in reverse (mkdir leg in a
``finally``). Bash ``_flock_stage_commit`` holds at most ONE mechanism (util-linux
``flock(1)`` *or* the mkdir fallback), so:

* on a ``flock(1)`` host, Python's ``fcntl.flock`` contends with bash's ``flock(1)``
  (same ``flock(2)`` syscall, same file); the mkdir leg is uncontended overhead;
* on a ``flock(1)``-less host (bash falls back to mkdir), Python's mkdir leg
  contends with bash's mkdir; the fcntl leg is uncontended.

Either way mutual exclusion holds, and because bash never waits on a second
mechanism there is no hold-and-wait cycle ⇒ deadlock-free. This closes the
``stiff-mop-lane`` gap (fcntl-only Python vs mkdir-only bash on macOS). After the
bash core is retired, ``dual_window`` flips to ``False`` permanently and the system
converges on plain ``fcntl.flock``.
"""

from __future__ import annotations

import errno
import fcntl
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

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


def _owner_stamp() -> str:
    """Identity written into a freshly-acquired mkdir lock: ``<hostname>:<pid>``."""
    return f"{socket.gethostname()}:{os.getpid()}"


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


def _mkdir_lock_is_stale(lock_dir: str) -> bool:
    """A held mkdir lock is reclaimable ONLY when its owner stamp proves a DEAD
    process on THIS host. An absent/unparseable stamp (a bash-style lock, or one
    observed in the brief window between mkdir and the stamp write), a foreign-host
    owner (whose liveness we cannot check — the shared-filesystem case), or a live
    PID all return False: never reclaim on anything short of proof (bug
    yaw-gravel-linen)."""
    try:
        with open(os.path.join(lock_dir, _MKDIR_OWNER_FILE), encoding="utf-8") as fh:
            stamp = fh.read().strip()
    except OSError:
        return False
    host, sep, pid_s = stamp.partition(":")
    if not sep or host != socket.gethostname():
        return False
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    return not _pid_alive(pid)


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
    This is race-safe because :func:`acquire` already holds the fcntl leg before
    calling here, so no other Python acquirer is between mkdir and release — and a
    dead owner stamp proves no process holds the lock."""
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
            if _mkdir_lock_is_stale(lock_dir):
                _reclaim_mkdir_lock(lock_dir)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        except OSError as exc:  # pragma: no cover - unexpected fs error
            if exc.errno == errno.EEXIST:
                if _mkdir_lock_is_stale(lock_dir):
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
