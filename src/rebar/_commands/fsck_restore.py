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
from rebar._store.ticket_layout import ticket_relpath
from rebar.reducer._cache import RETIRED_SUFFIX

logger = logging.getLogger(__name__)


def _deleted_history(tracker: str, ticket_id: str) -> dict[str, str]:
    """Map ``path -> commit that deleted it`` for every event file ever removed from this
    ticket dir, in ONE ticket-scoped pass.

    Modelled on :func:`rebar.attest.authorship.build_ticket_position_commit_map`, which does
    the same directory-prefix single-pass walk for ``--diff-filter=A``. CRITICAL DIFFERENCE:
    that helper lets the OLDEST commit win (correct for ADD — the oldest add introduced the
    event). For DELETE the correct fold is the NEWEST deletion, because a path can be added
    and deleted more than once across successive compactions and only the last removal has
    the current pre-image. ``git log`` streams newest-first, so FIRST-SEEN wins here.
    """
    prefixes = (f"{ticket_id}/", f"{ticket_relpath(ticket_id)}/")
    try:
        res = run_git(
            tracker,
            "log",
            "--diff-filter=D",
            "--name-only",
            "--format=@%H",
            "--",
            *prefixes,
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
        elif line.startswith(prefixes) and commit:
            out.setdefault(line, commit)  # newest-first stream => first seen is newest
    return out


def _deleted_path_for_uuid(tracker: str, ticket_id: str, uuid: str) -> tuple[str, str] | None:
    """``(path, deleting commit)`` for ONE uuid, or ``None`` (bug 85fa).

    :func:`_deleted_history` scopes to the ticket DIRECTORY; git path simplification can drop a
    deletion there that a PATH-scoped walk reports. Live-store measurement on f130's lost EDIT:
    ``-- <tid>/`` found 0, ``-- <tid>/*<uuid>*`` found 1; neither ``--all`` nor
    ``--full-history`` helped, so it is a pathspec-scope effect.
    """
    prefixes = (f"{ticket_id}/", f"{ticket_relpath(ticket_id)}/")
    try:
        res = run_git(
            tracker,
            "log",
            "--diff-filter=D",
            "--name-only",
            "--format=@%H",
            "--",
            f"{ticket_id}/*{uuid}*",
            f"{ticket_relpath(ticket_id)}/*{uuid}*",
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
        elif line.startswith(prefixes) and uuid in line and commit:
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
    """Restore the event files a legacy compaction DELETED, from tickets-branch history.

    Compaction before the I1 non-destructive rename (story tricolour-head-ratfish) removed
    its folded sources from the worktree, but the blobs stay reachable at the deleting
    commit's parent. Each is written back as a folded ``*.retired`` source — NEVER as a live
    event — so the subsequent rebuild replays it under ``include_retired=True`` without
    resurrecting it into the active log.

    Sweeps the ticket's FULL deletion history rather than the newest SNAPSHOT's
    ``source_event_uuids``: an earlier deleted STATUS that no surviving snapshot cites still
    breaks the chain's ``current_status`` precondition, so the reducer would reject both it
    and the close that follows.

    Returns the filenames restored (or, under ``dry_run``, those that WOULD be restored).
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
    """Restore any deleted sources, then rebuild. Returns ``(rebuilt, restored_filenames)``.

    This is the repair path's single entrypoint. The b636 fail-closed guard inside
    :func:`rebar._commands.compact.rebuild_snapshot_from_full_log` still applies: if the
    restore could not complete the log, the rebuild refuses and the ticket is surfaced for
    human triage rather than rewritten from a partial history.

    ``no_commit`` is forwarded verbatim to the rebuild. It defaults to ``False`` — the
    behaviour ``repair_or_plan`` has always had — so only the caller that needs deferred
    commits (``_repair_ticket`` under the ``--repair`` batch loop, which commits once per
    batch rather than once per ticket) has to pass it.
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
