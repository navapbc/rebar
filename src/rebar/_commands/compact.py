"""In-process ``compact`` / ``compact-all`` — the CLI facade.

Compaction squashes a ticket's event log into ONE SNAPSHOT event under the unified
write lock: re-list events inside the lock, partition out forward-compat
unknown-type events (never absorbed/deleted), re-check the threshold, reduce the
current state, write the SNAPSHOT, retire the originals, invalidate the reducer
cache, and ``git add -A`` + commit atomically.

Task b2bb split the engines out of this module, leaving the argument parsing and the
operator-facing reporting here:

* :mod:`rebar._commands.compact_txn` — the locked compaction TRANSACTION
  (``_compact_locked``) plus the snapshot primitives both engines share.
* :mod:`rebar._commands.compact_rebuild` — SNAPSHOT reconstruction from the full log
  (``rebuild_snapshot_from_full_log``), the fsck repair path.

The names those modules own are RE-EXPORTED here so every existing import path —
``fsck_repair`` / ``fsck_restore`` reaching for ``rebuild_snapshot_from_full_log``, and
the tests importing ``_snapshot_strip_keys`` / ``_build_authorship_ledger`` — keeps
resolving against ``rebar._commands.compact`` unchanged.

Reuses ``rebar._store.lock`` (the fcntl+mkdir dual-leg lock),
``rebar.reducer.reduce_ticket`` (in-process), and ``event_append.event_filename``.
SNAPSHOT bytes go through the single canonical serializer
``rebar._store.canonical.canonical_str`` (sorted keys, P1.0).
"""

from __future__ import annotations

import os
import sys

from rebar import config
from rebar._commands.compact_rebuild import (
    get_rebuild_count,
    rebuild_snapshot_from_full_log,
)
from rebar._commands.compact_txn import (  # noqa: F401 — re-exported public path
    _build_authorship_ledger,
    _compact_locked,
    _git,
    _snapshot_strip_keys,
    _sync_before_compact,
)
from rebar._engine_support.resolver import resolve_ticket_id

__all__ = [
    "compact_all_cli",
    "compact_cli",
    "get_rebuild_count",
    "rebuild_snapshot_from_full_log",
]


def _usage() -> int:
    sys.stderr.write(
        "Usage: ticket-compact.sh <ticket_id> [--threshold=N] [--horizon=NS]\n"
        "  Default threshold: REBAR_COMPACT_THRESHOLD env / compact.threshold config or 10\n"
        "  Default horizon:   REBAR_COMPACTION_HORIZON_NS env / compact.COMPACTION_HORIZON_NS\n"
        "                     config or 1800s in ns (events younger than this stay live)\n"
    )
    return 1


def compact_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar compact <id>`` entry."""
    if len(argv) < 1:
        return _usage()
    tracker = str(config.tracker_dir(repo_root))
    raw = argv[0]
    ticket_id = resolve_ticket_id(raw, tracker)
    if ticket_id is None:
        sys.stderr.write(f"Error: ticket '{raw}' not found\n")
        return 1

    # Default threshold from the typed config (compact.threshold; env
    # REBAR_COMPACT_THRESHOLD, deprecated alias COMPACT_THRESHOLD, or a config file).
    # A --threshold= flag below still overrides. A malformed config is reported as a
    # clean error (exit 1), not an uncaught traceback.
    try:
        _cfg = config.load_config(repo_root).compact
        threshold = _cfg.threshold
        horizon = _cfg.COMPACTION_HORIZON_NS
    except config.ConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    skip_sync = False
    no_commit = False
    for a in argv[1:]:
        if a.startswith("--threshold="):
            threshold = int(a[len("--threshold=") :])
        elif a.startswith("--horizon="):
            horizon = int(a[len("--horizon=") :])
        elif a == "--skip-sync":
            skip_sync = True
        elif a == "--no-commit":
            no_commit = True
        else:
            sys.stderr.write(f"Error: unknown argument '{a}'\n")
            return _usage()

    if not (
        os.path.isdir(tracker)
        and (
            os.path.isfile(os.path.join(tracker, ".git"))
            or os.path.isdir(os.path.join(tracker, ".git"))
        )
    ):
        sys.stderr.write("Error: ticket system not initialized. Run 'ticket init' first.\n")
        return 1
    ticket_dir = os.path.join(tracker, ticket_id)
    if not os.path.isdir(ticket_dir):
        sys.stderr.write(f"Error: ticket directory not found: {ticket_dir}\n")
        return 1

    if not skip_sync:
        _sync_before_compact(tracker)
        if any(
            f.endswith("-SNAPSHOT.json") and not f.startswith(".") for f in os.listdir(ticket_dir)
        ):
            sys.stdout.write(f"skipping compaction for {ticket_id} — remote SNAPSHOT exists\n")
            return 0

    preflock = sum(
        1 for f in os.listdir(ticket_dir) if f.endswith(".json") and not f.startswith(".")
    )
    if preflock <= threshold:
        sys.stdout.write(f"below threshold ({preflock} <= {threshold}) — skipping compaction\n")
        return 0

    rc = _compact_locked(tracker, ticket_id, ticket_dir, threshold, no_commit, horizon)
    # A successful compaction commits a SNAPSHOT inline (not via write_and_push), so
    # push it best-effort — unless --no-commit (nothing committed) or --skip-sync
    # (the caller owns sync: compact-on-close passes it and the transition pushes;
    # compact-all batches one commit + push itself). Bug prone-octet-cheek.
    if rc == 0 and not no_commit and not skip_sync:
        from rebar._store import push

        push.push_after_commit(tracker)
    return rc


# ── compact-all ──────────────────────────────────────────────────────────────
def _scan_snapshot_state(tracker: str) -> tuple[list[str], int]:
    """Return (ticket ids lacking a SNAPSHOT, count already having one), scanning
    ticket dirs (those with at least one event JSON), sorted by name."""
    needs: list[str] = []
    already = 0
    try:
        entries = sorted(os.scandir(tracker), key=lambda e: e.name)
    except OSError:
        return [], 0
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        names = os.listdir(entry.path)
        if not any(n.endswith(".json") for n in names):
            continue
        if any(n.endswith("-SNAPSHOT.json") for n in names):
            already += 1
        else:
            needs.append(entry.name)
    return needs, already


def _compact_all_parse(argv: list[str]) -> tuple[bool, int, bool, int | None]:
    """Parse ``compact-all`` flags. Returns ``(dry_run, limit, no_commit, early_rc)``
    where ``early_rc`` is non-None when the caller should return it immediately
    (``--help`` => 0, an unknown option => 1)."""
    dry_run = False
    limit = 0
    no_commit = False
    for a in argv:
        if a == "--dry-run":
            dry_run = True
        elif a.startswith("--limit="):
            limit = int(a[len("--limit=") :])
        elif a == "--no-commit":
            no_commit = True
        elif a in ("--help", "-h"):
            sys.stdout.write("Usage: ticket compact-all [--dry-run] [--limit=N] [--no-commit]\n")
            return dry_run, limit, no_commit, 0
        else:
            sys.stderr.write(f"Error: unknown option '{a}'\n")
            return dry_run, limit, no_commit, 1
    return dry_run, limit, no_commit, None


# raw-git-ok: store-maintenance command, seam-internal
def _commit_backfill(tracker: str, compacted: int) -> None:
    """Commit + push the batch of backfilled SNAPSHOTs (one commit for the whole run;
    the per-ticket calls passed ``--skip-sync`` to defer the push here — bug
    prone-octet-cheek)."""
    sys.stdout.write(f"Staging and committing {compacted} new SNAPSHOT files...\n")
    _git(tracker, "add", "-A")
    if _git(tracker, "diff", "--cached", "--quiet").returncode == 0:
        sys.stdout.write("No staged changes (SNAPSHOTs may already have been committed).\n")
        return
    _git(
        tracker,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        f"chore: backfill SNAPSHOT files for {compacted} tickets (ticket-compact-all)",
    )
    sys.stdout.write("Committed.\n")
    from rebar._store import push

    push.push_after_commit(tracker)


def compact_all_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar compact-all`` entry — backfill SNAPSHOTs for tickets lacking one."""
    import contextlib
    import io

    dry_run, limit, no_commit, early_rc = _compact_all_parse(argv)
    if early_rc is not None:
        return early_rc

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        sys.stderr.write(f"Error: tracker dir not found: {tracker}\n")
        return 1

    needs, already = _scan_snapshot_state(tracker)
    total_needs = len(needs)
    sys.stdout.write(f"Tickets already with SNAPSHOT : {already}\n")
    sys.stdout.write(f"Tickets needing compaction     : {total_needs}\n")
    if total_needs == 0:
        sys.stdout.write("Nothing to do.\n")
        return 0

    if dry_run:
        sys.stdout.write("\nDry-run — would compact:\n")
        for tid in needs:
            sys.stdout.write(f"  {tid}\n")
        return 0

    if limit > 0 and total_needs > limit:
        sys.stdout.write(f"Applying --limit={limit} (will stop after {limit} tickets).\n")
        needs = needs[:limit]
        total_needs = limit

    compacted = 0
    error_ids: list[str] = []
    sys.stdout.write(f"\nCompacting {total_needs} tickets...\n")
    sys.stdout.write("(each dot = 1 ticket; E = error)\n")
    for tid in needs:
        with contextlib.redirect_stderr(io.StringIO()):  # bash 2>/dev/null
            rc = compact_cli(
                [tid, "--threshold=0", "--skip-sync", "--no-commit"], repo_root=repo_root
            )
        if rc == 0:
            compacted += 1
            sys.stdout.write(".")
        else:
            error_ids.append(tid)
            sys.stdout.write("E")
        sys.stdout.flush()

    sys.stdout.write("\n\n")
    sys.stdout.write(
        f"Done: {compacted} compacted, {len(error_ids)} errors (of {total_needs} attempted)\n"
    )
    if error_ids:
        sys.stderr.write("Errored tickets:\n")
        for tid in error_ids:
            sys.stderr.write(f"  {tid}\n")

    if compacted > 0 and not no_commit:
        _commit_backfill(tracker, compacted)

    return 2 if error_ids else 0
