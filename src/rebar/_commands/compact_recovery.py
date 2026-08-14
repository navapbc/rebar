"""Crash-atomic compaction: the INTENT JOURNAL (commit-pending sentinel) and the
recovery preamble that converges an abandoned partial fold.

Bug ``compulsory-pernickety-mantis`` (epic ``becoming-berserk-grunion``): the fold mutates
the SHARED tracker worktree in steps — SNAPSHOT write, ``*.retired`` renames, ``git add`` +
commit — and a detached worker that dies between the first mutation and the commit leaves
the tree DIRTY. A dirty tree makes ``sync._union_merge``'s ``git merge origin/tickets``
abort, stranding every session's ticket commits off origin.

The sentinel makes the fold crash-atomic across process death, where ``try``/``except``
cannot reach: before its FIRST worktree mutation the fold journals an intent record —
ticket id, snapshot path, folded source list — OUTSIDE the worktree (under the tracker's
git dir, so the sentinel itself can never be tree dirt), and discards it on every completed
outcome. The recovery preamble (run under the store write lock at sweep/fold start) then
discriminates the half-fold states:

* a live journal whose snapshot exists on disk and is NOT in HEAD ⇒ an abandoned partial
  fold ⇒ REVERT (rename the ``*.retired`` sources back, unlink the uncommitted snapshot,
  unstage the ticket dir);
* a journal whose snapshot is already in HEAD ⇒ the crash hit between the commit and the
  journal discard, the fold LANDED ⇒ discard the journal, touch nothing;
* a journal whose snapshot is gone from disk ⇒ another owner (``fsck --repair-snapshots``)
  superseded the state ⇒ discard the journal, touch nothing;
* the intentionally-retained SNAPSHOT_INCONSISTENT state that
  ``compact_txn._retire_folded_sources`` keeps for ``fsck --repair-snapshots`` has NO
  journal by construction, so recovery never sees it — fsck remains its sole owner.

Layering: this module imports only ``rebar._store``; ``compact_txn`` and ``compact`` both
import it (never the reverse).
"""

from __future__ import annotations

import json
import logging
import os

from rebar._store import fsutil, lock
from rebar._store.gitutil import run_git_write
from rebar.reducer._cache import RETIRED_SUFFIX

logger = logging.getLogger(__name__)

#: Journal directory under the tracker's resolved git dir — outside the worktree.
_INTENT_DIR_NAME = "rebar-compact-intents"


# raw-git-ok: store-maintenance recovery, seam-internal
def _git(tracker: str, *args: str):
    return run_git_write(tracker, *args, check=False)


def _intent_dir(tracker: str) -> str | None:
    """The journal directory for *tracker*, or ``None`` when its git dir cannot be
    resolved (an uninitialized store — there is nothing to journal or recover there)."""
    gitdir = lock._gitdir(str(tracker))
    if gitdir is None:
        return None
    return os.path.join(gitdir, _INTENT_DIR_NAME)


def write_intent(tracker: str, ticket_id: str, snapshot_path: str, fold_files: list[str]) -> str:
    """Journal the fold that is ABOUT to mutate the worktree; returns the journal path.

    Written atomically (temp + rename) and fsynced: the sentinel must be durable before
    the first mutation it guards. Raises :class:`OSError` when it cannot be written — the
    caller refuses to fold without its crash sentinel, because an unjournaled crash is
    exactly the wedge this exists to prevent. Paths are stored tracker-relative so a
    tracker reached via a different mount/symlink still recovers."""
    intent_dir = _intent_dir(tracker)
    if intent_dir is None:
        raise OSError(f"cannot resolve the git dir for tracker {tracker}")
    os.makedirs(intent_dir, exist_ok=True)
    record = {
        "ticket_id": ticket_id,
        "snapshot": os.path.relpath(snapshot_path, tracker),
        "sources": [os.path.relpath(fp, tracker) for fp in fold_files],
    }
    path = os.path.join(intent_dir, f"{ticket_id}.json")
    fsutil.atomic_write(path, json.dumps(record, indent=2), fsync=True)
    return path


def discard(journal_path: str | None) -> None:
    """Remove a journal (idempotent, best-effort). A leaked journal is safe — recovery
    re-examines it and discards it once its snapshot is committed or superseded."""
    if not journal_path:
        return
    try:
        os.remove(journal_path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("compact: could not discard intent journal %s", journal_path)


def pending_intents(tracker: str) -> list[str]:
    """Every intent journal currently on disk for *tracker* (sorted; empty on any error)."""
    intent_dir = _intent_dir(tracker)
    if intent_dir is None:
        return []
    try:
        names = os.listdir(intent_dir)
    except OSError:
        return []
    return sorted(os.path.join(intent_dir, n) for n in names if n.endswith(".json"))


def _snapshot_in_head(tracker: str, rel_snapshot: str) -> bool:
    return _git(tracker, "cat-file", "-e", f"HEAD:{rel_snapshot}").returncode == 0


def _revert_partial_fold(tracker: str, record: dict) -> None:
    """Revert one abandoned partial fold to the pre-fold state: rename every retired
    source back (only where the original is missing — idempotent), unlink the uncommitted
    snapshot, and unstage the ticket dir (a crash between ``git add`` and ``git commit``
    leaves a dirty INDEX that would also abort the union merge)."""
    for rel in record.get("sources", []):
        original = os.path.join(tracker, rel)
        retired = original + RETIRED_SUFFIX
        if os.path.exists(retired) and not os.path.exists(original):
            try:
                os.rename(retired, original)
            except OSError:
                logger.warning("compact recovery: could not restore %s", original)
    snapshot = os.path.join(tracker, record.get("snapshot", ""))
    try:
        os.remove(snapshot)
    except OSError:
        logger.warning("compact recovery: could not remove abandoned snapshot %s", snapshot)
    ticket_id = record.get("ticket_id", "")
    if ticket_id:
        _git(tracker, "reset", "-q", "--", f"{ticket_id}/")


def recover_abandoned_folds(tracker: str) -> int:
    """Converge every abandoned partial fold; the caller MUST hold the store write lock.

    Returns the number of folds reverted. See the module docstring for the state
    discrimination table; the one non-obvious rule is that a journal only triggers a
    revert while its recorded snapshot both exists on disk AND is absent from HEAD — the
    unique signature of a fold that died before its commit."""
    reverted = 0
    for journal in pending_intents(tracker):
        try:
            with open(journal, encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            logger.warning("compact recovery: unreadable intent journal %s — discarding", journal)
            discard(journal)
            continue
        rel_snapshot = record.get("snapshot", "")
        snapshot_path = os.path.join(tracker, rel_snapshot) if rel_snapshot else ""
        if (
            rel_snapshot
            and os.path.exists(snapshot_path)
            and not _snapshot_in_head(tracker, rel_snapshot)
        ):
            logger.warning(
                "compact recovery: reverting abandoned partial fold for %s",
                record.get("ticket_id", "<unknown>"),
            )
            _revert_partial_fold(tracker, record)
            reverted += 1
        discard(journal)
    return reverted


def recover_abandoned_folds_locked(tracker: str) -> int:
    """:func:`recover_abandoned_folds` behind its own store write lock acquisition.

    The pending probe runs FIRST so the common case (no journals — every fold completed)
    costs one ``listdir`` and takes no lock at all. A lock that cannot be acquired defers
    recovery to the next caller rather than failing the operation that ran the preamble —
    the journal survives, so nothing is lost by waiting."""
    if not pending_intents(tracker):
        return 0
    try:
        handle = lock.acquire(tracker, timeout=30, attempts=2, dual_window=True)
    except Exception:
        # A busy/incompatible store defers recovery — it never fails the caller.
        logger.warning("compact recovery: store lock unavailable; deferring", exc_info=True)
        return 0
    try:
        return recover_abandoned_folds(tracker)
    finally:
        handle.release()
