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
    """
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
    for attempt in range(1, _INDEX_LOCK_ATTEMPTS):
        if result.returncode == 0:
            return result
        if not _is_index_lock_error(result.stderr or result.stdout or ""):
            return result
        _reclaim_if_stale_index_lock(tracker)
        time.sleep(_INDEX_LOCK_BACKOFF_S * attempt)
        result = run_once()
    return result


def run_git_write(
    tracker: str | os.PathLike[str],
    *args: str,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """``run_git`` for an index-MUTATING op (``add``/``commit``/``reset``…), self-healing
    git's ``.git/index.lock`` contention. Runs the op and, ONLY on the index.lock
    signature, reclaims a provably-stale lock, backs off, and retries up to the attempt
    cap (see :func:`_with_index_lock_retry`). A success or any non-lock failure returns
    at once, so a genuine error is unchanged. ``check=True`` raises
    :class:`subprocess.CalledProcessError` on the final non-zero exit (default ``False``
    so callers that inspect ``returncode`` / raise their own error get the result verbatim).

    Safe to route ANY tracker git op through: index.lock only appears on index-mutating
    commands, so a read op simply never trips the retry."""
    result = _with_index_lock_retry(str(tracker), lambda: run_git(tracker, *args, check=False))
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", *args] if tracker is None else ["git", "-C", str(tracker), *args],
            result.stdout,
            result.stderr,
        )
    return result
