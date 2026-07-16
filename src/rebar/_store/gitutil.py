"""One shared ``git`` subprocess wrapper.

A leaf helper (stdlib only — ``os``/``subprocess``; imports nothing from
``rebar.*``) that consolidates the dozen hand-rolled ``_git()`` wrappers that had
drifted into a different signature/return/error contract each. Every wrapper ran
the identical shape underneath — ``subprocess.run(["git", "-C", cwd, *args],
capture_output=True, text=True, …)`` — so :func:`run_git` is that shape once, and
each call site keeps its OWN return/error contract by adapting the returned
:class:`subprocess.CompletedProcess` locally (inspect ``returncode``/``stdout``,
raise its own exception, translate a timeout, …).

NEVER ``shell=True`` — argv is a list, so a git argument can never be reinterpreted
by a shell. This helper does not redact: it returns the ``CompletedProcess``
verbatim, and any token/secret redaction stays where it already lives — in the
caller that formats ``stderr`` into a message.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping


def run_git(
    cwd: str | os.PathLike[str] | None,
    *args: str,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    input_data: str | bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git -C <cwd> <args…>`` and return the :class:`subprocess.CompletedProcess`.

    A thin, uniform wrapper over :func:`subprocess.run` for the tickets-store git
    plumbing. Defaults match the historical wrappers' common shape (capture stdout
    and stderr, decode as text). ``check=True`` raises
    :class:`subprocess.CalledProcessError` on a non-zero exit (call sites that
    inspect ``returncode`` or raise their own error pass ``check=False``);
    ``timeout`` (when set) lets :class:`subprocess.TimeoutExpired` propagate — a
    caller that wants a timeout folded into a synthetic failed result catches it
    itself. ``env=None`` inherits the current environment.

    ``cwd=None`` omits the ``-C <cwd>`` prefix entirely, running ``git`` in the
    process CWD (some callers verify commits relative to the caller's directory
    rather than a fixed repo). ``input_data`` (when set) is fed to git's stdin —
    forwarded to :func:`subprocess.run`'s ``input`` for e.g. ``git hash-object``.

    Contract note: with ``text=True`` (the default), ``input_data`` must be ``str`` —
    :func:`subprocess.run` encodes text-mode stdin. Passing ``bytes`` with ``text=True``
    would otherwise fail deep in the stdlib with an opaque ``AttributeError: 'bytes'
    object has no attribute 'encode'``; this wrapper raises a clear :class:`TypeError`
    instead. Binary stdin requires ``text=False`` (then stdout/stderr are ``bytes`` too).
    """
    if text and isinstance(input_data, bytes):
        raise TypeError(
            "run_git: bytes input_data requires text=False (binary stdin cannot be "
            "encoded in text mode); pass text=False for binary stdin, or a str for text mode."
        )
    argv = ["git", *args] if cwd is None else ["git", "-C", cwd, *args]
    return subprocess.run(
        argv,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
        input=input_data,
    )


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
_INDEX_LOCK_ATTEMPTS = 5
_INDEX_LOCK_BACKOFF_S = 0.2  # gap = base * attempt → ~2s summed over the 4 inter-attempt gaps


def _is_index_lock_error(text: str) -> bool:
    """True if *text* is git's index.lock-contention signature (case-insensitive)."""
    low = text.lower()
    return "index.lock" in low and ("file exists" in low or "another git process" in low)


# Test seam: a no-arg callable (default ``None`` = disabled) invoked inside
# ``_reclaim_if_stale_index_lock`` at the TOCTOU window — after the lock is judged stale and
# before the guarded unlink — so a test can deterministically inject a peer replacing the
# lock in that window (no sleeps). Production leaves this ``None``.
_reclaim_probe: Callable[[], None] | None = None


def _reclaim_if_stale_index_lock(tracker: str) -> None:
    """Remove the tracker's git ``index.lock`` ONLY IF provably stale (mtime age >
    ``_INDEX_LOCK_STALE_S``). Best-effort and safe: a young/live lock (age <= threshold,
    or unstat-able, or absent) is LEFT in place — removing a lock a live peer holds can
    corrupt the index. Reuses fsck's git-dir resolution so the same lock path is meant."""
    from rebar._commands.fsck import _resolve_tracker_git_dir

    git_dir = _resolve_tracker_git_dir(tracker)
    if not git_dir:
        return
    lock_file = os.path.join(git_dir, "index.lock")
    try:
        st = os.stat(lock_file)
    except OSError:
        return  # no lock file (or unstat-able) → nothing to reclaim
    if time.time() - st.st_mtime <= _INDEX_LOCK_STALE_S:
        return  # young/live lock → never reclaim
    if _reclaim_probe is not None:
        _reclaim_probe()
    # Re-validate identity (device+inode) AND age at the moment of removal: a peer may
    # have removed our stale lock and dropped a fresh LIVE one at the same path in the
    # window since the stat above (the TOCTOU). Only unlink if it is STILL the same file
    # AND still stale — otherwise abort, leaving the peer's fresh lock intact.
    try:
        st2 = os.stat(lock_file)
    except OSError:
        return  # already gone (a peer reclaimed it first) → nothing to do
    if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
        return  # replaced by a different file (a fresh lock) → do NOT remove
    if time.time() - st2.st_mtime <= _INDEX_LOCK_STALE_S:
        return  # refreshed in place → now live → do NOT remove
    try:
        os.remove(lock_file)
    except OSError:
        pass


# Test seam: a callable (default ``None`` = disabled) invoked after EVERY ``run_once()``
# attempt inside ``_with_index_lock_retry`` with ``(attempt_number, result)`` — the initial
# pre-loop call fires as attempt 1, each in-loop retry as 2, 3, …. It lets a test count the
# real attempts and release a planted lock ONLY after the first failure is confirmed (a
# deterministic alternative to timer-based lock release). Production leaves this ``None`` so
# the call is skipped and behavior is unchanged.
_retry_probe: Callable[[int, subprocess.CompletedProcess], None] | None = None


def _with_index_lock_retry(
    tracker: str, run_once: Callable[[], subprocess.CompletedProcess]
) -> subprocess.CompletedProcess:
    """Run *run_once* (an index-mutating git invocation), retrying ONLY the index.lock
    contention signature with a bounded backoff. On success or a NON-lock failure the
    result is returned immediately (behavior unchanged — a real error still surfaces at
    once). Between lock retries a provably-stale lock is reclaimed; a young lock that
    never releases exhausts the attempts and its final failing result is returned. This
    is the composition seam: a caller that also retries a DIFFERENT signature (e.g.
    event_append's object-DB ``git add`` retry) passes its own inner loop as *run_once*."""
    result = run_once()
    if _retry_probe is not None:
        _retry_probe(1, result)
    for attempt in range(1, _INDEX_LOCK_ATTEMPTS):
        if result.returncode == 0:
            return result
        if not _is_index_lock_error(result.stderr or result.stdout or ""):
            return result
        _reclaim_if_stale_index_lock(tracker)
        time.sleep(_INDEX_LOCK_BACKOFF_S * attempt)
        result = run_once()
        if _retry_probe is not None:
            _retry_probe(attempt + 1, result)
    return result


# git intermittently fails to READ ``HEAD`` on CI runners during an index-mutating op that
# must resolve it first — most often ``git commit``, which parses HEAD (``parse_commit``) to
# set the new commit's parent. Under a SHARED tracker worktree the HEAD commit's loose object
# in ``.git/objects/`` is transiently unreadable (a runner-FS read hiccup, NOT data
# corruption), and git aborts with ``fatal: could not parse HEAD`` (exit 128) BEFORE writing
# anything. It is the READ-side analogue of event_append's WRITE-side object-DB ``git add``
# transient (``_TRANSIENT_ADD_MARKERS``): the identical invocation succeeds on retry — a
# re-run on the same state passes (a Gerrit ``recheck`` on the same patchset goes green) — so
# retrying ONLY this signature turns a runner-FS blip from a hard write loss (which red-lights
# unrelated CI) into a self-healed write. The op is safe to re-run because it failed at the
# HEAD-parse step before mutating the store (idempotent). Kept a tight, greppable marker set
# (only the proven ``could not parse HEAD`` signature) so it never masks a genuine error.
# Bug childsafe-special-springtail.
_TRANSIENT_HEAD_MARKERS = ("could not parse head",)
_TRANSIENT_HEAD_ATTEMPTS = 3
_TRANSIENT_HEAD_BACKOFF_S = 0.1


def _is_transient_head_error(text: str) -> bool:
    """True if *text* is git's transient HEAD-parse read signature (case-insensitive)."""
    low = text.lower()
    return any(marker in low for marker in _TRANSIENT_HEAD_MARKERS)


def _with_transient_head_retry(
    run_once: Callable[[], subprocess.CompletedProcess],
) -> subprocess.CompletedProcess:
    """Run *run_once* (an idempotent index-mutating git invocation), retrying ONLY the
    transient ``could not parse HEAD`` read signature with a bounded backoff. On success or
    a NON-transient failure the result is returned immediately (behavior unchanged — a real
    error still surfaces at once). The retried invocation MUST be idempotent: git fails at
    the HEAD-parse step before writing anything, so re-running the SAME op is safe. This is
    the INNER composition loop — :func:`run_git_write` wraps it in :func:`_with_index_lock_retry`
    (index.lock is the OUTER retry, this HEAD-parse transient the inner)."""
    result = run_once()
    for attempt in range(1, _TRANSIENT_HEAD_ATTEMPTS):
        if result.returncode == 0:
            return result
        if not _is_transient_head_error(result.stderr or result.stdout or ""):
            return result
        time.sleep(_TRANSIENT_HEAD_BACKOFF_S * attempt)
        result = run_once()
    return result


def run_git_write(
    tracker: str | os.PathLike[str],
    *args: str,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """``run_git`` for an index-MUTATING op (``add``/``commit``/``reset``…), self-healing
    git's ``.git/index.lock`` contention AND the transient ``could not parse HEAD`` read
    fault. Runs the op and, ONLY on the index.lock signature, reclaims a provably-stale lock,
    backs off, and retries (see :func:`_with_index_lock_retry`); ONLY on the transient
    HEAD-parse read signature, backs off and retries the identical (idempotent) op (see
    :func:`_with_transient_head_retry`). The two compose — index.lock is the OUTER retry, the
    HEAD-parse transient the INNER — so each self-heals without interfering. A success or any
    OTHER failure returns at once, so a genuine error is unchanged. ``check=True`` raises
    :class:`subprocess.CalledProcessError` on the final non-zero exit (default ``False``
    so callers that inspect ``returncode`` / raise their own error get the result verbatim).

    Safe to route ANY tracker git op through: index.lock and the HEAD-parse transient only
    appear on index-mutating commands, so a read op simply never trips either retry."""
    result = _with_index_lock_retry(
        str(tracker),
        lambda: _with_transient_head_retry(lambda: run_git(tracker, *args, check=False)),
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", *args] if tracker is None else ["git", "-C", str(tracker), *args],
            result.stdout,
            result.stderr,
        )
    return result
