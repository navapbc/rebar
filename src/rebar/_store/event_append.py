"""The locked event-commit transaction for the tickets store — and its self-heal.

In-process replacement for the bash write path
``ticket-append-event.sh`` -> ``write_commit_event`` -> ``_flock_stage_commit``: takes
a fully-composed event dict (the seam already builds ``{timestamp, uuid,
event_type, env_id, author, data}``), and under the unified write lock does the atomic
rename + ``git add`` + ``git commit``. ``write_and_push`` additionally runs the
best-effort push.

**Split by concern.** The write path's two leaf concerns live in their own modules and
are re-exported here so every existing import path and monkeypatch target keeps working:

- ``_store/event_prepare.py`` — pre-lock validation + CANONICAL serialisation + staging
  (:data:`EVENT_TYPES`, :class:`StoreError`, :func:`event_filename`,
  :func:`_prepare_event`). Runs before any lock; holds the byte-parity contract.
- ``_store/event_commit_git.py`` — the bounded, retry-composed git verbs every lock-held
  git child is issued through (:func:`_run_git`, :func:`_git_add`, :func:`_git_commit`, …).

What REMAINS here is the transaction itself — the three ``lock.write_lock`` bodies, batch
rollback, and the push / enrichment-drain hand-off — together with the commit-failure
SELF-HEAL (:func:`_recover_from_unmerged`, :func:`_recover_from_invalid_object`). The
self-heal stays with the transaction deliberately: it re-issues ``_git_add`` on its retry
path, and the store suite patches ``event_append._git_add`` ONCE expecting both the
happy-path add and the recovery re-add to observe it through this single module global.

**Scope — this is the LOCAL ticket-store write path.** The Jira reconciler
(``rebar_reconciler/``) is a separate *client* of this store; its inbound
commit-batcher is a Jira-sync internal, NOT the general local batch-write API. Do
not conflate the two — see ``docs/architecture.md`` "Two writers, one store".

Exit-code parity (surfaced as ``StoreError.returncode`` -> the seam's ``CommandError``):
``1`` lock timeout / atomic-rename failure / git-commit failure (distinct stderr each),
``75`` rebase/merge guard. Mirrors ``_flock_stage_commit`` (which maps its internal
2/3 to an external return 1).
"""

from __future__ import annotations

import logging
import os

# ``subprocess`` is re-exported (redundant alias): the store suite patches
# ``event_append.subprocess.run``, and it is the SAME cached module object the relocated
# verbs in ``event_commit_git`` call, so that seam still reaches them.
import subprocess as subprocess
from collections.abc import Callable, Iterable
from typing import Any

from rebar._store import lock as _lock
from rebar._store import staging as _staging

# Concern leaves, re-exported so every existing ``event_append.NAME`` binding — dotted,
# ``setattr``/``getattr`` by string, or through an import alias — resolves unchanged, and
# so the relocated verbs stay reachable through this module's globals for monkeypatching.
from rebar._store.event_commit_git import (
    _GIT_ADD_ATTEMPTS as _GIT_ADD_ATTEMPTS,
)
from rebar._store.event_commit_git import (
    _GIT_TIMEOUT as _GIT_TIMEOUT,
)
from rebar._store.event_commit_git import (
    _git_add,
    _git_commit,
    _git_commit_paths,
    _git_rm,
    _restore_paths,
    _run_git,
    _unstage,
)
from rebar._store.event_commit_git import (
    _is_transient_add_error as _is_transient_add_error,
)
from rebar._store.event_prepare import (
    EVENT_TYPES as EVENT_TYPES,
)
from rebar._store.event_prepare import (
    StoreError,
    _ensure_initialized,
    _prepare_event,
)
from rebar._store.event_prepare import (
    _validate_event as _validate_event,
)
from rebar._store.event_prepare import (
    canonical_bytes as canonical_bytes,
)
from rebar._store.event_prepare import (
    event_filename as event_filename,
)

# Shared index.lock self-healing (bug fix-indexlock-retry). ``_INDEX_LOCK_STALE_S`` is
# re-exported here (redundant alias) because a test reads ``event_append._INDEX_LOCK_STALE_S``.
from rebar._store.gitutil import _INDEX_LOCK_STALE_S as _INDEX_LOCK_STALE_S

# Used directly by the retained self-heal below — NOT part of the relocated verb set.
from rebar._store.gitutil import (
    _with_transient_fault_retry,
    discard_unmerged_paths,
    path_is_foreign_to_branch,
)
from rebar._store.lock import LockTimeout, RebaseGuard  # re-export for callers

_log = logging.getLogger(__name__)


def delete_events(tracker: str | os.PathLike, relpaths: Iterable[str], commit_msg: str) -> int:
    """Delete committed event files under the write lock, pathspec-scoped.

    The retention-prune counterpart to :func:`stage_and_commit`. Sidecar prunes remove
    older reducer-ignored events, and — like every other store write — they MUST serialize
    through the unified write lock rather than racing it with a raw ``git rm`` + whole-index
    ``git commit``. This acquires :func:`rebar._store.lock.write_lock`, checks the rebase
    guard, ``git rm``s exactly *relpaths*, and commits ONLY those paths
    (:func:`_git_commit_paths` — never a whole-index commit that could commit a concurrent
    writer's staged event under this message), riding out index.lock contention via the
    shared retry. On a commit failure the staged deletions are restored to HEAD.

    Returns the number of paths deleted (``0`` for an empty list — a no-op that takes no
    lock and makes no commit). Raises :class:`StoreError` (1), :class:`RebaseGuard` (75),
    or :class:`LockTimeout` (1), same as the canonical writer; callers wanting the
    best-effort sidecar posture keep their own ``try``/``except``."""
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)
    paths = [r for r in relpaths if r]
    if not paths:
        return 0
    with _lock.write_lock(tracker, dual_window=True, retries=_lock.write_path_retries()):
        _lock.check_no_rebase_in_progress(tracker)  # raises RebaseGuard (75)
        rm = _git_rm(tracker, paths)
        if rm.returncode != 0:
            rm_err = (rm.stderr or rm.stdout).strip()
            raise StoreError(
                "Error: git rm failed while holding lock" + (f": {rm_err}" if rm_err else ""),
                1,
            )
        commit = _git_commit_paths(tracker, commit_msg, paths)
        if commit.returncode != 0:
            _restore_paths(tracker, paths)  # leave the store as it was
            git_err = (commit.stderr or commit.stdout).strip()
            raise StoreError(
                "Error: git commit failed while holding lock" + (f": {git_err}" if git_err else ""),
                1,
            )
    return len(paths)


def stage_and_commit(
    tracker: str | os.PathLike,
    ticket_id: str,
    event: dict[str, Any],
    *,
    under_lock_check: Callable[[], None] | None = None,
) -> int:
    """Validate, canonical-stage, lock, atomic-rename, ``git add``+``commit``.

    Returns 0 on success; raises :class:`StoreError` (1), :class:`RebaseGuard` (75),
    or :class:`LockTimeout` (1) with the exact bash stderr."""
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)
    staged = _prepare_event(tracker, ticket_id, event)

    event_type = str(event["event_type"]).upper()
    commit_msg = f"ticket: {event_type} {ticket_id}"
    try:
        with _lock.write_lock(tracker, dual_window=True, retries=_lock.write_path_retries()):
            _lock.check_no_rebase_in_progress(tracker)  # raises RebaseGuard (75)
            if under_lock_check is not None:
                under_lock_check()
            try:
                staged.promote()  # atomic publish (dir+event together for a new ticket)
            except OSError as exc:
                raise StoreError("Error: atomic rename failed", 1) from exc
            add = _git_add(tracker, [staged.relative_path])
            if add.returncode != 0:
                # Check add's return code BEFORE running commit (audit 2.2): the commit
                # below commits the whole index, so running it after a failed add could
                # sweep unrelated staged residue in under THIS write's message. Reset the
                # index as well as unlinking the worktree file so the (possibly partially)
                # staged event cannot leak into the next successful write's commit.
                _unstage(tracker, staged.relative_path)
                _silent_unlink(staged.final_path)
                staged.unpublish()
                # Surface git's real stderr. The create path historically hid it behind
                # this generic message, leaving intermittent CI git races (bug edf7 —
                # "could not parse HEAD" / index.lock contention) undiagnosable; the
                # transition path (txn.py) already includes stderr. The recognizable
                # phrase is kept as a substring for anything matching on it.
                add_err = (add.stderr or add.stdout).strip()
                raise StoreError(
                    "Error: git commit failed while holding lock"
                    + (f": {add_err}" if add_err else ""),
                    1,
                )
            commit = _git_commit(tracker, commit_msg)
            if commit.returncode != 0:
                # A pre-existing unmerged (UU) index entry — e.g. a stranded stash/merge
                # conflict on a reconciler-regenerable .bridge_state/* file (bug 6818) —
                # makes git refuse the commit entirely. Self-heal regenerable paths to
                # HEAD and retry; surface an actionable error for a non-regenerable one.
                healed, detail = _recover_from_unmerged(tracker, [staged.relative_path], commit_msg)
                if not healed and detail is None:
                    # A poisoned index (an index entry whose object VANISHED — a gc repack or
                    # a partial write under pressure) makes git refuse this AND every later
                    # commit until the whole index is reset to HEAD (bug 4c1c / Mode D).
                    healed = _recover_from_invalid_object(
                        tracker, [staged.relative_path], commit_msg, commit.stderr or commit.stdout
                    )
                if not healed:
                    # Drop the staged blob from the index too (not just disk) so the failed
                    # event cannot be committed by the next successful write.
                    _unstage(tracker, staged.relative_path)
                    _silent_unlink(staged.final_path)
                    staged.unpublish()
                    git_err = (commit.stderr or commit.stdout).strip()
                    raise StoreError(
                        detail
                        or (
                            "Error: git commit failed while holding lock"
                            + (f": {git_err}" if git_err else "")
                        ),
                        1,
                    )
    except (RebaseGuard, LockTimeout):
        staged.discard()
        raise
    finally:
        staged.discard()  # no-op once published
    return 0


def batch_stage_and_commit(
    tracker: str | os.PathLike, items: Iterable[tuple[str, dict[str, Any]]]
) -> int:
    """Commit MANY events under ONE lock acquire + ONE ``git commit`` (all-or-nothing).

    *items* is an iterable of ``(ticket_id, event)`` pairs. Every event is validated
    and canonical-staged (the same byte contract as :func:`stage_and_commit`) BEFORE
    the lock is taken; then, holding the single write lock (I5) exactly ONCE, each
    staged temp is atomically renamed into its I2 path (``{ticket}/{ts}-{uuid}-{TYPE}
    .json``), a single ``git add`` stages them all, and ONE ``git commit`` seals the
    batch. Returns the number of events committed (``0`` for an empty batch — a no-op
    that takes no lock and makes no commit).

    **Batch atomicity is all-or-nothing per commit.** Because replay/dedup/union-merge
    /compaction key off each event's UUID and NOT commit boundaries, collapsing N
    commits into 1 is invisible to readers — but a *partial* batch is not. So any
    failure (validation, rename, ``git add``, or a non-recoverable ``git commit``)
    rolls the batch back completely: every already-renamed final is unstaged from the
    index AND unlinked from the worktree, leaving the store exactly as it was. A
    crash mid-batch (before the commit) leaves at most orphaned worktree files that
    are never in any commit; the next writer's ``git add``/``commit`` only touches its
    own paths, and a re-run re-emits the whole batch. The lock is NOT re-entrant, so
    this MUST acquire it once and loop the renames inside — it never calls
    :func:`stage_and_commit` per event.

    Raises :class:`StoreError` (1), :class:`RebaseGuard` (75), or :class:`LockTimeout`
    (1) with the exact bash stderr, same as the single-event path."""
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)

    # Validate + stage every event up front (fail fast, before the lock). On any
    # failure here, unlink the temps staged so far — nothing has been renamed yet.
    prepared: list[_staging.StagedEvent] = []
    swept_stale = False
    try:
        for ticket_id, event in items:
            staged = _prepare_event(tracker, ticket_id, event, sweep_stale=not swept_stale)
            prepared.append(staged)
            if staged.staging_dir is not None:
                swept_stale = True
    except BaseException:
        for staged in prepared:
            staged.discard()
        raise

    if not prepared:
        return 0

    commit_msg = f"ticket: batch {len(prepared)} events"
    relpaths = [staged.relative_path for staged in prepared]
    renamed: list[_staging.StagedEvent] = []  # already published
    try:
        with _lock.write_lock(tracker, dual_window=True, retries=_lock.write_path_retries()):
            _lock.check_no_rebase_in_progress(tracker)  # raises RebaseGuard (75)
            for staged in prepared:
                try:
                    staged.promote()  # atomic publish
                except OSError as exc:
                    _rollback_batch(tracker, renamed)
                    raise StoreError("Error: atomic rename failed", 1) from exc
                renamed.append(staged)
            add = _git_add(tracker, relpaths)
            if add.returncode != 0:
                _rollback_batch(tracker, renamed)
                add_err = (add.stderr or add.stdout).strip()
                raise StoreError(
                    "Error: git commit failed while holding lock"
                    + (f": {add_err}" if add_err else ""),
                    1,
                )
            commit = _git_commit(tracker, commit_msg)
            if commit.returncode != 0:
                healed, detail = _recover_from_unmerged(tracker, relpaths, commit_msg)
                if not healed and detail is None:
                    # Poisoned-index (vanished-object) self-heal — see stage_and_commit and
                    # _recover_from_invalid_object (bug 4c1c / Mode D).
                    healed = _recover_from_invalid_object(
                        tracker, relpaths, commit_msg, commit.stderr or commit.stdout
                    )
                if not healed:
                    _rollback_batch(tracker, renamed)
                    git_err = (commit.stderr or commit.stdout).strip()
                    raise StoreError(
                        detail
                        or (
                            "Error: git commit failed while holding lock"
                            + (f": {git_err}" if git_err else "")
                        ),
                        1,
                    )
    except (RebaseGuard, LockTimeout):
        for staged in prepared:
            staged.discard()
        raise
    finally:
        for staged in prepared:
            staged.discard()  # no-op once published
    return len(prepared)


def write_and_push(
    tracker: str | os.PathLike,
    ticket_id: str,
    event: dict[str, Any],
    *,
    under_lock_check: Callable[[], None] | None = None,
) -> int:
    """Locked canonical commit, then the best-effort push (mirrors write_commit_event)."""
    rc = stage_and_commit(tracker, ticket_id, event, under_lock_check=under_lock_check)
    from rebar._store import push

    canonical = _lock.canonical_tracker(tracker)
    push.push_tickets_branch(canonical)
    # Best-effort, fail-silent write-path nudge that an existing store is behind the
    # idempotent ensure-registry (epic odd-vortex-elbow / WS2). This is the single
    # choke point through which _seam.append_event (comment/tag/edit/link/set_*/sign)
    # and the composer create/edit/revert path funnel; the nudge NEVER affects the
    # (already-committed) write. Lazy import so the read path stays untouched.
    try:
        from rebar._store import ensures as _ensures

        _ensures.maybe_emit_pending_hint(canonical)
    except Exception:  # noqa: BLE001 — the hint must never fail a committed write
        pass
    # Opportunistic cross-ticket enrichment drain (epic only-crave-art / c1de): a cheap
    # gate that no-ops unless something is soaked. Best-effort — never fails the write.
    _maybe_enrich_drain(str(canonical))
    return rc


def batch_write_and_push(
    tracker: str | os.PathLike, items: Iterable[tuple[str, dict[str, Any]]]
) -> int:
    """Batched commit (:func:`batch_stage_and_commit`), then ONE best-effort push.

    The bulk analogue of :func:`write_and_push`: instead of one push per event, the
    whole batch commits under a single lock and a single push follows. An empty batch
    commits nothing and skips the push. Returns the number of events committed."""
    n = batch_stage_and_commit(tracker, items)
    if n:
        from rebar._store import push

        push.push_tickets_branch(_lock.canonical_tracker(tracker))
    return n


def _rollback_batch(tracker: str, renamed: list[_staging.StagedEvent]) -> None:
    """Undo a failed batch: unstage every already-published event from the index and
    unlink it from the worktree, so a partial batch leaves NO phantom event (neither
    staged nor committable by the next write) and no orphaned worktree file.

    Ticket 021d: a directory this batch itself published is then removed too, once its own
    event is gone — otherwise rolling back a failed create would re-create exactly the
    empty-ticket-directory debris the staging path exists to prevent."""
    for staged in renamed:
        _unstage(tracker, staged.relative_path)
    for staged in renamed:
        _silent_unlink(staged.final_path)
    for staged in renamed:
        staged.unpublish()


def _maybe_enrich_drain(tracker: str) -> None:
    """Ride the write path with the opportunistic enrichment drain gate. Fully isolated: a
    missing [agents] extra or any failure is a clean no-op (never fails the triggering write)."""
    try:
        from rebar.llm.enrich_drain import maybe_drain

        maybe_drain(tracker)
    except Exception:  # noqa: BLE001 — a drain concern must never fail a write; broad-but-swallowed
        pass


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# Reconciler-managed bridge-state files are REGENERABLE (the reconciler rebuilds them
# on its next pass; a missing/empty one just forces a full re-fetch), so a stranded
# conflict on them can be safely resolved to HEAD. Other paths are real ticket data.
_REGENERABLE_PREFIX = ".bridge_state/"


# raw-git-ok: locked store seam internal
def _recover_from_unmerged(
    tracker: str, event_relpaths: list[str], commit_msg: str
) -> tuple[bool, str | None]:
    """Recover a commit that ``git`` refused because of a PRE-EXISTING unmerged (UU)
    index entry (bug 6818). A stranded stash/merge conflict leaves an unmerged index
    that blocks EVERY ``git commit``, wedging all store writes.

    Returns ``(healed, detail)``:
    - ``(True, None)`` — every unmerged path was reconciler-regenerable OR FOREIGN to the
      tickets branch; they were cleared and the event commit was retried successfully.
    - ``(False, <actionable message>)`` — a path the branch DOES track is unmerged (real
      ticket data, never auto-discarded); the caller raises with that message.
    - ``(False, None)`` — no unmerged paths (the commit failed for another reason) or
      the retry still failed; the caller raises the generic error.
    """
    unmerged = _run_git(
        ["git", "-C", tracker, "diff", "--name-only", "--diff-filter=U"]
    ).stdout.split()
    if not unmerged:
        return (False, None)
    regen = [p for p in unmerged if p.startswith(_REGENERABLE_PREFIX)]
    rest = [p for p in unmerged if p not in regen]
    # A path the tickets branch does not track CANNOT be ticket data (bug 2fa6), so discard
    # it like a regenerable one rather than wedging every store write on it.
    foreign = [p for p in rest if path_is_foreign_to_branch(tracker, p)]
    ticket_data = [p for p in rest if p not in foreign]
    if ticket_data:
        return (
            False,
            "Error: git commit blocked by unmerged path(s) in the tracker index: "
            f"{', '.join(ticket_data)} — the tickets worktree has a stranded merge/stash "
            "conflict. Resolve it (e.g. `git -C <tracker> checkout HEAD -- <path>`) and retry.",
        )
    discard_unmerged_paths(tracker, regen, foreign)
    _git_add(tracker, list(event_relpaths))
    # Same runner-FS transient self-heals as _git_commit — this UU-recovery commit reads
    # HEAD and writes loose objects too, and must not lose a resolved write to a blip.
    retry = _with_transient_fault_retry(
        lambda: _run_git(["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg])
    )
    return (retry.returncode == 0, None)


# git ``write-tree`` refuses the commit with this signature when an index entry references
# an object that is MISSING from the object DB (``git commit`` builds a tree from the index's
# cached shas, and only ``write-tree`` verifies each blob EXISTS — corrupt CONTENT is not
# re-read at commit, so the trigger is a vanished object, not a garbled one).
_INVALID_OBJECT_MARKERS = ("invalid object", "error building trees")


def _is_invalid_object_error(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _INVALID_OBJECT_MARKERS)


def _staged_index_paths(tracker: str) -> list[str]:
    """Paths currently staged in the index. ``ls-files --cached`` reads ``.git/index``
    directly (no object access), so it is safe to call on a poisoned index."""
    r = _run_git(["git", "-C", tracker, "ls-files", "--cached"])
    return r.stdout.splitlines() if r.returncode == 0 else []


# raw-git-ok: locked store seam internal
def _recover_from_invalid_object(
    tracker: str, event_relpaths: list[str], commit_msg: str, commit_stderr: str
) -> bool:
    """Self-heal a commit git refused because the index references a MISSING object (bug
    4c1c / Mode D (residual of ac26) — the enrich-prune ``invalid object … Error building trees``
    cascade). A loose object that vanished between ``git add`` and ``git commit`` — a
    background gc repack, or a partial object write under FS/memory pressure — poisons the
    SHARED index, and because the failed commit leaves that entry staged, EVERY subsequent
    write's commit fails identically until it is dropped (proven cascade). The per-path
    ``_unstage`` cannot clear it: the poison belongs to an EARLIER write's path, not the
    current one, so each writer unstages the wrong path and the poison persists.

    Reset the WHOLE index to HEAD (``read-tree HEAD`` drops any poisoned entries regardless
    of which path they belong to, and touches ONLY the index — every worktree event file
    stays on disk, so no committed data is lost), re-stage THIS write's file(s) — which
    REGENERATES a missing object from the intact worktree file when the vanished object was
    this very write's — and retry the commit. Serialized under the write lock, the vanishing
    write is the first to hit its own poison, so it self-heals before any peer sees it.

    Returns ``True`` iff the retry committed; a non-invalid-object failure returns ``False``
    immediately (left for the caller to raise), leaving the index untouched."""
    if not _is_invalid_object_error(commit_stderr):
        return False
    # A poisoned index is an ANOMALY worth RECORDING, never a routine path: in the wild this
    # signature is as often a leaked GIT_INDEX_FILE or an external writer as it is a vanished
    # object, and a *recurring* heal points at hardware or a bug — so surface every heal
    # instead of silently papering over it. Capture the staged set first (ls-files reads the
    # index directly, so it is safe on a poisoned index) to name any file the reset orphans.
    staged_before = _staged_index_paths(tracker)
    # HEAD must exist for read-tree HEAD; the cascade only arises after prior writes, so it
    # always does. If it somehow doesn't, the retry commit simply fails → the caller raises.
    read_tree = _run_git(["git", "-C", tracker, "read-tree", "HEAD"])
    _git_add(tracker, list(event_relpaths))
    retry = _with_transient_fault_retry(
        lambda: _run_git(["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg])
    )
    healed = retry.returncode == 0
    # An orphan is an EARLIER failed write's file dropped from the index by the reset but left
    # in the worktree — visible to local replay yet uncommitted/unpushed. Name it so a real
    # local↔remote divergence is observable rather than silent (follow-up: reconcile it).
    orphaned = sorted(set(staged_before) - set(event_relpaths))
    _log.warning(
        "self-healed a poisoned index (invalid/missing object): read_tree_ok=%s healed=%s%s",
        read_tree.returncode == 0,
        healed,
        f" orphaned_worktree_paths={orphaned}" if orphaned else "",
    )
    return healed
