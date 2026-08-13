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

import json
import os
import sys

from rebar import config
from rebar._commands._compact_policy import is_foldable
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
def _foldable_event_count(ticket_dir: str, now: int, horizon: int) -> int:
    """How many of *ticket_dir*'s live events the fold would actually squash right now.

    Deliberately asks the SAME question :func:`compact_txn._compact_locked` asks, through the
    SAME predicate (:func:`rebar._commands._compact_policy.is_foldable`) and the same
    configured horizon, so selection and work cannot drift. Counting merely LIVE events
    instead would select a ticket whose excess events are all inside the horizon; the fold
    would write nothing, and the next sweep would select it again — churn.

    Matches the fold's candidate set: ``*.json``, excluding dotfiles, already-retired sources
    and ``-SYNC.json`` bridge metadata. An unreadable or undecodable file counts as foldable
    with an unknown timestamp only when the horizon is off, mirroring ``is_foldable``'s
    treatment of a ``None`` timestamp."""
    count = 0
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return 0
    for name in names:
        if name.startswith(".") or not name.endswith(".json") or name.endswith("-SYNC.json"):
            continue
        try:
            with open(os.path.join(ticket_dir, name), encoding="utf-8") as fh:
                raw_ts = json.load(fh).get("timestamp")
        except (json.JSONDecodeError, OSError):
            raw_ts = None
        ts = raw_ts if isinstance(raw_ts, int) else None
        if is_foldable(ts, now, horizon):
            count += 1
    return count


def _scan_snapshot_state(
    tracker: str, threshold: int = 0, horizon: int = 0
) -> tuple[list[str], int]:
    """Return (ticket ids worth compacting, count of the rest), sorted by name.

    A ticket is selected on EITHER of two arms — this is a widening of the historical rule,
    deliberately not a replacement:

    * **Backfill** (the historical arm, preserved): it has no ``-SNAPSHOT.json`` and has at
      least one foldable event. Every ticket still earns its first SNAPSHOT regardless of size.
    * **Recurrence** (the new arm): its FOLDABLE event count exceeds *threshold*, whatever its
      snapshot state.

    The recurrence arm is why this exists. Selecting ONLY on the backfill arm made
    ``compact-all`` a one-time operation: a ticket folded once and since grown by hundreds of
    events had a SNAPSHOT, so it was never folded again. That was survivable while every close
    compacted inline; since compaction left the close path (bug choosy-arthrodic-barbet) this
    sweep is the store's ONLY standing trigger, and a trigger that cannot re-fire is no
    trigger.

    Selecting ONLY on the threshold arm would have been a silent regression in the other
    direction — a ticket with fewer events than the threshold would never get a first SNAPSHOT
    at all — which is why both arms are kept.

    Both arms converge. After a backfill the ticket has a SNAPSHOT (arm 1 no longer applies)
    and its live count is back below the threshold (arm 2 does not apply), so the next sweep
    leaves it alone.

    ``threshold=0`` / ``horizon=0`` keep the historical call shape working — every live event
    is foldable at horizon 0, so any ticket with events is selected."""
    needs: list[str] = []
    rest = 0
    now = hlc.physical_now()
    try:
        entries = sorted(os.scandir(tracker), key=lambda e: e.name)
    except OSError:
        return [], 0
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        foldable = _foldable_event_count(entry.path, now, horizon)
        try:
            has_snapshot = any(n.endswith("-SNAPSHOT.json") for n in os.listdir(entry.path))
        except OSError:
            has_snapshot = True  # unreadable: never select it, the fold would fail anyway
        if foldable > threshold or (foldable > 0 and not has_snapshot):
            needs.append(entry.name)
        else:
            rest += 1
    return needs, rest


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


def _best_effort_push(tracker: str, no_commit: bool) -> None:
    """Honour the sweep's best-effort push contract on the paths that commit NOTHING.

    The push is about not stranding commits, so it must not depend on whether this sweep
    happened to fold anything: an earlier write in the same session can be sitting unpushed,
    and a sweep is often the last thing to run. Previously this rode along for free because
    every selected ticket was counted as compacted (even a fold that wrote nothing), so
    ``compacted > 0`` was effectively "we selected something". Now that the count is honest,
    a sweep that folds nothing would silently skip the push — so the two are decoupled.

    ``--no-commit`` opts out, exactly as it does for the commit itself."""
    if no_commit:
        return
    from rebar._store import push

    push.push_after_commit(tracker)


def _snapshot_names(ticket_dir: str) -> set[str]:
    """The ticket dir's current ``*-SNAPSHOT.json`` filenames (empty on any read error)."""
    try:
        return {n for n in os.listdir(ticket_dir) if n.endswith("-SNAPSHOT.json")}
    except OSError:
        return set()


def compact_all_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar compact-all`` entry — the recurring store-wide compaction sweep.

    Selects every ticket whose FOLDABLE event count exceeds ``compact.threshold`` and folds
    it, so a ticket that was compacted before and has since grown is folded again. Backfilling
    a ticket that has no SNAPSHOT yet is the same rule applied to a whole log. Since
    compaction left the close path (bug choosy-arthrodic-barbet) this sweep is the store's
    only standing trigger, and it is meant to run OUT OF BAND — in a disposable clone, on a
    schedule — so it never contends for an interactive session's store lock."""
    import contextlib
    import io

    dry_run, limit, no_commit, early_rc = _compact_all_parse(argv)
    if early_rc is not None:
        return early_rc

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        sys.stderr.write(f"Error: tracker dir not found: {tracker}\n")
        return 1

    try:
        _cfg = config.load_config(repo_root).compact
        threshold, horizon = _cfg.threshold, _cfg.COMPACTION_HORIZON_NS
    except config.ConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    needs, already = _scan_snapshot_state(tracker, threshold, horizon)
    total_needs = len(needs)
    sys.stdout.write(f"Tickets needing no compaction : {already}\n")
    sys.stdout.write(f"Tickets needing compaction    : {total_needs}\n")
    if total_needs == 0:
        sys.stdout.write("Nothing to do.\n")
        _best_effort_push(tracker, no_commit)
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
    skipped = 0
    error_ids: list[str] = []
    sys.stdout.write(f"\nCompacting {total_needs} tickets...\n")
    sys.stdout.write("(. = folded; - = nothing to fold; E = error)\n")
    for tid in needs:
        # Count a ticket as compacted only when a NEW SNAPSHOT actually appeared. The return
        # code cannot answer this: _compact_locked returns 0 for a successful fold AND for
        # every no-op branch (below threshold, nothing older than the horizon, no safe
        # placement gap), so `rc == 0` counted all of them as work — inflating the tally and
        # hiding a sweep that folded nothing. Observing the artifact is uniform across all
        # three branches and needs no change to the fold's return contract.
        before = _snapshot_names(os.path.join(tracker, tid))
        with contextlib.redirect_stderr(io.StringIO()):  # bash 2>/dev/null
            rc = compact_cli(
                [tid, "--threshold=0", "--skip-sync", "--no-commit"], repo_root=repo_root
            )
        if rc != 0:
            error_ids.append(tid)
            sys.stdout.write("E")
        elif _snapshot_names(os.path.join(tracker, tid)) - before:
            compacted += 1
            sys.stdout.write(".")
        else:
            skipped += 1
            sys.stdout.write("-")
        sys.stdout.flush()

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
        _commit_backfill(tracker, compacted)  # commits, then pushes
    else:
        _best_effort_push(tracker, no_commit)

    return 2 if error_ids else 0
