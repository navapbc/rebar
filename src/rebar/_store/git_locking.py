"""Apply the ticket store's Git lock-contention policy.

``gitutil`` runs Git processes, while this module coordinates the advisory locks,
bounded retries, and stale-lock reclamation around supplied callables. It launches
no Git process. ``_resolve_tracker_git_dir`` locates per-worktree and common lock
files. ``gitutil`` re-exports it for fsck and bridge repair. The dependency points
from ``gitutil`` into this stdlib-based module, with ``git_outcome`` supplying the
marker registry.

Concurrent agents make index and ref lock conflicts expected. The policy from bug
9305-b42c serializes rebar's index mutations behind a kernel-released advisory
lock, then retries Git lock signatures with a bounded jittered backoff. Only
index locks receive conservative stale reclamation. Exhaustion produces one
diagnostic naming the contended lock and its remedy.

Bounded waiting suits unattended callers better than indefinite blocking, while
retry handles contention better than immediate failure. The policy has no config
surface. Tests adjust its module constants.
"""

from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from rebar._store import git_outcome

try:  # POSIX advisory locking; absent on some platforms (e.g. plain Windows)
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ── tracker git-dir resolution (every lock path below is anchored on it) ─────
# Pure filesystem: no git subprocess, no rebar module-level import
# (ticket b432-c9dc-c1b4-4a45). Re-exported by ``gitutil`` for its historical importers.


def _resolve_tracker_git_dir(tracker: str) -> str:
    tracker_git = os.path.join(tracker, ".git")
    if os.path.isfile(tracker_git):
        with open(tracker_git, encoding="utf-8") as f:
            gitdir = f.read().strip()
        gitdir = gitdir[len("gitdir: ") :] if gitdir.startswith("gitdir: ") else gitdir
        if not gitdir.startswith("/"):
            gitdir = os.path.join(tracker, gitdir)
        return gitdir
    if os.path.isdir(tracker_git):
        return tracker_git
    return ""


# The tickets tracker is a SHARED git worktree, so rebar's own write lock (which only
# serialises writes WITHIN one clone) does not stop a concurrent rebar process — or a
# crashed git that left a stale lock — from colliding on git's OWN ``.git/index.lock``.
# git then refuses ``git add``/``git commit`` with "Unable to create '<gitdir>/index.lock':
# File exists. Another git process seems to be running …". A CONTENDED lock (a live peer
# that releases quickly) clears on retry, so riding it out with a bounded backoff turns a
# hard write loss into a self-healed write. A STALE lock (a crashed git that never
# released) is reclaimed between attempts ONLY when provably old (mtime age >
# ``_INDEX_LOCK_STALE_S``) — never a young/live lock, whose removal can corrupt a peer's
# index; a young lock that never releases still ultimately FAILS the write. Same staleness
# threshold + resolution helper as fsck's Check 3 (bug fix-indexlock-retry). Shared here so
# EVERY index-mutating git op (event_append's add/commit AND txn.py's claim/transition
# add/commit) self-heals through the one implementation.
_INDEX_LOCK_STALE_S = 300  # a lock older than this is a crash remnant, safe to reclaim
# Bounded jittered backoff (bug 9305): gap(n) = min(base * 2^(n-1), cap) x jitter — the sum
# of the nominal inter-attempt gaps is ~21s (see _lock_retry_budget_s), the operator-ratified
# "tens of seconds" default, up from the pre-9305 ~2s that a peer's ordinary commit could
# outlast. Exponential-with-cap is git lockfile.c's own shape (quadratic-with-cap there).
_INDEX_LOCK_ATTEMPTS = 10
_INDEX_LOCK_BACKOFF_S = 0.2
_INDEX_LOCK_BACKOFF_CAP_S = 5.0


def _jitter(delay: float) -> float:
    """*delay* scaled by uniform [0.75, 1.25] — git lockfile.c's exact jitter shape
    ("back off for between 0.75*backoff_ms and 1.25*backoff_ms"), so competing processes
    de-synchronize instead of thundering in lockstep (9305 research rec #4)."""
    return delay * random.uniform(0.75, 1.25)


def _backoff_sleep(seconds: float) -> None:
    """rebar's own retry/poll backoff (bug 9305-b42c), behind one seam so a test can
    measure rebar's deliberate latency and not stdlib subprocess's timeout-poll sleep."""
    time.sleep(seconds)


def _lock_backoff_s(attempt: int) -> float:
    """The nominal (pre-jitter) backoff before retry *attempt* (1-based gap index)."""
    return min(_INDEX_LOCK_BACKOFF_S * (2 ** (attempt - 1)), _INDEX_LOCK_BACKOFF_CAP_S)


def _lock_retry_budget_s() -> float:
    """The nominal total wait the lock retry can spend backing off (the bounded budget a
    stuck lock exhausts before its one actionable error surfaces)."""
    return sum(_lock_backoff_s(n) for n in range(1, _INDEX_LOCK_ATTEMPTS))


def _is_index_lock_error(text: str) -> bool:
    """True if *text* is git's index.lock-contention signature (case-insensitive).

    A lookup against the shared registry (:mod:`rebar._store.git_outcome`), which owns
    the marker strings; kept under this name so the existing call sites are unchanged."""
    return git_outcome.is_index_lock(text)


def _is_git_lock_error(text: str) -> bool:
    """True if *text* is ANY git lock-conflict signature: the index.lock contention above,
    a ref lock, or another ``<name>.lock`` create conflict (``HEAD.lock`` /
    ``packed-refs.lock`` / ``config.lock``). Only index.lock gets the stale-reclaim
    treatment; the others are purely ridden out (git's maintenance holds ref locks for
    microseconds — retry has a real chance; 9305 research §1a).

    A lookup against the shared registry; the markers live in
    :mod:`rebar._store.git_outcome`."""
    return git_outcome.is_git_lock(text)


# git names the contended lock file in its own message ("Unable to create '<path>': File
# exists." / "cannot lock ref '<ref>'"); parse it so the exhaustion error can name it.
_LOCK_PATH_RE = re.compile(r"[Uu]nable to create '([^']+\.lock)'")
# Parses the contended ref NAME out of git's message for the exhaustion hint — a formatting
# concern, not a verdict, so it stays here with _augment_lock_exhaustion.
# git-marker-ok: extracts the ref name for an error message; it classifies nothing.
_LOCK_REF_RE = re.compile(r"cannot lock ref '([^']+)'")


def _augment_lock_exhaustion(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    """Fold rebar's actionable guidance into the FINAL still-locked failure: name the
    contended lock file (parsed from git's own message when present) and state the remedy.
    The existing call-site contracts (raise from ``stderr`` / print ``stderr``) then surface
    one actionable error with no per-site changes."""
    text = result.stderr or result.stdout or ""
    m = _LOCK_PATH_RE.search(text)
    if m:
        lock_name = m.group(1)
    else:
        ref = _LOCK_REF_RE.search(text)
        lock_name = f"ref '{ref.group(1)}'" if ref else "a git lock file"
    result.stderr = (result.stderr or "") + (
        f"\nrebar: git lock still contended after {_INDEX_LOCK_ATTEMPTS} attempts over "
        f"~{_lock_retry_budget_s():.0f}s (lock: {lock_name}). Another git process holds it; "
        "if no git process is running on this store, the lock file is stale — remove it "
        "and retry."
    )
    return result


# Per-store advisory git-op lock (bug 9305). rebar's store write lock already serializes
# the LOCKED write paths, but git ops also run outside it (sync/push fetch-merge legs,
# fsck repair, init) and non-rebar git can hold the same locks — so the seam additionally
# serializes rebar's OWN index-mutating git invocations behind one kernel flock per store.
# flock is dropped by the kernel when its holder dies: no staleness/reclamation logic to
# get wrong (the cargo/Bazel property, 9305 research §3). ADVISORY: on budget exhaustion
# we log and proceed WITHOUT it — correctness stays with the git-level retry below (JGit
# takes its in-process fair lock before the file lock for the same reason, §1c(b)).
_STORE_GIT_LOCK_NAME = "rebar-git-op.lock"
_STORE_GIT_LOCK_WAIT_S = 30.0
_STORE_GIT_LOCK_POLL_S = 0.05


def _store_git_lock_path(tracker: str) -> str | None:
    """The per-store advisory lock file path (inside the tracker's git dir, like git's own
    locks — never tracked), or ``None`` when *tracker* is not a git repo."""
    git_dir = _resolve_tracker_git_dir(tracker)
    if not git_dir:
        return None
    return os.path.join(git_dir, _STORE_GIT_LOCK_NAME)


@contextmanager
def _store_git_op_lock(tracker: str) -> Iterator[None]:
    """Hold the per-store advisory git-op lock for one git invocation (plus its retries).

    Fast path is a single non-blocking ``flock`` — zero added latency, NO sleep, when
    uncontended. When a peer holds it, poll with short jittered sleeps up to
    ``_STORE_GIT_LOCK_WAIT_S`` (debug log), then — advisory, not load-bearing — proceed
    without it at warning level: the bounded git-level lock retry still governs
    correctness, so a wedged peer degrades to pre-9305 behavior rather than a new failure
    mode. Best-effort on any OS fault (an unwritable git dir must not fail the git op)."""
    lock_path = _store_git_lock_path(tracker)
    if lock_path is None or fcntl is None:
        yield
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        yield
        return
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            logger.debug("store git-op lock %s contended; waiting (bounded)", lock_path)
            deadline = time.monotonic() + _STORE_GIT_LOCK_WAIT_S
            while time.monotonic() < deadline:
                _backoff_sleep(_jitter(_STORE_GIT_LOCK_POLL_S))
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    continue
            if not acquired:
                logger.warning(
                    "store git-op lock %s still held after %.0fs; proceeding without "
                    "serialization (the bounded git lock retry still governs)",
                    lock_path,
                    _STORE_GIT_LOCK_WAIT_S,
                )
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# Cross-process fetch coordination keyed on the Git COMMON directory (bug
# agrologic-oval-bobolink). Remote-tracking refs (``refs/remotes/<remote>/<branch>``) live
# in the common dir, SHARED by every linked worktree and by the symlinked tickets clone. Two
# uncoordinated ref-updating fetches against one common dir race git's ref compare-and-swap,
# and the loser fails ``cannot lock ref '<ref>': is at <new> but expected <old>`` — the
# snapshot fetch aborts an attested op, the sync fetch silently drops a freshness round.
# The per-worktree advisory git-op lock above does NOT cover this: its identity is the
# per-worktree git dir (correct for the per-worktree ``index.lock``), while the racing refs
# are shared, and the sync/snapshot fetch legs run OUTSIDE it anyway. So ref-updating
# fetches take ONE exclusive flock keyed on the canonical common dir, which serializes
# every rebar fetcher sharing those refs regardless of which worktree or operation drives
# it. It is a dedicated FETCH lock, never the ticket write lock, so a slow network fetch
# serializes only other fetches — never a local ticket writer. Acquisition is BOUNDED and
# then degrades to unlocked (like the advisory git-op lock): a wedged peer fetch must not
# pin the long-lived MCP server behind it, and the caller's bounded compare-and-swap retry
# stays the correctness net once we proceed unlocked.
_FETCH_COORD_LOCK_NAME = "rebar-fetch.lock"
# Bound how long to wait for a peer's fetch before proceeding unlocked. Comfortably longer
# than a healthy tickets fetch (a few tiny event files) yet far short of the snapshot
# fetch's 300s ceiling, so one wedged holder degrades to the CAS-retry net rather than
# stalling the server for minutes.
_FETCH_COORD_WAIT_S = 30.0
_FETCH_COORD_POLL_S = 0.05


def _resolve_common_git_dir(repo_root: str) -> str | None:
    """The canonical Git COMMON directory of *repo_root* — the one every linked worktree
    shares and where remote-tracking refs live — symlink-resolved to an absolute path, or
    ``None`` when *repo_root* is not a git repo.

    A linked worktree's per-worktree git dir carries a ``commondir`` pointer to it; a main
    checkout's git dir IS the common dir. Pure filesystem (no git subprocess), matching this
    module's other git-dir resolution, and ``realpath`` collapses the ``.tickets-tracker``
    symlink so two worktrees of one store resolve to the SAME identity."""
    git_dir = _resolve_tracker_git_dir(repo_root)
    if not git_dir:
        return None
    commondir_marker = os.path.join(git_dir, "commondir")
    if os.path.isfile(commondir_marker):
        try:
            with open(commondir_marker, encoding="utf-8") as f:
                common = f.read().strip()
        except OSError:
            common = ""
        if common:
            if not os.path.isabs(common):
                common = os.path.join(git_dir, common)
            git_dir = common
    return os.path.realpath(git_dir)


def _acquire_fetch_coord_flock(fd: int) -> bool:
    """Take the common-dir fetch flock, BOUNDED. Fast path is a single non-blocking
    ``LOCK_EX`` — zero added latency when uncontended. When a peer holds it, poll with short
    jittered sleeps up to ``_FETCH_COORD_WAIT_S`` and then give up, returning ``False`` so
    the caller proceeds UNLOCKED (the bounded CAS retry is the net). A blocking wait with no
    ceiling could pin the long-lived MCP server behind a wedged peer fetch, so it is
    deliberately avoided here."""
    if fcntl is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        pass
    deadline = time.monotonic() + _FETCH_COORD_WAIT_S
    while time.monotonic() < deadline:
        _backoff_sleep(_jitter(_FETCH_COORD_POLL_S))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            continue
    return False


@contextmanager
def fetch_coordination_lock(repo_root: str) -> Iterator[None]:
    """Hold the exclusive cross-process fetch lock for *repo_root*'s Git COMMON dir.

    A ref-updating fetch WAITS (bounded) for a peer fetch on the same common dir rather than
    race it, because the loser of git's ref compare-and-swap gets no in-band recovery beyond
    the caller's retry. Keyed on the canonical (symlink/worktree-resolved) common dir so
    linked worktrees and the symlinked tickets clone share ONE lock.

    Best-effort throughout: if the common dir cannot be resolved (not a git repo), the lock
    file cannot be opened, or a wedged peer holds it past ``_FETCH_COORD_WAIT_S``, the block
    proceeds UNLOCKED — the caller's bounded compare-and-swap retry stays the correctness
    net, and that retry is also what covers a NON-rebar git peer, which never takes this lock
    at all."""
    common = _resolve_common_git_dir(repo_root)
    if common is None or fcntl is None:
        yield
        return
    lock_path = os.path.join(common, _FETCH_COORD_LOCK_NAME)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return
    acquired = False
    try:
        acquired = _acquire_fetch_coord_flock(fd)
        if not acquired:
            logger.warning(
                "fetch coordination lock %s still held after %.0fs; proceeding without "
                "serialization (the bounded ref-CAS retry still governs)",
                lock_path,
                _FETCH_COORD_WAIT_S,
            )
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# Test seam: a no-arg callable (default ``None`` = disabled) invoked inside
# ``_reclaim_if_stale_index_lock`` at the TOCTOU window — after the lock is judged stale and
# before the guarded unlink — so a test can deterministically inject a peer replacing the
# lock in that window (no sleeps). Production leaves this ``None``.
_reclaim_probe: Callable[[], None] | None = None


def _reclaim_if_stale_index_lock(tracker: str, *, force: bool = False) -> None:
    """Remove ``index.lock`` only when its staleness is proven.

    Unlocked callers require age beyond ``_INDEX_LOCK_STALE_S``. Missing, unreadable,
    or younger locks remain because deleting a peer's lock can corrupt the index.
    ``force=True`` is for callers already holding the exclusive store write lock,
    which proves that any Git index lock is orphaned and avoids the Mode B delay.
    Both modes revalidate device and inode before removal so a replacement survives.
    """
    git_dir = _resolve_tracker_git_dir(tracker)
    if not git_dir:
        return
    # git-marker-ok: the lock file's filesystem PATH, not a failure marker.
    lock_file = os.path.join(git_dir, "index.lock")
    try:
        st = os.stat(lock_file)
    except OSError:
        return  # no lock file (or unstat-able) → nothing to reclaim
    if not force and time.time() - st.st_mtime <= _INDEX_LOCK_STALE_S:
        return  # young/live lock (unlocked context) → never reclaim
    if _reclaim_probe is not None:
        _reclaim_probe()
    # Revalidate device, inode, and age at removal. If a peer replaced or refreshed
    # the file during the TOCTOU window, preserve its lock.
    try:
        st2 = os.stat(lock_file)
    except OSError:
        return  # already gone (a peer reclaimed it first) → nothing to do
    if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
        return  # replaced by a different file (a fresh lock) → do NOT remove
    if not force and time.time() - st2.st_mtime <= _INDEX_LOCK_STALE_S:
        return  # refreshed in place → now live → do NOT remove
    try:
        os.remove(lock_file)
    except OSError:
        pass


# Tests use this disabled-by-default seam to observe every attempt and release a
# planted lock after a confirmed failure without timer-based coordination.
_retry_probe: Callable[[int, subprocess.CompletedProcess], None] | None = None


def _with_index_lock_retry(
    tracker: str,
    run_once: Callable[[], subprocess.CompletedProcess],
    *,
    force_reclaim: bool = False,
) -> subprocess.CompletedProcess:
    """Run one index-mutating Git callable with bounded lock retry.

    Success or a non-lock failure returns immediately. Git lock signatures retry
    under the per-store advisory lock with jittered exponential backoff. Only a
    stale index lock is reclaimed. Callers can compose another retry policy inside
    *run_once* for a different signature.

    ``force_reclaim=True`` serves store writers that hold the exclusive write lock,
    which proves a remaining index lock is orphaned regardless of age. Bug 9305
    extends the budget to tens of seconds and includes ref-lock conflicts. A final
    conflict receives actionable stderr from :func:`_augment_lock_exhaustion`.
    """
    with _store_git_op_lock(tracker):
        result = run_once()
        if _retry_probe is not None:
            _retry_probe(1, result)
        for attempt in range(1, _INDEX_LOCK_ATTEMPTS):
            if result.returncode == 0:
                return result
            failure_text = result.stderr or result.stdout or ""
            if not _is_git_lock_error(failure_text):
                return result
            logger.debug(
                "git lock contention on %s (attempt %d/%d): retrying after jittered backoff",
                tracker,
                attempt,
                _INDEX_LOCK_ATTEMPTS,
            )
            if _is_index_lock_error(failure_text):
                _reclaim_if_stale_index_lock(tracker, force=force_reclaim)
            _backoff_sleep(_jitter(_lock_backoff_s(attempt)))
            result = run_once()
            if _retry_probe is not None:
                _retry_probe(attempt + 1, result)
    if result.returncode != 0 and _is_git_lock_error(result.stderr or result.stdout or ""):
        return _augment_lock_exhaustion(result)
    return result
