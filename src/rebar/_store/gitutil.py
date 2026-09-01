"""One shared ``git`` subprocess wrapper.

A leaf helper (stdlib only at module level — no ``rebar.*`` import at all) that
consolidates the dozen hand-rolled ``_git()`` wrappers that had
drifted into a different signature/return/error contract each. Every wrapper ran
the identical shape underneath — ``subprocess.run(["git", "-C", cwd, *args],
capture_output=True, text=True, …)`` — so :func:`run_git` is that shape once, and
each call site keeps its OWN return/error contract by adapting the returned
:class:`subprocess.CompletedProcess` locally (inspect ``returncode``/``stdout``,
raise its own exception, translate a timeout, …).

It also owns the tracker's shared FILESYSTEM primitives — ``_ticket_dirs`` and
``_dir_is_archived`` (``_resolve_tracker_git_dir`` moved to
:mod:`rebar._store.git_locking`, which anchors every lock path on it, and is
re-exported here). They previously lived in
``rebar._commands.fsck_repair``, which inverted the layering: this store-layer module had to
defer-import a command-layer *repair* module to answer 'where is the tracker's git dir', and
seven consumers reached into 'repair' for helpers that have nothing to do with repairing
(ticket b432-c9dc-c1b4-4a45). They are stdlib-only here; ``_dir_is_archived``'s reducer import
stays function-local so this module keeps its no-module-level-``rebar.*`` property.

NEVER ``shell=True`` — argv is a list, so a git argument can never be reinterpreted
by a shell. This helper does not redact: it returns the ``CompletedProcess``
verbatim, and any token/secret redaction stays where it already lives — in the
caller that formats ``stderr`` into a message.

**The git lock-contention policy lives in :mod:`rebar._store.git_locking`** (bounded
jittered retry + per-store advisory ``flock`` serialization, bug 9305-b42c): the
advisory locks, :func:`~rebar._store.git_locking._with_index_lock_retry` and the stale
``index.lock`` reclamation, together with the research and the reasoning that chose
them. :func:`run_git_write` composes that policy with the transient runner-FS retry
below; the names are re-exported here because callers and tests read them from this
module.
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
from rebar._store.ticket_layout import iter_ticket_dirs

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
    dirs = iter_ticket_dirs(tracker)
    if include_archived:
        return [d.ticket_id for d in dirs]
    return [d.ticket_id for d in dirs if not _dir_is_archived(d.path)]


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
    """:func:`run_git` with the watchdog timeout FOLDED INTO a synthetic failed result.

    This is the ONE place rebar's store builds git's rc-124 "timed out" outcome. Before it
    the same three-line ``try/except TimeoutExpired`` lived in four modules with three
    different timeout constants, and only one classifier treated the result as retriable —
    the drift this seam exists to make impossible. A hung git therefore fails the op
    cleanly (unwinding out of any lock the caller holds) rather than raising a
    :class:`subprocess.TimeoutExpired` the returncode-inspecting callers do not expect.

    The marker that RECOGNISES this result lives in :mod:`rebar._store.git_outcome` (it is
    a TRANSPORT-retriable row); constructing it lives here, where the shared runner is.
    ``check`` is always ``False``: a caller wanting an exception inspects the returncode.

    ``runner`` lets a module hand in ITS OWN late-bound ``run_git`` global so a test that
    patches ``<module>.run_git`` by name still intercepts the call — folding the timeout
    here must not quietly relocate a module's monkeypatch seam.
    """
    invoke = run_git if runner is None else runner
    try:
        return invoke(cwd, *args, check=False, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        argv = ["git", *args] if cwd is None else ["git", "-C", str(cwd), *args]
        return subprocess.CompletedProcess(argv, 124, "", f"git timed out after {timeout}s")


# The three runner-FS transient marker tables moved to :mod:`rebar._store.git_outcome`,
# which owns every git marker string in the store; the reasoning that chose each one travels
# with them. Re-exported here under their historical names because callers and tests read
# them from this module.
#   READ-side HEAD parse  — bug childsafe-special-springtail
#   READ-side object DB   — bug wrongful-chemic-squeaker
#   WRITE-side temp create — bugs vocal-dip-robin / brainy-floral-globefish, moved out of
#                            event_append by bug unheedful-custodial-bluebottle so EVERY
#                            caller of the shared write seam inherits the same self-heal
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


# Watchdog bound for the index-mutating store git ops routed through run_git_write
# (txn/compact/delete add/commit/rm). WATCHDOG-grade, not a latency budget (9305 research
# §2: "bounding a local git call is defensible watchdog-grade, not latency-grade"):
# deliberately NOT copied from the 30s/300s network values — these are small local
# add/commits on the tickets tree (sub-second normally), so 120s distinguishes "wedged
# fs/lock" from "slow" with ~100x margin while still freeing the store write lock a hung
# mount would otherwise hold forever. event_append keeps its own 30s (_GIT_TIMEOUT there).
_LOCAL_GIT_TIMEOUT = 120


# raw-git-ok: locked store seam internal
def run_git_write(
    tracker: str | os.PathLike[str],
    *args: str,
    check: bool = False,
    timeout: float = _LOCAL_GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """``run_git`` for an index-MUTATING op (``add``/``commit``/``reset``…), self-healing
    git's ``.git/index.lock`` / ref-lock contention AND the transient runner-FS git faults
    (the ``could not parse HEAD`` / ``bad object`` READ faults and the loose-object
    temp-create WRITE fault), serialized behind the per-store advisory git-op lock (bug 9305).
    Runs the op and, ONLY on a git lock-conflict signature, reclaims a provably-stale
    index.lock, backs off (jittered, bounded), and retries (see
    :func:`_with_index_lock_retry`); ONLY on a transient runner-FS signature, backs
    off and retries the identical (idempotent) op (see :func:`_with_transient_fault_retry`).
    The two compose — the lock conflict is the OUTER retry, the runner-FS transient the
    INNER — so each self-heals without interfering. A success or any OTHER failure returns
    at once, so a genuine error is unchanged; a lock conflict that outlives the bounded
    budget returns ONE actionable error naming the lock file. Each attempt is bounded by
    the :data:`_LOCAL_GIT_TIMEOUT` watchdog, folded into a synthetic rc-124 result (the
    ``event_append._run_git`` shape) so a hung filesystem fails the op cleanly instead of
    hanging the caller. ``check=True`` raises :class:`subprocess.CalledProcessError` on the
    final non-zero exit (default ``False`` so callers that inspect ``returncode`` / raise
    their own error get the result verbatim).

    Safe to route ANY tracker git op through: a read op never produces a lock or an
    object-write signature, so it simply never trips either retry.

    ``timeout`` overrides the :data:`_LOCAL_GIT_TIMEOUT` watchdog for callers that declare
    their own module-level bound (e.g. the s3 doctor's) — the bound travels with the caller
    rather than being silently replaced when it adopts this seam."""

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


# ── deferred auto-maintenance (bd66) ─────────────────────────────────────────────────────
# git >= 2.47 runs ``git maintenance run --auto`` FOREGROUND at the end of ``git commit`` (ADR
# 0051 forces it foreground so the repack serialises UNDER the store write lock). On a mature
# store past git's ``gc.auto`` threshold that inline repack is O(store), charged to the commit's
# tight bound (event_append's 30s ``_GIT_TIMEOUT``, c2ba), which SIGKILLs it mid-repack and loses
# the write. The fix SPLITS the two: this flag tuple suppresses auto-maintenance on the O(1)
# lock-held commit; ``event_commit_git.run_auto_maintenance`` replays it under the write lock.
_AUTOMAINT_OFF: tuple[str, ...] = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")


# ── stranded-index classification (bug 2fa6) ────────────────────────────────────────────
# The tickets branch has a KNOWN SHAPE: per-ticket event directories (several id styles —
# `b636-f31a-d590-4642`, `jira-reb-1001`) plus a small set of store dotfiles. Rather than
# pattern-match those styles, ASK THE BRANCH: a path whose top-level component is absent from
# HEAD's tree does not belong to the store at all.
#
# This matters because a stranded unmerged index blocks EVERY store write. Before this, the
# recovery had only two buckets — reconciler-regenerable (discard) and everything-else
# (refuse as ticket data) — so paths that were NEITHER wedged the store until a human
# intervened with raw git. That happened when a `git stash pop` in the tickets worktree
# applied a stash created in a SOURCE worktree (the stash stack is shared across worktrees),
# dropping `src/…` and `.rebar/…` into the store.


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
