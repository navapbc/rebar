"""SNAPSHOT rebuild — the fsck repair path that recomputes a ticket's SNAPSHOT from its
full ordered event log (RC2b Option 1, "rebuild-on-stray").

Extracted from :mod:`.compact` along the existing call-graph seam (the module-size policy
in AGENTS.md): ``compact`` owns ordinary compaction, this leaf owns the rebuild and its
safety gates. ``compact`` re-exports the public names so ``compact.rebuild_snapshot_from_full_log``
attribute access keeps resolving.

The shared low-level helpers (``_git``, ``_git_author``, ``_read_event_uuid``,
``_snapshot_strip_keys``, ``_build_authorship_ledger``) still live in ``compact`` and are
imported lazily inside the functions below so the two modules do not form an import cycle.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from rebar._commands import _seam
from rebar._store import compat, event_append, fsutil, hlc, lock
from rebar._store.canonical import canonical_str
from rebar.reducer import reduce_ticket
from rebar.reducer._cache import RETIRED_SUFFIX, is_active_event

logger = logging.getLogger(__name__)

# Process-level count of SNAPSHOT rebuilds (RC2b Option 1) — observability for the
# fsck remediation path (A3). Read via get_rebuild_count().
_REBUILD_COUNT = 0


def get_rebuild_count() -> int:
    """Number of snapshot rebuilds performed by this process (RC2b Option 1)."""
    return _REBUILD_COUNT


def snapshot_missing_sources(ticket_dir: str) -> list[str]:
    """UUIDs cited by the newest active SNAPSHOT that have NO event file on disk (b636).

    Compaction before the I1 non-destructive rename (story tricolour-head-ratfish)
    DELETED its folded sources, so for those tickets the cited state survives
    only inside the SNAPSHOT's ``compiled_state``. A non-empty return therefore means the
    raw log is PROVABLY INCOMPLETE and a from-zero replay (even ``include_retired=True``)
    would silently reconstruct a partial history — reverting closed tickets to whatever
    status event happened to survive. Callers must fail closed on that.

    Best-effort: an unreadable/absent snapshot yields ``[]`` (nothing proven missing).
    """
    try:
        names = sorted(os.listdir(ticket_dir))
    except OSError:
        return []
    snaps = [n for n in names if is_active_event(n) and n.endswith("-SNAPSHOT.json")]
    if not snaps:
        return []
    try:
        with open(os.path.join(ticket_dir, snaps[-1]), encoding="utf-8") as fh:
            snapshot = json.load(fh)
    except (OSError, ValueError):
        return []
    cited = ((snapshot.get("data") or {}).get("source_event_uuids")) or []
    if not cited:
        return []
    # A source is present if any file (active OR retired) carries its uuid.
    present = {uuid_ for uuid_ in cited if any(uuid_ in name for name in names)}
    return [uuid_ for uuid_ in cited if uuid_ not in present]


def _refuse_incomplete_log(ticket_id: str, ticket_dir: str) -> bool:
    """True when a rebuild MUST be refused because the raw log is provably incomplete (b636).

    If the prior SNAPSHOT cites sources that no longer exist on disk (legacy delete-style
    compaction), replaying what survives would silently discard the state those events
    carried — order-sensitive STATUS above all, which ticket 34b1 already classifies as
    HUMAN-TRIAGE, never auto-rebuilt. Logs the refusal so the operator can triage.
    """
    missing = snapshot_missing_sources(ticket_dir)
    if not missing:
        return False
    logger.warning(
        "fsck: REFUSING snapshot rebuild for %s — prior SNAPSHOT cites %d source event(s) "
        "absent from disk (%s); the raw log is incomplete and a rebuild would drop state "
        "held only in the snapshot. Human triage required.",
        ticket_id,
        len(missing),
        _abbrev(missing),
    )
    return True


def _abbrev(uuids: list[str], limit: int = 3) -> str:
    """``a, b, c...`` — first ``limit`` uuids, ellipsised when there are more."""
    head = ", ".join(uuids[:limit])
    return f"{head}..." if len(uuids) > limit else head


def _rebuild_source_state(ticket_id: str, ticket_dir: str) -> dict | None:
    """The state a rebuild would persist, or ``None`` when the rebuild must be refused.

    Two gates. **b636 fail-closed**: a from-zero replay is only sound when the raw log is
    COMPLETE, so a prior SNAPSHOT citing sources absent from disk aborts the rebuild rather
    than reconstructing a partial history. Then the usual reduce validation: full raw-history
    state (active + retired, snapshots stripped) INCLUDING the merged-in orphan the stale
    snapshot's positional skip had dropped.
    """
    if _refuse_incomplete_log(ticket_id, ticket_dir):
        return None
    compiled_state = reduce_ticket(ticket_dir, include_retired=True)
    if compiled_state is None or compiled_state.get("status") in ("error", "fsck_needed"):
        logger.warning("fsck: snapshot rebuild for %s aborted (reduce failed)", ticket_id)
        return None
    return compiled_state


def rebuild_snapshot_from_full_log(
    tracker: str,
    ticket_id: str,
    ticket_dir: str,
    *,
    no_commit: bool = False,
) -> bool:
    """RC2b Option 1 (rebuild-on-stray): recompute a ticket's SNAPSHOT from the FULL
    ordered event log INCLUDING ``*.retired`` sources, folding a merged-in pre-snapshot
    orphan that a stale snapshot's positional skip had silently dropped.

    Crash-safe via a ``.snapshot-rebuild.bak`` sentinel: it is written before any
    mutation and removed only after a clean round-trip (a fresh reduce reproduces the
    rebuilt state). A ``.bak`` present at entry means a prior rebuild was interrupted —
    we rebuild again (the operation is idempotent). Runs under the write lock
    (single-writer). Returns True if a rebuild was performed.
    """
    global _REBUILD_COUNT

    from rebar._commands.compact import (
        _build_authorship_ledger,
        _git,
        _git_author,
        _read_event_uuid,
        _snapshot_strip_keys,
    )

    try:
        handle = lock.acquire(tracker, timeout=30, attempts=2, dual_window=True)
    except lock.LockTimeout as exc:
        logger.warning("fsck: cannot rebuild snapshot for %s: %s", ticket_id, exc)
        return False
    except compat.StoreIncompatibleError as exc:
        # Story 21dd: fail closed on an incompatible store — the snapshot rebuild is a
        # mutation, so skip it (the read-only diagnostic still surfaces the record).
        logger.warning("fsck: cannot rebuild snapshot for %s: %s", ticket_id, exc)
        return False
    try:
        bak_path = os.path.join(ticket_dir, ".snapshot-rebuild.bak")
        if os.path.exists(bak_path):
            logger.warning(
                "fsck: interrupted snapshot rebuild for %s (.bak present) — restarting", ticket_id
            )

        compiled_state = _rebuild_source_state(ticket_id, ticket_dir)
        if compiled_state is None:
            return False
        # The legacy ``signature`` mirror is never persisted into a rebuilt snapshot
        # either (task 7ed9 never-emit) — it is re-derived in memory on replay.
        _strip = _snapshot_strip_keys()
        compiled_state = {k: v for k, v in compiled_state.items() if k not in _strip}

        # Every raw (non-snapshot) event becomes a source of the new SNAPSHOT; the live
        # ones are retired, superseded snapshot(s) are retired too.
        live_raw: list[str] = []
        source_uuids: list[str] = []
        old_snaps: list[str] = []
        raw_paths: list[str] = []
        for name in sorted(os.listdir(ticket_dir)):
            if name.startswith(".") or name.endswith("-SYNC.json"):
                continue
            path = os.path.join(ticket_dir, name)
            base = name[: -len(RETIRED_SUFFIX)] if name.endswith(RETIRED_SUFFIX) else name
            if base.endswith("-SNAPSHOT.json"):
                if is_active_event(name):
                    old_snaps.append(path)
                continue
            source_uuids.append(_read_event_uuid(path))
            raw_paths.append(path)
            if is_active_event(name):
                live_raw.append(path)
        # Authorship ledger (epic gnu-whale-ichor / 3183): rebuild it from the FULL raw log
        # (active + retired) so a rebuilt SNAPSHOT preserves the signed-event ledger too.
        compiled_state["authorship_ledger"] = _build_authorship_ledger(
            raw_paths, os.path.dirname(os.path.realpath(tracker))
        )

        env_id = _seam.env_id(Path(tracker))
        author = _git_author()
        snapshot_uuid = str(uuid.uuid4())
        snapshot_ts = hlc.next_tick(tracker, ticket_id)
        snapshot_event = {
            "event_type": "SNAPSHOT",
            "timestamp": snapshot_ts,
            "uuid": snapshot_uuid,
            "env_id": env_id,
            "author": author,
            "data": {
                "compiled_state": compiled_state,
                "source_event_uuids": source_uuids,
                "compacted_at": snapshot_ts,
            },
        }
        # Denormalized author attribution (epic gnu-whale-ichor) — derive repo_root from
        # the tracker (no repo_root param on this fsck-repair path).
        snapshot_event.update(_seam.attribution_fields(os.path.dirname(os.path.realpath(tracker))))

        # Sentinel/back-up the pre-rebuild snapshot BEFORE mutating.
        try:
            backup = ""
            if old_snaps:
                with open(old_snaps[-1], encoding="utf-8") as f:
                    backup = f.read()
            fsutil.atomic_write(bak_path, backup, encoding="utf-8")
        except OSError:
            logger.warning("fsck: could not write rebuild sentinel for %s", ticket_id)
            return False

        final_path = os.path.join(
            ticket_dir, event_append.event_filename(snapshot_ts, snapshot_uuid, "SNAPSHOT")
        )
        fsutil.atomic_write(final_path, canonical_str(snapshot_event), encoding="utf-8")

        for fp in live_raw + old_snaps:
            retired = fp + RETIRED_SUFFIX
            if os.path.exists(retired):
                continue
            try:
                os.rename(fp, retired)
            except OSError:
                logger.warning("fsck: could not retire %s during rebuild", fp, exc_info=True)

        try:
            os.remove(os.path.join(ticket_dir, ".cache.json"))
        except OSError:
            pass

        # Clean round-trip: a fresh reduce must reproduce the rebuilt status before we
        # drop the sentinel (else leave it so the next fsck retries).
        check = reduce_ticket(ticket_dir)
        if check is None or check.get("status") != compiled_state.get("status"):
            logger.warning(
                "fsck: snapshot rebuild round-trip mismatch for %s — leaving .bak for retry",
                ticket_id,
            )
            return False
        try:
            os.remove(bak_path)
        except OSError:
            pass

        _REBUILD_COUNT += 1
        logger.warning(
            "fsck: rebuilt SNAPSHOT for %s from full log (%d sources) — folded a merged-in "
            "pre-snapshot orphan",
            ticket_id,
            len(source_uuids),
        )

        if not no_commit:
            add = _git(tracker, "add", "-A", f"{ticket_id}/")
            if add.returncode == 0:
                staged = _git(tracker, "diff", "--cached", "--quiet")
                if staged.returncode != 0:
                    _git(
                        tracker,
                        "commit",
                        "-q",
                        "--no-verify",
                        "-m",
                        f"ticket: REBUILD SNAPSHOT {ticket_id}",
                    )
        return True
    finally:
        handle.release()
