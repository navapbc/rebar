"""Restore event files that a legacy compaction DELETED, recovered from tickets history.

Split out of :mod:`.fsck_repair` along the existing call-graph seam (the module-size policy in
AGENTS.md): ``fsck_repair`` owns the snapshot-source ACCOUNTING and the repair decision, this
leaf owns RECOVERING the bytes and rebuilding on top of them. ``fsck_repair`` re-exports the
public names so ``fsck_repair.<name>`` attribute access keeps resolving.

``snapshot_missing_sources`` stays in ``fsck_repair`` and is imported lazily below so the two
modules do not form an import cycle.
"""

from __future__ import annotations

import logging
import os
import subprocess

from rebar._store.gitutil import run_git
from rebar.reducer._cache import RETIRED_SUFFIX

logger = logging.getLogger(__name__)


def _deleted_history(tracker: str, ticket_id: str) -> dict[str, str]:
    """Map each deleted event path to its newest deleting commit in one directory pass.

    A path may be added and removed across several compactions. ``git log`` is newest first,
    so retaining the first deletion preserves the current pre-image.
    """
    try:
        res = run_git(
            tracker,
            "log",
            "--diff-filter=D",
            "--name-only",
            "--format=@%H",
            "--",
            f"{ticket_id}/",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        return {}
    out: dict[str, str] = {}
    commit = ""
    for line in (res.stdout or "").splitlines():
        if line.startswith("@"):
            commit = line[1:]
        elif line.startswith(f"{ticket_id}/") and commit:
            out.setdefault(line, commit)  # newest-first stream => first seen is newest
    return out


def _deleted_path_for_uuid(tracker: str, ticket_id: str, uuid: str) -> tuple[str, str] | None:
    """Return one UUID's deleted path and commit, or ``None``.

    The path-scoped lookup is a fallback for deletions omitted by git history
    simplification during the directory-scoped pass.
    """
    try:
        res = run_git(
            tracker,
            "log",
            "--diff-filter=D",
            "--name-only",
            "--format=@%H",
            "--",
            f"{ticket_id}/*{uuid}*",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    commit = ""
    for line in (res.stdout or "").splitlines():
        if line.startswith("@"):
            commit = line[1:]
        elif line.startswith(f"{ticket_id}/") and uuid in line and commit:
            return line, commit  # newest-first stream => first hit is the newest deletion
    return None


def _uuid_fallback(
    tracker: str, ticket_id: str, ticket_dir: str, have: set, *, dry_run: bool
) -> list[str]:
    """Second pass for sources the directory-scoped walk missed (bug 85fa), bounded to uuids
    the snapshot STILL cites as absent (the per-uuid glob is much slower)."""
    out: list[str] = []
    from rebar._commands.fsck_repair import snapshot_missing_sources

    for missing_uuid in snapshot_missing_sources(ticket_dir):
        hit = _deleted_path_for_uuid(tracker, ticket_id, missing_uuid)
        if hit is None:
            continue
        path, commit = hit
        name = path.split("/")[-1]
        if name in have or name + RETIRED_SUFFIX in have:
            continue
        if dry_run:
            out.append(name)
            continue
        blob = run_git(tracker, "show", f"{commit}^:{path}", check=False, text=False)
        if blob.returncode != 0 or not blob.stdout:
            continue
        with open(os.path.join(ticket_dir, name + RETIRED_SUFFIX), "wb") as fh:
            fh.write(blob.stdout)
        have.add(name + RETIRED_SUFFIX)
        out.append(name)
        logger.warning(
            "fsck: restored %s for %s via the per-uuid fallback (the directory-scoped walk "
            "missed it)",
            name,
            ticket_id,
        )
    return out


def restore_deleted_sources(
    tracker: str, ticket_id: str, ticket_dir: str, *, dry_run: bool = False
) -> list[str]:
    """Restore deleted event sources from tickets-branch history as ``*.retired`` files.

    Restoration scans the full deletion history because uncited earlier events can still be
    required by reducer preconditions. Retired files participate in a full-log rebuild without
    returning to the active log. Return restored filenames, or planned filenames in
    ``dry_run`` mode.
    """
    try:
        have = set(os.listdir(ticket_dir))
    except OSError:
        return []
    restored: list[str] = []
    for path, commit in sorted(_deleted_history(tracker, ticket_id).items()):
        name = path.split("/")[-1]
        if name in have or name + RETIRED_SUFFIX in have:
            continue
        if dry_run:
            restored.append(name)
            continue
        try:
            blob = run_git(tracker, "show", f"{commit}^:{path}", check=False, text=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if blob.returncode != 0 or not blob.stdout:
            continue
        try:
            with open(os.path.join(ticket_dir, name + RETIRED_SUFFIX), "wb") as fh:
                fh.write(blob.stdout)
        except OSError:
            logger.warning("fsck: could not restore %s for %s", name, ticket_id)
            continue
        restored.append(name)

    restored.extend(_uuid_fallback(tracker, ticket_id, ticket_dir, have, dry_run=dry_run))

    if restored and not dry_run:
        logger.warning(
            "fsck: restored %d deleted source event(s) for %s from tickets history",
            len(restored),
            ticket_id,
        )
    return restored


def rebuild_with_restore(
    tracker: str, ticket_id: str, ticket_dir: str, *, no_commit: bool = False
) -> tuple[bool, list[str]]:
    """Restore deleted sources, then return the rebuild result and restored filenames.

    The rebuild fails closed when restoration cannot complete the log, leaving the ticket for
    human triage. ``no_commit`` passes through so batch repair can defer per-ticket commits.
    """
    from rebar._commands.fsck_repair import snapshot_missing_sources

    restored: list[str] = []
    if snapshot_missing_sources(ticket_dir):
        restored = restore_deleted_sources(tracker, ticket_id, ticket_dir)
        cache = os.path.join(ticket_dir, ".cache.json")
        if os.path.exists(cache):
            try:
                os.remove(cache)
            except OSError:
                pass

    from rebar._commands.compact import rebuild_snapshot_from_full_log

    return (
        rebuild_snapshot_from_full_log(tracker, ticket_id, ticket_dir, no_commit=no_commit),
        restored,
    )
