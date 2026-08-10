"""SNAPSHOT reconstruction — the fsck repair path that recomputes a ticket's SNAPSHOT
from its FULL ordered event log.

Extracted from ``compact.py`` (task b2bb). Its change-driver is the snapshot FORMAT and
the repair semantics, which move independently of the normal-fold transaction
(``compact_txn``) and of the CLI surface (``compact``).

The shared snapshot primitives it needs — ``_git``, ``_git_author``,
``_snapshot_strip_keys``, ``_build_authorship_ledger`` — are imported from
``compact_txn`` so the dependency runs one way (repair depends on the normal path) and
never cycles.

The rebuild counter lives HERE, next to the function that increments it:
``rebuild_snapshot_from_full_log`` does ``global _REBUILD_COUNT``, so hosting
``get_rebuild_count`` in another module would read a different module global and
silently report zero. ``compact`` re-exports it to preserve its public path.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from rebar._commands import _seam, fsck_repair
from rebar._commands.compact_txn import (
    _build_authorship_ledger,
    _git,
    _git_author,
    _snapshot_strip_keys,
)
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


def _read_event_uuid(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("uuid", os.path.basename(path))
    except (json.JSONDecodeError, OSError):
        return os.path.basename(path)


def _partition_rebuild_sources(
    ticket_dir: str,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Scan the ticket dir and return ``(live_raw, source_uuids, old_snaps, raw_paths)``.

    Every raw (non-snapshot) event — active OR ``*.retired`` — becomes a source of the new
    SNAPSHOT; the live ones are retired by the caller, and superseded snapshot(s) are
    retired too. ``-SYNC.json`` bridge metadata and dotfiles are skipped."""
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
    return live_raw, source_uuids, old_snaps, raw_paths


# raw-git-ok: store-maintenance command, seam-internal
def _commit_rebuild(tracker: str, ticket_id: str) -> None:
    """Best-effort stage+commit of a rebuilt SNAPSHOT (failures are non-fatal: the
    rebuild itself already succeeded on disk and the next store write will carry it)."""
    add = _git(tracker, "add", "-A", f"{ticket_id}/")
    if add.returncode != 0:
        return
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


# raw-git-ok: store-maintenance command, seam-internal
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

        # Full raw-history state INCLUDING the merged-in orphan the stale snapshot dropped.
        # None when the rebuild must NOT proceed — the b636 fail-closed guard (prior SNAPSHOT
        # cites sources absent from disk => incomplete log) or a failed reduce.
        compiled_state = fsck_repair.rebuild_source_state(ticket_id, ticket_dir)
        if compiled_state is None:
            logger.warning(
                "fsck: snapshot rebuild for %s aborted (incomplete log or reduce failure)",
                ticket_id,
            )
            return False
        # The legacy ``signature`` mirror is never persisted into a rebuilt snapshot
        # either (task 7ed9 never-emit) — it is re-derived in memory on replay.
        _strip = _snapshot_strip_keys()
        compiled_state = {k: v for k, v in compiled_state.items() if k not in _strip}

        live_raw, source_uuids, old_snaps, raw_paths = _partition_rebuild_sources(ticket_dir)
        # Authorship ledger (epic gnu-whale-ichor / 3183): rebuild it from the FULL raw log
        # (active + retired) so a rebuilt SNAPSHOT preserves the signed-event ledger too.
        compiled_state["authorship_ledger"] = _build_authorship_ledger(
            raw_paths, os.path.dirname(os.path.realpath(tracker))
        )

        snapshot_uuid = str(uuid.uuid4())
        snapshot_ts = hlc.next_tick(tracker, ticket_id)
        snapshot_event = {
            "event_type": "SNAPSHOT",
            "timestamp": snapshot_ts,
            "uuid": snapshot_uuid,
            "env_id": _seam.env_id(Path(tracker)),
            "author": _git_author(),
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
            _commit_rebuild(tracker, ticket_id)
        return True
    finally:
        handle.release()
