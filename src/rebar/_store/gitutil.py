"""Provide shared Git subprocess and ticket-store filesystem helpers.

:func:`run_git` consolidates store Git wrappers while leaving each caller to
interpret :class:`subprocess.CompletedProcess`. Argument lists are never executed
with ``shell=True``. Results are returned without redaction because callers own
diagnostic formatting and secret removal.

This module also owns ``_ticket_dirs`` and ``_dir_is_archived``. Moving those
filesystem primitives out of command-layer repair code restores store-layer
dependency direction (ticket b432-c9dc-c1b4-4a45). The reducer import remains
function-local. :mod:`rebar._store.git_locking` owns lock paths, advisory locking,
bounded jittered retry, and stale index-lock recovery. Re-exported names preserve
existing callers and tests. :func:`run_git_write` composes that policy with the
transient filesystem retry defined here.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Mapping

from rebar._store import git_outcome
from rebar._store.git_locking import (  # noqa: F401  (compat re-export — see the docstring)
    _INDEX_LOCK_STALE_S,
    _augment_lock_exhaustion,
    _backoff_sleep,
    _is_git_lock_error,
    _is_index_lock_error,
    _jitter,
    _lock_retry_budget_s,
    _reclaim_if_stale_index_lock,
    _resolve_tracker_git_dir,
    _store_git_lock_path,
    _store_git_op_lock,
    _with_index_lock_retry,
    fetch_coordination_lock,
)

logger = logging.getLogger(__name__)


# ── Tracker filesystem primitives ────────────────────────────────────────────
# Shared by fsck's diagnostic + repair paths, compact, bridge_repair,
# tracker_maintenance and this module's own lock handling. Pure filesystem: no git
# subprocess, no rebar module-level import (ticket b432-c9dc-c1b4-4a45).


def _dir_is_archived(ticket_path: str) -> bool:
    """True only when the ``.archived`` marker exists AND the event log net-confirms archival.

    The marker is a fast-path cache, never the decision: a stale marker (reverted archive, or
    a marker written without an ARCHIVED event) must not hide the ticket from store walks, so
    the log check (:func:`rebar.reducer._api._is_net_archived` — ARCHIVED uuids minus
    REVERT-targeted uuids) always confirms before a dir is skipped.

    The reducer import is deliberately function-local: it keeps this module free of any
    module-level ``rebar.*`` import, so no consumer can create an import cycle through it."""
    if not os.path.exists(os.path.join(ticket_path, ".archived")):
        return False
    from rebar.reducer._api import _is_net_archived

    return _is_net_archived(ticket_path)


def _ticket_dirs(tracker: str, *, include_archived: bool = False) -> list[str]:
    """The shared store-walk iterator: sorted ticket dirs, ACTIVE-only by default.

    Skips hidden dirs (.git, .bridge_state, …): the bash `"$TRACKER_DIR"/*/` glob never
    matched dot-dirs, and ticket ids never start with '.'. Archived tickets are excluded
    unless ``include_archived`` — an archive is terminal (the fold at archive time leaves no
    unfolded tail), so maintenance walks cost store ACTIVITY, not store history."""
    dirs = sorted(
        d
        for d in os.listdir(tracker)
        if not d.startswith(".") and os.path.isdir(os.path.join(tracker, d))
    )
    if include_archived:
        return dirs
    return [d for d in dirs if not _dir_is_archived(os.path.join(tracker, d))]


# raw-git-ok: locked store seam internal
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


# raw-git-ok: locked store seam internal
def run_git_bounded(
    cwd: str | os.PathLike[str] | None,
    *args: str,
    timeout: float,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> subprocess.CompletedProcess:
    """Run Git with timeout folded into a failed result with code ``124``.

    This is the sole constructor of the store's synthetic timeout outcome. A hung
    process therefore unwinds caller locks through existing return-code handling.
    :mod:`rebar._store.git_outcome` owns its transport-retry marker. ``check``
    remains false. A supplied late-bound *runner* preserves module monkeypatch seams.
    """
    invoke = run_git if runner is None else runner
    try:
        return invoke(cwd, *args, check=False, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        argv = ["git", *args] if cwd is None else ["git", "-C", str(cwd), *args]
        return subprocess.CompletedProcess(argv, 124, "", f"git timed out after {timeout}s")


# ``git_outcome`` owns transient HEAD-read, object-read, and object-write markers.
# Historical re-exports preserve callers and give every write path the same recovery.
_TRANSIENT_HEAD_MARKERS = git_outcome.TRANSIENT_HEAD_MARKERS
_TRANSIENT_OBJECT_MARKERS = git_outcome.TRANSIENT_OBJECT_MARKERS
_TRANSIENT_WRITE_MARKERS = git_outcome.TRANSIENT_WRITE_MARKERS
_TRANSIENT_FAULT_ATTEMPTS = 3
_TRANSIENT_FAULT_BACKOFF_S = 0.1


def is_transient_object_read_error(text: str) -> bool:
    """True if *text* is git's transient object-DB read signature (case-insensitive).

    Exposed so a caller that must distinguish this fault in the error it raises — an
    unreadable object is not a data conflict, and sends the operator to a different tool —
    classifies it against the SAME marker set the retry uses, never a second private copy."""
    return git_outcome.is_transient_object_read(text)


def _is_transient_object_write_error(text: str) -> bool:
    """True if *text* is git's transient object-DB WRITE signature (case-insensitive) — the
    loose-object temp-create fault of :data:`_TRANSIENT_WRITE_MARKERS`.

    Module-private, unlike the read-side predicate: that one is public because a production
    caller (the s3 doctor) folds a hint from it into the error it raises, and no caller
    classifies the write fault that way today. ``event_append`` re-exports this under its own
    historical name so there is still exactly ONE marker definition."""
    return git_outcome.is_transient_object_write(text)


def _is_transient_git_fault(text: str) -> bool:
    """True if *text* is any transient runner-FS git signature (case-insensitive): the
    READ-side HEAD-parse and ``bad object`` faults, or the WRITE-side loose-object
    temp-create fault. A lookup against the shared registry."""
    return git_outcome.is_transient_fs(text)


def _with_transient_fault_retry(
    run_once: Callable[[], subprocess.CompletedProcess],
    *,
    attempts: int = _TRANSIENT_FAULT_ATTEMPTS,
) -> subprocess.CompletedProcess:
    """Run *run_once* (an idempotent git invocation), retrying ONLY the transient runner-FS
    signatures of :func:`_is_transient_git_fault` with a bounded backoff. On success or a
    NON-transient failure the result is returned immediately (behavior unchanged — a real
    error still surfaces at once), and a transient one that outlives *attempts* returns its
    failing result, so a persistent fault still fails loudly. The retried invocation MUST be
    idempotent: the READ faults abort before anything is written, and re-running a
    content-addressed object write re-writes the same objects. This is the INNER composition
    loop — :func:`run_git_write` wraps it in :func:`_with_index_lock_retry` (index.lock is the
    OUTER retry, the runner-FS transient the inner)."""
    result = run_once()
    for attempt in range(1, attempts):
        if result.returncode == 0:
            return result
        if not _is_transient_git_fault(result.stderr or result.stdout or ""):
            return result
        _backoff_sleep(_TRANSIENT_FAULT_BACKOFF_S * attempt)
        result = run_once()
    return result


# This watchdog bounds local index mutations rather than response latency. Its
# 120-second margin distinguishes a wedged filesystem from an ordinary small commit
# while releasing the write lock eventually. Network and event-append bounds differ.
_LOCAL_GIT_TIMEOUT = 120


# raw-git-ok: locked store seam internal
def run_git_write(
    tracker: str | os.PathLike[str],
    *args: str,
    check: bool = False,
    timeout: float = _LOCAL_GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run a store Git operation with bounded write-path recovery.

    An outer retry handles index and ref lock contention under the per-store
    advisory lock (bug 9305). An inner retry handles idempotent transient HEAD,
    object-read, and object-write failures. Success and unrelated errors return
    immediately. Exhausted lock contention adds an actionable diagnostic. Each
    attempt uses ``_LOCAL_GIT_TIMEOUT``, with timeout folded into result code ``124``.

    ``check=True`` raises :class:`subprocess.CalledProcessError` for the final
    failure. Read operations can also use this seam because they do not match write
    recovery signatures. Callers may override the watchdog with *timeout*.
    """

    def _bounded_once() -> subprocess.CompletedProcess:
        return run_git_bounded(tracker, *args, timeout=timeout)

    result = _with_index_lock_retry(
        str(tracker),
        lambda: _with_transient_fault_retry(_bounded_once),
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", *args] if tracker is None else ["git", "-C", str(tracker), *args],
            result.stdout,
            result.stderr,
        )
    return result


# Git 2.47 can run foreground auto-maintenance after commit. Suppress that
# store-wide work inside the short commit bound, then replay it under the write lock
# through ``event_commit_git.run_auto_maintenance`` (ADR 0051, bug bd66).
_AUTOMAINT_OFF: tuple[str, ...] = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")


# Classify stranded paths by asking whether HEAD tracks their top-level component.
# This avoids guessing ticket identifier shapes. A source-worktree stash can place
# foreign paths in the shared tracker index, where treating them as ticket data would
# block every store write (bug 2fa6).


def path_is_foreign_to_branch(tracker: str, path: str) -> bool:
    """True when ``path``'s top-level component is not tracked on the checked-out branch.

    Such a path CANNOT be ticket data, so a stranded conflict on it is safe to discard.
    Conservative by construction: anything the branch does track is treated as store data and
    left for a human. Fails CLOSED (returns False) if git cannot answer.
    """
    top = path.split("/", 1)[0]
    if not top:
        return False
    probe = run_git(tracker, "cat-file", "-e", f"HEAD:{top}", check=False)
    return probe.returncode != 0


# raw-git-ok: locked store seam internal — this is the store's OWN stranded-index recovery,
# invoked from event_append under the write lock. It is the sanctioned alternative to an
# operator running `git rm` / `git checkout` in the tracker by hand, which is what this bug
# (2fa6) exists to design out.
def discard_unmerged_paths(tracker: str, regenerable: list[str], foreign: list[str]) -> None:
    """Clear stranded unmerged entries: drop every stage from the index, then restore the
    REGENERABLE ones from HEAD (the reconciler rebuilds their content) and delete the FOREIGN
    ones outright (HEAD has no copy to restore — they never belonged to this branch)."""
    both = [*regenerable, *foreign]
    if not both:
        return
    run_git(  # raw-git-ok: locked store seam internal
        tracker, "rm", "-q", "--cached", "--", *both, check=False
    )
    if regenerable:
        run_git(  # raw-git-ok: locked store seam internal
            tracker, "checkout", "HEAD", "--", *regenerable, check=False
        )
    for rel in foreign:
        try:
            os.remove(os.path.join(tracker, rel))
        except OSError:
            pass
