"""CLI facade for ticket compaction and store-wide sweeps.
The locked fold lives in compact_txn, full-log fsck rebuild in
compact_rebuild, and shared derivations in compact_plan. This module
re-exports their established names and reports sweep results."""

from __future__ import annotations

import logging
import os
import sys

from rebar import config
from rebar._commands import compact_plan
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
from rebar._store import hlc
from rebar._store import lock as _lock
from rebar._store import lock_owner as _lock_owner
from rebar._store.gitutil import _AUTOMAINT_OFF

logger = logging.getLogger(__name__)

__all__ = [
    "compact_all_cli",
    "compact_cli",
    "compact_dispatch_cli",
    "get_rebuild_count",
    "rebuild_snapshot_from_full_log",
]


def _usage() -> int:
    sys.stderr.write(
        "Usage: rebar store compact <ticket_id> [--threshold=N] [--horizon=NS]\n"
        "  Default threshold: REBAR_COMPACT_THRESHOLD env / compact.threshold config or 10\n"
        "  Default horizon:   REBAR_COMPACTION_HORIZON_NS env / compact.COMPACTION_HORIZON_NS\n"
        "                     config or 1800s in ns (events younger than this stay live)\n"
    )
    return 1


def compact_cli(
    argv: list[str], *, repo_root=None, position_commits: dict[str, str] | None = None
) -> int:
    """``rebar store compact <id>`` entry.

    ``position_commits`` is the OPTIONAL prebuilt position->commit map the store-wide sweep
    threads in so the authorship-ledger history walk does not run inside the store write lock
    (see :func:`compact_all_cli`). Absent, the fold builds its own per-ticket map as before."""
    if len(argv) < 1:
        return _usage()
    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.core.compact import build_compact_one

    tracker = str(config.tracker_dir(repo_root))
    raw = argv[0]
    ticket_id = resolve_ticket_id(raw, tracker)
    if ticket_id is None:
        sys.stderr.write(f"Error: ticket '{raw}' not found\n")
        return 1

    # Default threshold from the typed config (compact.threshold; env
    # REBAR_COMPACT_THRESHOLD, or a config file).
    # A --threshold= flag below still overrides. A malformed config is reported as a
    # clean error (exit 1), not an uncaught traceback.
    try:
        _cfg = config.compose_config(repo_root).compact
        threshold = _cfg.threshold
        horizon = _cfg.COMPACTION_HORIZON_NS
    except config.ConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    # Bespoke reject guard, kept ahead of the factory: compact's value flags are
    # equals-only (the space form is rejected) and its ticket id is permissive and may
    # look like an option, neither of which argparse matches byte-for-byte; and the
    # reject must fire only AFTER the ticket id resolves so a bad id still wins over a
    # bad flag. The factory below is the parser of record for the accepted values.
    for a in argv[1:]:
        if not (
            a.startswith(("--threshold=", "--horizon=")) or a in ("--skip-sync", "--no-commit")
        ):
            sys.stderr.write(f"Error: unknown argument '{a}'\n")
            return _usage()
    try:
        ns = build_compact_one(prog="rebar store compact").parse_args([*argv[1:], "--", raw])
    except ParseError as exc:
        return render_parse_error(exc)
    if ns.threshold is not None:
        threshold = int(ns.threshold)
    if ns.horizon is not None:
        horizon = int(ns.horizon)
    skip_sync = ns.skip_sync
    no_commit = ns.no_commit

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

    rc = _compact_locked(
        tracker,
        ticket_id,
        ticket_dir,
        threshold,
        no_commit,
        horizon,
        position_commits=position_commits,
    )
    # A successful compaction commits a SNAPSHOT inline (not via write_and_push), so
    # push it best-effort — unless --no-commit (nothing committed) or --skip-sync
    # (the caller owns sync: compact-on-close passes it and the transition pushes;
    # compact-all batches one commit + push itself). Bug prone-octet-cheek.
    if rc == 0 and not no_commit and not skip_sync:
        from rebar._store import push

        push.push_after_commit(tracker)
    return rc


# ── compact-all ──────────────────────────────────────────────────────────────
def _foldable_event_count(ticket_dir: str, now: int, horizon: int) -> int:
    """Count events the locked fold can actually absorb under the horizon.
    Use the shared candidate listing and foldability rule. Exclude unknown
    event types, bridge metadata, retired files, and prior snapshots.
    An unreadable candidate counts only when the horizon is off."""
    return compact_plan.foldable_stats(ticket_dir, now, horizon).count


def _scan_snapshot_state(
    tracker: str,
    threshold: int = 0,
    horizon: int = 0,
    *,
    snapshot_alpha: float = 0.0,
    include_archived: bool = False,
) -> tuple[list[str], int]:
    """Return sorted ticket IDs needing a first snapshot or a recurrence fold,
    plus the remaining count. Backfill selects snapshotless tickets with any
    foldable event. Recurrence selects counts above threshold. Scan active
    tickets by default. include_archived restores migration coverage."""
    from rebar._store.gitutil import _ticket_dirs

    needs: list[str] = []
    rest = 0
    now = hlc.physical_now()
    try:
        names = _ticket_dirs(tracker, include_archived=include_archived)
    except OSError:
        return [], 0
    for name in names:
        path = os.path.join(tracker, name)
        stats = compact_plan.foldable_stats(path, now, horizon)
        probe = compact_plan.has_snapshot(path)
        # unreadable: never select it on the backfill arm, the fold would fail anyway
        has_snapshot = True if probe is None else probe
        if compact_plan.needs_folding(
            stats.count,
            has_snapshot,
            threshold,
            snapshot_alpha=snapshot_alpha,
            pending_source_bytes=stats.pending_source_bytes,
            active_snapshot_bytes=stats.active_snapshot_bytes,
        ):
            needs.append(name)
        else:
            rest += 1
    return needs, rest


def _compact_all_parse(argv: list[str]) -> tuple[bool, int, bool, bool, int | None]:
    """Parse ``compact-all`` flags. Returns ``(dry_run, limit, no_commit, include_archived,
    early_rc)`` where ``early_rc`` is non-None when the caller should return it immediately
    (``--help`` => 0, an unknown option => 1)."""
    dry_run = False
    limit = 0
    no_commit = False
    include_archived = False
    usage = (
        "Usage: rebar store compact --all [--dry-run] [--limit=N] [--no-commit] "
        "[--include-archived]\n"
    )
    for a in argv:
        if a == "--dry-run":
            dry_run = True
        elif a.startswith("--limit="):
            limit = int(a[len("--limit=") :])
        elif a == "--no-commit":
            no_commit = True
        elif a == "--include-archived":
            include_archived = True
        elif a in ("--help", "-h"):
            sys.stdout.write(usage)
            return dry_run, limit, no_commit, include_archived, 0
        else:
            sys.stderr.write(f"Error: unknown option '{a}'\n")
            return dry_run, limit, no_commit, include_archived, 1
    return dry_run, limit, no_commit, include_archived, None


# raw-git-ok: store-maintenance command, seam-internal
def _commit_backfill(tracker: str, compacted: int, folded_ids: set[str] | None = None) -> None:
    """Commit + push whatever the sweep's folds left staged-able, then push.

    Since bug compulsory-pernickety-mantis each fold commits its OWN unit inside its
    locked section, so this normally finds nothing to stage and acts as (a) a safety net
    for any residue and (b) the run's ONE batched push (the per-ticket calls pass
    ``--skip-sync`` to defer the push here — bug prone-octet-cheek). The push must run
    even when nothing is staged, precisely BECAUSE the per-ticket commits are the ones
    waiting to be delivered.

    ``folded_ids`` scopes the staging to the tickets this run actually folded; ``None`` keeps
    the historical whole-tree behaviour for callers that do not track them."""
    sys.stdout.write(f"Staging and committing {compacted} new SNAPSHOT files...\n")
    # Stage ONLY the ticket dirs this sweep folded. `git add -A` staged the whole tree, so a
    # concurrent writer's freshly-written event file — sitting in the worktree for the
    # milliseconds between its rename and its own commit — was swept into the compaction commit
    # and landed under a "chore: backfill SNAPSHOT files" message. Not data loss, but another
    # session's event committed by us, under a message that does not describe it.
    if folded_ids:
        _git(tracker, "add", "--", *[f"{tid}/" for tid in sorted(folded_ids)])
    else:
        _git(tracker, "add", "-A")
    if _git(tracker, "diff", "--cached", "--quiet").returncode == 0:
        sys.stdout.write("No staged changes (the folds committed per ticket).\n")
    else:
        _git(
            tracker,
            *_AUTOMAINT_OFF,
            "commit",
            "-q",
            "--no-verify",
            "-m",
            f"chore: backfill SNAPSHOT files for {compacted} tickets (ticket-compact-all)",
        )
        sys.stdout.write("Committed.\n")
    from rebar._store import push

    push.push_after_commit(tracker)


def _best_effort_push(tracker: str, *, no_commit: bool, dry_run: bool) -> None:
    """Push pending commits even when a sweep folded nothing.
    Skip for dry-run or no-commit. Preserve OptionalDependencyError from a
    misconfigured S3 remote instead of hiding it."""
    if no_commit or dry_run:
        return
    from rebar._store import push

    push.push_after_commit(tracker)


def _snapshot_names(ticket_dir: str) -> set[str]:
    """Return snapshot filenames, or empty on read error."""
    try:
        return {n for n in os.listdir(ticket_dir) if n.endswith("-SNAPSHOT.json")}
    except OSError:
        return set()


def _sweep_should_yield(tracker: str, done: int) -> bool:
    """Check between tickets whether a foreground writer needs the lock.
    Leave remaining optional folds for a later sweep."""
    if not _lock.write_lock_is_busy(tracker):
        return False
    sys.stdout.write("\n")
    logger.info(
        "compaction sweep yielding after %d ticket(s): store write lock busy (holder: %s); "
        "the remaining events stay live for the next sweep",
        done,
        _lock.describe_lock_holder(tracker),
    )
    return True


def _sweep_one_ticket(
    tracker: str,
    tid: str,
    repo_root,
    position_commits: dict[str, str] | None,
    *,
    no_commit: bool = False,
) -> str:
    """Fold one ticket and return '.', '-', or 'E'.
    Count work only when a new snapshot appears because a successful return
    may also mean no fold. Commit each fold under its lock, except when the
    operator requested no-commit. Pushes remain batched."""
    import contextlib
    import io

    argv = [tid, "--threshold=0", "--skip-sync"]
    if no_commit:
        argv.append("--no-commit")
    before = _snapshot_names(os.path.join(tracker, tid))
    with contextlib.redirect_stderr(io.StringIO()):  # bash 2>/dev/null
        rc = compact_cli(argv, repo_root=repo_root, position_commits=position_commits)
    if rc != 0:
        return "E"
    return "." if _snapshot_names(os.path.join(tracker, tid)) - before else "-"


def _sweep_position_map(repo_root) -> dict[str, str] | None:
    """Build one position-keyed introducing-commit map before the sweep locks.
    The authorship ledger reads positions, so a path-keyed map would miss.
    Return None when building fails or yields nothing, allowing per-ticket
    fallback. An empty dict would trigger per-event history walks in the lock."""
    from rebar.attest import authorship as _authorship

    try:
        built = _authorship.build_position_commit_map(repo_root=repo_root)
    except Exception:  # noqa: BLE001 — an unbuildable map degrades to per-ticket walks, never fails the sweep
        return None
    return built or None


def _run_sweep(
    tracker: str, needs: list[str], repo_root, *, no_commit: bool = False
) -> tuple[int, int, set[str], list[str]]:
    """Fold each selected ticket; return ``(compacted, skipped, folded_ids, error_ids)``.

    Extracted from :func:`compact_all_cli` so that function stays under the C901 threshold —
    ``.github/complexity-baseline.json`` is shrink-only and admits no new debt."""
    position_commits = _sweep_position_map(repo_root)
    compacted = 0
    skipped = 0
    folded_ids: set[str] = set()
    error_ids: list[str] = []
    # Tag every store-lock acquisition this loop drives (via _sweep_one_ticket ->
    # _compact_locked) as `op=compact-sweep`, so a writer blocked by the sweep sees a named,
    # safe-to-interrupt holder rather than a bare pid (camerashy-erectable-frog). Ambient, so
    # it also covers a direct `rebar store compact --all` invocation, not only the detached trigger.
    with _lock_owner.operation_label("compact-sweep"):
        for tid in needs:
            if _sweep_should_yield(tracker, compacted + skipped + len(error_ids)):
                break
            outcome = _sweep_one_ticket(
                tracker, tid, repo_root, position_commits, no_commit=no_commit
            )
            if outcome == "E":
                error_ids.append(tid)
            elif outcome == ".":
                compacted += 1
                folded_ids.add(tid)
            else:
                skipped += 1
            sys.stdout.write(outcome)
            sys.stdout.flush()
    return compacted, skipped, folded_ids, error_ids


def compact_all_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar store compact --all`` entry — the recurring store-wide compaction sweep.

    Selects every ticket whose FOLDABLE event count exceeds ``compact.threshold`` and folds
    it, so a ticket that was compacted before and has since grown is folded again. Backfilling
    a ticket that has no SNAPSHOT yet is the same rule applied to a whole log. Since
    compaction left the close path (bug choosy-arthrodic-barbet) this sweep is the store's
    only standing trigger, and it is meant to run OUT OF BAND — in a disposable clone, on a
    schedule — so it never contends for an interactive session's store lock."""

    dry_run, limit, no_commit, include_archived, early_rc = _compact_all_parse(argv)
    if early_rc is not None:
        return early_rc

    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.core.compact import build_compact_all

    # The factory is the parser of record: its namespace drives the four selection
    # toggles below. ``_compact_all_parse`` above is retained ONLY for the two things
    # argparse cannot express byte-exactly — the bespoke ``unknown option`` reject
    # (exit 1, not argparse's 2) and the ``--help``/``-h`` early-exit to stdout — so by
    # the time we parse, ``argv`` holds only accepted flags and the factory never
    # rejects a real invocation (a rejecting factory injected by the governance test
    # does, which is the point).
    try:
        ns = build_compact_all(prog="rebar store compact --all").parse_args(argv)
    except ParseError as exc:
        return render_parse_error(exc)
    dry_run = ns.dry_run
    limit = int(ns.limit) if ns.limit is not None else 0
    no_commit = ns.no_commit
    include_archived = ns.include_archived

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        sys.stderr.write(f"Error: tracker dir not found: {tracker}\n")
        return 1

    try:
        _cfg = config.compose_config(repo_root).compact
        threshold, horizon = _cfg.threshold, _cfg.COMPACTION_HORIZON_NS
        snapshot_alpha = _cfg.snapshot_alpha
    except config.ConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    # Crash-recovery preamble (bug compulsory-pernickety-mantis): converge any partial
    # fold a killed worker abandoned, BEFORE selecting — a reverted ticket becomes
    # foldable again and must be counted. Skipped on --dry-run (no side effects) and
    # free when no journal is pending. Takes the store write lock only when recovering.
    if not dry_run:
        from rebar._commands import compact_recovery

        compact_recovery.recover_abandoned_folds_locked(tracker)

    needs, already = _scan_snapshot_state(
        tracker,
        threshold,
        horizon,
        snapshot_alpha=snapshot_alpha,
        include_archived=include_archived,
    )
    total_needs = len(needs)
    sys.stdout.write(f"Tickets needing no compaction : {already}\n")
    sys.stdout.write(f"Tickets needing compaction    : {total_needs}\n")
    if total_needs == 0:
        sys.stdout.write("Nothing to do.\n")
        _best_effort_push(tracker, no_commit=no_commit, dry_run=dry_run)
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

    sys.stdout.write(f"\nCompacting {total_needs} tickets...\n")
    sys.stdout.write("(. = folded; - = nothing to fold; E = error)\n")
    compacted, skipped, folded_ids, error_ids = _run_sweep(
        tracker, needs, repo_root, no_commit=no_commit
    )

    sys.stdout.write("\n\n")
    sys.stdout.write(
        f"Done: {compacted} compacted, {skipped} nothing to fold, {len(error_ids)} errors "
        f"(of {total_needs} attempted)\n"
    )
    if error_ids:
        sys.stderr.write("Errored tickets:\n")
        for tid in error_ids:
            sys.stderr.write(f"  {tid}\n")

    if compacted > 0 and not no_commit:
        _commit_backfill(tracker, compacted, folded_ids)  # commits, then pushes
    else:
        _best_effort_push(tracker, no_commit=no_commit, dry_run=dry_run)

    # Record that a sweep ran, whatever it folded. Both triggers stamp through this one call,
    # so a store swept by CI does not also detach a worker on its next close, and a store with
    # no CI ages its own stamp. A dry run stamps nothing — it did not sweep.
    if not dry_run:
        from rebar._commands import compact_trigger

        compact_trigger.record_sweep(tracker)

    return 2 if error_ids else 0


# Sweep-only flags: valid only beside ``--all`` (the one-ticket arm has no such options).
_SWEEP_ONLY_FLAGS = ("--dry-run", "--limit", "--include-archived")


def compact_dispatch_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar store compact`` entry: one ticket, or the store-wide sweep under ``--all``.

    ``--all`` (before any ``--``) selects :func:`compact_all_cli` with the remaining
    arguments, exactly as the former ``compact-all`` spelling received them. A sweep-only
    flag without ``--all`` is a usage error (exit 2) rather than a silently ignored option.
    Anything else is the one-ticket arm, :func:`compact_cli`, unchanged.
    """
    end = argv.index("--") if "--" in argv else len(argv)
    head = argv[:end]
    if "--all" in head:
        return compact_all_cli([a for a in head if a != "--all"] + argv[end:], repo_root=repo_root)
    sweep_only = next((a for a in head if a.split("=", 1)[0] in _SWEEP_ONLY_FLAGS), None)
    if sweep_only is not None:
        flag = sweep_only.split("=", 1)[0]
        sys.stderr.write(f"Error: {flag} applies only to the sweep: rebar store compact --all\n")
        return 2
    return compact_cli(argv, repo_root=repo_root)
