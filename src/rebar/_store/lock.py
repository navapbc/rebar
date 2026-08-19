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
reclamation logic (``lock_owner._mkdir_lock_is_stale``). Because neither leg waits on the other
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
:func:`rebar._store.lock_owner._mkdir_lock_is_stale` for the full decision table, and
:func:`_acquire_mkdir`
for why the held fcntl leg is positive proof of a dead same-host owner.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

from rebar._store import lock_owner as _owner
from rebar._store.compat import check_store_compat
from rebar._store.gitutil import _backoff_sleep, _jitter

logger = logging.getLogger(__name__)

WRITE_LOCK_NAME = ".ticket-write.lock"
MKDIR_LOCK_NAME = ".ticket-write.lock.d"
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

    Reads the ownership stamp the mkdir leg ALREADY writes
    (:func:`rebar._store.lock_owner._owner_stamp`) and
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
    * a shape :func:`rebar._store.lock_owner._parse_v2_stamp` declines (the forward-compat
      path a NEWER rebar's
      stamp would take here) → ``unrecognised``;
    * a v2 stamp missing required fields (a torn mid-write read) → ``incomplete``.
    """
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
    """Poll ``mkdir`` (atomic on POSIX) until acquired or ``deadline``.

    On contention, reclaim a provably-stale lock (see
    :func:`rebar._store.lock_owner._mkdir_lock_is_stale`).

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
    """Retry passes for the canonical write path: ``REBAR_LOCK_RETRIES`` (default 2),
    clamped to ``[0, _MAX_RETRIES]``. ``REBAR_LOCK_RETRIES=0`` restores the historical
    fail-fast-on-one-budget behaviour for CI/ops. An unparseable value falls back to the
    default rather than raising — a malformed knob must not break every write.

    Resolved through the owned config seam (:func:`rebar.config.resolve_lock_retries`)
    rather than ``os.environ`` here; the signature is kept (the read is INTERNAL) so the
    ``event_append`` / ``txn`` call sites that pass ``retries=write_path_retries()`` are
    untouched."""
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
