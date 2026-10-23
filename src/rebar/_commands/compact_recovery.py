"""Recover interrupted folds through an intent journal outside the worktree.
Before mutation, record snapshot and sources. Under the write lock,
revert uncommitted snapshots, discard journals for committed or replaced
snapshots, and leave retained inconsistent states for fsck repair."""

from __future__ import annotations

import json
import logging
import os

from rebar._commands import compact_plan
from rebar._store import fsutil, lock
from rebar._store.gitutil import run_git_write

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


def _snapshot_in_head(tracker: str, rel_snapshot: str) -> bool | None:
    """Whether the journalled snapshot is committed in the tracker's HEAD.

    ``None`` means INDETERMINATE: HEAD itself cannot be interrogated (an unresolvable
    HEAD, or a probe that timed out — ``run_git_write`` folds its watchdog into a
    synthetic rc-124 result). ``run_git_write(check=False)`` also fails non-zero on
    transient faults, and misreading one of those as "absent from HEAD" would revert a
    LANDED fold — so only a definite miss against a resolvable HEAD reads as ``False``;
    anything indeterminate keeps the journal for the next recovery preamble instead."""
    if _git(tracker, "rev-parse", "-q", "--verify", "HEAD").returncode != 0:
        return None
    probe = _git(tracker, "cat-file", "-e", f"HEAD:{rel_snapshot}")
    if probe.returncode == 0:
        return True
    if probe.returncode == 124:
        return None
    return False


def _revert_partial_fold(tracker: str, record: dict) -> None:
    """Restore retired sources and unstage the ticket directory.
    Remove an uncommitted snapshot only after every source is restored.
    If restoration fails, retain the snapshot for fsck repair to avoid
    losing the folded event."""
    originals = [os.path.join(tracker, rel) for rel in record.get("sources", [])]
    snapshot = os.path.join(tracker, record.get("snapshot", ""))
    if compact_plan.restore_retired(originals):
        compact_plan.discard_uncommitted_snapshot(snapshot)
    else:
        logger.warning(
            "compact recovery: revert incomplete — snapshot %s retained; run "
            "`rebar store fsck --repair-snapshots`",
            snapshot,
        )
    ticket_id = record.get("ticket_id", "")
    if ticket_id:
        compact_plan.unstage_ticket_dir(tracker, ticket_id)


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
        if rel_snapshot and os.path.exists(snapshot_path):
            in_head = _snapshot_in_head(tracker, rel_snapshot)
            if in_head is None:
                # Indeterminate HEAD probe (transient git fault): touch nothing and KEEP
                # the journal — the next preamble re-examines it against a healthy repo.
                logger.warning(
                    "compact recovery: cannot interrogate HEAD for %s — deferring", journal
                )
                continue
            if not in_head:
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
