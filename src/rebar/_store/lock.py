"""Provide the tickets store's single write lock.

All write paths use a fixed two-leg acquisition. The platform kernel lock on
``.ticket-write.lock`` precedes the atomic mkdir lock ``.ticket-write.lock.d``. Release uses
the reverse order. The kernel leg is released on process death. The mkdir leg preserves
portable exclusion and ownership evidence. ``dual_window=True`` is the permanent default.
``dual_window=False`` deliberately selects only the kernel leg.

The mkdir leg records a colon-free v2 stamp::

    rebar-lock v2 host=<host-identity> ns=<pid-ns-id> pid=<pid> start=<start-time>

The boot ID identifies container recreations on one kernel, with the hostname as fallback.
The namespace, PID, and start time distinguish recycled PIDs. Legacy readers refuse the
colon-free form. :class:`LockTimeout` reports these existing fields without affecting
reclamation. Unproven ownership remains held until the age ceiling. Corroborated liveness is
never overridden by age.
"""

from __future__ import annotations

import errno
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

from rebar._store import lock_kernel as _kernel
from rebar._store import lock_owner as _owner
from rebar._store.compat import check_store_compat
from rebar._store.gitutil import _backoff_sleep, _jitter

logger = logging.getLogger(__name__)

WRITE_LOCK_NAME = ".ticket-write.lock"
MKDIR_LOCK_NAME = ".ticket-write.lock.d"
# Bash parity: FLOCK_STAGE_COMMIT_TIMEOUT (default 30s) per attempt × max_retries(2).
_DEFAULT_TIMEOUT = 30
_DEFAULT_ATTEMPTS = 2

# Optional bounded passes after one acquisition budget expires. Zero preserves existing callers.
# Canonical writes opt in through :func:`write_path_retries`. Maintenance stays single-pass so
# optional work continues to stand aside.
_DEFAULT_RETRIES = 0
_WRITE_PATH_RETRIES = 2
_MAX_RETRIES = 10
_RETRY_BACKOFF_BASE_S = 0.5
_RETRY_BACKOFF_CAP_S = 2.0


# Exceptions carry a subprocess-compatible return code and message. They do not print, so
# callers emit each failure once.


class LockTimeout(Exception):
    """Report an exhausted write-lock budget with a bash-compatible prefix.

    When available, the suffix renders the existing v2 ownership stamp through
    :func:`describe_lock_holder`. Missing, unreadable, or unsupported stamps produce an
    explicit ``unknown`` reason. Reporting adds no state and never affects reclamation."""

    returncode = 1

    def __init__(self, total_wait: int, holder: str | None = None) -> None:
        self.total_wait = total_wait
        self.holder = holder
        message = f"flock: could not acquire lock after {total_wait}s"
        if holder:
            message += f" (holder: {holder})"
            # If the holder is labelled optional housekeeping (a compaction sweep), state the
            # safe action inline so an operator need not chase a docstring to learn that
            # interrupting it is lossless (camerashy-erectable-frog).
            remedy = _owner.interruptible_remedy(holder)
            if remedy:
                message += f"\n    {remedy}"
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
    """Poll the platform's exclusive leg until acquired or ``deadline``. Returns the held
    fd (caller closes to release). Raises :class:`LockTimeout`-signal via returning -1 on
    timeout (caller maps to the right total_wait).

    The leg is ``fcntl.flock(LOCK_EX|LOCK_NB)`` on POSIX and ``msvcrt.locking(LK_NBLCK)``
    on Windows, selected by :mod:`rebar._store.lock_kernel`; the name is kept because this
    IS the fcntl leg wherever ``fcntl`` exists. On a platform with NEITHER primitive the
    selector raises :class:`~rebar._store.lock_kernel.NoExclusiveLegError` and it
    propagates: acquiring the mkdir leg without an exclusive leg held would falsify
    ``fcntl_held=True`` and let rebar reclaim a live holder's lock."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    while True:
        try:
            _kernel.take_exclusive(fd)
            return fd
        except OSError as exc:
            # Only genuine contention (the lock is held elsewhere) is waited out; any other
            # errno (ENOLCK/EIO/EBADF/…) is a real fault that must surface with its identity
            # rather than be masked as a spurious 30-60s LockTimeout. (EINTR does not reach
            # here: PEP 475 retries the interrupted syscall at the C level.)
            if not _kernel.is_contention(exc):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                return -1
            # Jittered so many competing acquirers de-synchronize (9305 research rec #4).
            time.sleep(_jitter(0.05))


def write_lock_is_busy(tracker: str | os.PathLike) -> bool:
    """Probe whether *tracker*'s write lock is held without waiting.

    The probe takes the kernel and mkdir legs in acquisition order with a zero deadline.
    Both are required to detect portable or legacy holders. Every acquired leg is released.
    Errors report not busy. This racy advisory result only lets optional compaction stand aside.
    Mutation safety still depends on :func:`acquire`."""
    tracker = canonical_tracker(tracker)
    lock_path = os.path.join(tracker, WRITE_LOCK_NAME)
    lock_dir = os.path.join(tracker, MKDIR_LOCK_NAME)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            _kernel.take_exclusive(fd)
        except _kernel.NoExclusiveLegError:
            # No primitive to probe WITH. This function is advisory and documented to fail
            # OPEN, so report "not busy" and let the real acquire arbitrate — exactly the
            # degradation an unexpected error already takes.
            return False
        except OSError as exc:
            # Genuine contention answers the question; any other errno is a real fault
            # that must not masquerade as a busy lock (it would silently suppress work).
            return _kernel.is_contention(exc)
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
    """Return a nonraising description of the current write-lock holder.

    The description renders the existing v2 host, PID, start time, hold age, liveness, and
    operation fields. Missing or unreadable stamps, unsupported forms, and incomplete v2
    stamps return distinct ``unknown`` reasons."""
    try:
        lock_dir = os.path.join(canonical_tracker(tracker), MKDIR_LOCK_NAME)
        try:
            with open(os.path.join(lock_dir, _owner._MKDIR_OWNER_FILE), encoding="utf-8") as fh:
                stamp = fh.read().strip()
        except OSError:
            return "unknown (no ownership stamp)"
        fields = _owner._parse_v2_stamp(stamp)
        if fields is None:
            return "unknown (unrecognised ownership stamp)"
        if not fields:
            return "unknown (incomplete ownership stamp)"
        parts = [f"host={fields['host']}", f"pid={fields['pid']}"]
        if fields["start"] != _owner._STAMP_UNKNOWN:
            parts.append(f"start={fields['start']}")
        age = _owner._mkdir_lock_age_s(lock_dir)
        if age is not None:
            parts.append(f"held={age:.0f}s")
        parts.append(f"pid_state={_owner._describe_stamped_pid(fields)}")
        if "op" in fields:
            parts.append(f"op={fields['op']}")
        return " ".join(parts)
    except Exception:  # noqa: BLE001 — diagnostics are best-effort; never raise
        return "unknown (ownership stamp could not be read)"


def _acquire_mkdir(lock_dir: str, deadline: float) -> bool:
    """Poll atomic ``mkdir`` until acquired or *deadline*.

    The caller must hold the same tracker's platform kernel leg for the mkdir lifetime.
    A surviving same-host owner would still hold that leg, so possession proves a prior
    same-host owner is gone. It proves nothing about another host. Both supported kernel
    primitives enforce machine-wide exclusion and release on process death. A platform
    without either primitive raises before this function."""
    while True:
        try:
            os.mkdir(lock_dir)
            # Stamp ownership so a later acquirer can reclaim this lock if we die
            # before releasing. Best-effort: a failed stamp only forfeits early
            # reclamation of our own lock (no correctness impact — we hold it).
            try:
                with open(
                    os.path.join(lock_dir, _owner._MKDIR_OWNER_FILE), "w", encoding="utf-8"
                ) as fh:
                    fh.write(_owner._owner_stamp())
            except OSError:
                pass
            return True
        except FileExistsError:
            if _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True):
                _owner._reclaim_mkdir_lock(lock_dir)
            if time.monotonic() >= deadline:
                return False
            # Jittered so many competing acquirers de-synchronize (9305 research rec #4).
            time.sleep(_jitter(0.1))
        except OSError as exc:  # pragma: no cover - unexpected fs error
            if exc.errno == errno.EEXIST:
                if _owner._mkdir_lock_is_stale(lock_dir, fcntl_held=True):
                    _owner._reclaim_mkdir_lock(lock_dir)
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
                os.remove(os.path.join(self._lock_dir, _owner._MKDIR_OWNER_FILE))
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
    """Return canonical write retry passes clamped to ``[0, _MAX_RETRIES]``.

    ``REBAR_LOCK_RETRIES`` defaults to two and zero restores one-budget behavior. Invalid
    values use the default. The owned config resolver keeps environment access internal and
    preserves existing call signatures."""
    from rebar import config

    return config.resolve_lock_retries(_WRITE_PATH_RETRIES, _MAX_RETRIES)


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
    """Acquire the exclusive tickets write lock and return its handle.

    Each pass has a ``timeout * attempts`` budget. Extra passes use jittered backoff, while
    zero retries preserves one-budget behavior. Retrying inside acquisition is safe because
    the caller's write body has not run. Exhaustion raises :class:`LockTimeout` with the
    cumulative wait."""
    tracker = canonical_tracker(tracker)
    # Reject an incompatible committed store before taking a write lock. This chokepoint
    # covers direct acquisition and ``write_lock`` callers while preserving reads.
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
    """Run one acquisition pass against a ``total_wait``-second deadline.

    The kernel leg precedes the optional mkdir leg. Failure to acquire either raises
    :class:`LockTimeout`. *tracker* is already canonical."""
    lock_path = os.path.join(tracker, WRITE_LOCK_NAME)
    lock_dir = os.path.join(tracker, MKDIR_LOCK_NAME)
    deadline = time.monotonic() + total_wait

    fd = _acquire_fcntl(lock_path, deadline)
    if fd == -1:
        # Report the existing stamp so an operator can identify the blocker.
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
