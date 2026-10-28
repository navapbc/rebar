"""Converge SNAPSHOTs that froze close metadata a later reopen should have cleared (df72).

The reducer fold drops ``close_class``, ``close_reason``, ``force_close_reason`` and
``completion_expectation`` on a ``closed -> *`` edge (bug 6df4-d3f0). Replay starts at the
latest SNAPSHOT and never re-folds the retired events behind it, so a ticket compacted before
that fix keeps the stale values, and no fold or cache-version change can reach them.

The unit finds a SNAPSHOT whose replayed close metadata disagrees with a full-log rebuild and
rebuilds only that ticket. Selection compares state rather than testing a field such as
``last_reopened_at``: ``closed -> in_progress`` clears close metadata without setting it.
A converged store makes no commit. :func:`run_ensures` holds the write lock, so the rebuild
uses the lock-free entry point.
"""

from __future__ import annotations

import logging
import os

from rebar._store.ensures import EnsureOutcome
from rebar.reducer._processors_status import CLOSE_METADATA_KEYS

logger = logging.getLogger("rebar")

# Stable, immutable id: persisted in ``.ensure-applied`` and asserted against
# ``ensures._registry()`` by the registry-drift guard.
UNIT_ID = "snapshot-close-metadata"

_SNAPSHOT_SUFFIX = "-SNAPSHOT.json"


def _snapshot_names_close_metadata(ticket_dir: str) -> bool:
    """Whether the latest active SNAPSHOT text mentions any close-metadata key.

    A cheap prefilter over the raw text: a false positive costs one comparison, and a
    ticket whose SNAPSHOT never mentions a key cannot be carrying a stale value."""
    try:
        names = [n for n in os.listdir(ticket_dir) if n.endswith(_SNAPSHOT_SUFFIX)]
        if not names:
            return False
        with open(os.path.join(ticket_dir, max(names)), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    return any(f'"{key}"' in text for key in CLOSE_METADATA_KEYS)


def _replay_disagrees_with_full_log(ticket_id: str, ticket_dir: str) -> bool | None:
    """Whether replayed close metadata differs from a full-log rebuild, or ``None`` when
    the full log cannot be trusted (a SNAPSHOT cites sources absent from disk)."""
    from rebar._commands import fsck_repair
    from rebar.reducer import reduce_ticket

    replayed = reduce_ticket(ticket_dir)
    full = fsck_repair.rebuild_source_state(ticket_id, ticket_dir)
    if replayed is None or full is None:
        return None
    return any(replayed.get(key) != full.get(key) for key in CLOSE_METADATA_KEYS)


def converge_snapshot_close_metadata_unit(tracker: str) -> EnsureOutcome:
    """Rebuild each ticket whose SNAPSHOT froze close metadata its history later cleared."""
    from rebar._commands.compact_rebuild import rebuild_snapshot_under_lock

    rebuilt: list[str] = []
    unresolved: list[str] = []
    for entry in sorted(os.scandir(tracker), key=lambda e: e.name):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not _snapshot_names_close_metadata(entry.path):
            continue
        stale = _replay_disagrees_with_full_log(entry.name, entry.path)
        if stale is None:
            unresolved.append(entry.name)
        elif stale:
            done = rebuild_snapshot_under_lock(tracker, entry.name, entry.path)
            (rebuilt if done else unresolved).append(entry.name)
    if unresolved:
        logger.warning(
            "snapshot-close-metadata: %d ticket(s) not converged (incomplete log or failed "
            "rebuild): %s",
            len(unresolved),
            ", ".join(unresolved),
        )
    if rebuilt:
        return EnsureOutcome(UNIT_ID, "changed", f"rebuilt {len(rebuilt)} stale SNAPSHOT(s)")
    return EnsureOutcome(UNIT_ID, "ok", f"{len(unresolved)} not converged" if unresolved else "")
