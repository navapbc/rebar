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
import logging
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
from rebar._store import lock as _lock
from rebar._store import lock_owner as _lock_owner

logger = logging.getLogger(__name__)

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


def compact_cli(
    argv: list[str], *, repo_root=None, position_commits: dict[str, str] | None = None
) -> int:
    """``rebar compact <id>`` entry.

    ``position_commits`` is the OPTIONAL prebuilt position->commit map the store-wide sweep
    threads in so the authorship-ledger history walk does not run inside the store write lock
    (see :func:`compact_all_cli`). Absent, the fold builds its own per-ticket map as before."""
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
        _cfg = config.compose_config(repo_root).compact
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
    """How many of *ticket_dir*'s live events the fold would actually squash right now.

    Deliberately asks the SAME question :func:`compact_txn._compact_locked` asks, through the
    SAME predicate (:func:`rebar._commands._compact_policy.is_foldable`) and the same
    configured horizon, so selection and work cannot drift. Counting merely LIVE events
    instead would select a ticket whose excess events are all inside the horizon; the fold
    would write nothing, and the next sweep would select it again — churn.

    Matches the fold's candidate set exactly — ``*.json`` excluding dotfiles, already-retired
    sources and ``-SYNC.json`` bridge metadata — and, like
    :func:`compact_txn._parse_candidate_events`, DROPS forward-compat unknown-type events. A
    newer clone's event type is preserved untouched by the fold rather than squashed, so
    counting it here would make selection promise work the fold will not do, and a ticket whose
    excess was all unknown-type would be selected on every sweep forever.

    The ticket's OWN ``-SNAPSHOT.json`` is excluded too, for a different reason: the fold does
    absorb a prior snapshot into the new one, but a snapshot is already-folded STATE, not
    pending work. Counting it left every settled ticket reporting one unit of outstanding work
    forever, which is exactly the kind of permanent off-by-one that turns a threshold into a
    churn source at low settings.

    An unreadable or undecodable file counts as foldable with an unknown timestamp only when
    the horizon is off, mirroring ``is_foldable``'s treatment of a ``None`` timestamp — and
    mirroring the fold, which keeps such a file as a candidate rather than dropping it."""
    from rebar.reducer import KNOWN_EVENT_TYPES

    count = 0
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return 0
    for name in names:
        if name.startswith(".") or not name.endswith(".json") or name.endswith("-SYNC.json"):
            continue
        if name.endswith("-SNAPSHOT.json"):
            continue  # already-folded state, not pending work
        try:
            with open(os.path.join(ticket_dir, name), encoding="utf-8") as fh:
                event = json.load(fh)
            etype = event.get("event_type", "")
            raw_ts = event.get("timestamp")
        except (json.JSONDecodeError, OSError):
            etype, raw_ts = "", None
        if etype and etype not in KNOWN_EVENT_TYPES:
            continue  # forward-compat: the fold preserves these rather than squashing them
        ts = raw_ts if isinstance(raw_ts, int) else None
        if is_foldable(ts, now, horizon):
            count += 1
    return count


def _scan_snapshot_state(
    tracker: str, threshold: int = 0, horizon: int = 0, *, include_archived: bool = False
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
    is foldable at horizon 0, so any ticket with events is selected.

    Walks ACTIVE tickets only by default, via the shared iterator
    (:func:`rebar._store.gitutil._ticket_dirs`): an archived ticket was terminally
    folded at archive time, so re-scanning it is pure history cost. ``include_archived``
    restores the full walk (the migration door for tickets archived before the fold existed)."""
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
        foldable = _foldable_event_count(path, now, horizon)
        try:
            has_snapshot = any(n.endswith("-SNAPSHOT.json") for n in os.listdir(path))
        except OSError:
            has_snapshot = True  # unreadable: never select it, the fold would fail anyway
        if foldable > threshold or (foldable > 0 and not has_snapshot):
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
    usage = "Usage: ticket compact-all [--dry-run] [--limit=N] [--no-commit] [--include-archived]\n"
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
    """Honour the sweep's push contract on the paths that commit NOTHING.

    The push is about not stranding commits, so it must not depend on whether this sweep
    happened to fold anything: an earlier write in the same session can be sitting unpushed,
    and a sweep is often the last thing to run. Previously this rode along for free because
    every selected ticket was counted as compacted (even a fold that wrote nothing), so
    ``compacted > 0`` was effectively "we selected something". Now that the count is honest,
    a sweep that folds nothing would silently skip the push — so the two are decoupled.

    ``--no-commit`` opts out, exactly as it does for the commit itself, and ``--dry-run`` opts
    out because a dry run must have NO side effects — pushing is a side effect even when this
    invocation committed nothing of its own, since it delivers whatever was already pending.

    NOT unconditionally best-effort, despite the name: :func:`rebar._store.push.push_after_commit`
    deliberately re-raises :class:`OptionalDependencyError` so a misconfigured S3 remote halts
    loudly with an actionable install message instead of being swallowed. That fail-closed
    escape is preserved here on purpose — it is the same contract ``_commit_backfill``'s push
    has always had, and silencing it for the sweep would make the one configuration error the
    store cannot recover from invisible on its only standing trigger."""
    if no_commit or dry_run:
        return
    from rebar._store import push

    push.push_after_commit(tracker)


def _snapshot_names(ticket_dir: str) -> set[str]:
    """The ticket dir's current ``*-SNAPSHOT.json`` filenames (empty on any read error)."""
    try:
        return {n for n in os.listdir(ticket_dir) if n.endswith("-SNAPSHOT.json")}
    except OSError:
        return set()


def _sweep_should_yield(tracker: str, done: int) -> bool:
    """Whether the sweep should stand aside for a foreground writer, checked BETWEEN tickets.

    The trigger probes the store lock once before starting, which only helps a sweep that was
    never going to hurt: a run that begins on a quiet store and then folds for minutes never
    looks again, so every writer arriving mid-run waits it out. Compaction is optional
    housekeeping — it yields, and the remaining tickets keep for the next pass.

    A separate function rather than an inline branch because ``compact_all_cli`` sits at the
    C901 threshold and ``.github/complexity-baseline.json`` admits no new debt."""
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
    """Fold ONE ticket for the sweep; return its progress character.

    ``"."`` folded, ``"-"`` nothing to fold, ``"E"`` errored. The outcome is decided by the
    ARTIFACT — did a new ``*-SNAPSHOT.json`` appear — not by the return code, because
    ``_compact_locked`` returns 0 for a successful fold AND for every no-op branch (below
    threshold, nothing older than the horizon, no safe placement gap). Counting ``rc == 0`` as
    work inflated the tally and hid a sweep that folded nothing.

    Each fold COMMITS its own unit inside its locked section (bug
    compulsory-pernickety-mantis): the sweep used to pass ``--no-commit`` and batch one
    commit at the end, which left every fold of the run sitting dirty in the shared
    worktree until ``_commit_backfill`` — a worker killed mid-sweep stranded the whole
    batch as the tree dirt that wedges reconverge. ``no_commit`` survives only as the
    operator's explicit ``compact-all --no-commit`` request. The PUSH stays batched
    (``--skip-sync``; bug prone-octet-cheek) — commits are local and cheap, pushes are not."""
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
    """The ONE history walk for a whole sweep, built BEFORE any lock is taken.

    Without this, each ticket's authorship ledger ran its own ``git log --full-history`` walk
    INSIDE the store write lock (``compact_txn._build_authorship_ledger``). That is bug 7084
    one scale up: 7084 found the same walk running per signed EVENT and batched it to
    per-TICKET, which was right for compact-on-close because it folds ONE ticket. Once
    ``compact-all`` became the standing sweep, per-ticket became the new per-event — thousands
    of full-history walks, each holding the global write lock, observed live as a fresh git
    child every few seconds at 100% CPU while every writer in the fleet stalled.

    ``git log`` is read-only and never needed the lock. Building the map once costs one walk
    for the entire run and leaves the locked section with only the SNAPSHOT write, the renames
    and the commit.

    MUST be the POSITION-keyed builder. The ledger looks up by position, so the path-keyed
    :func:`~rebar.attest.authorship.build_introducing_commit_map` would miss every entry and
    fall silently back to per-event walks — same output, same cost, no error to notice.

    Returns ``None`` — never an empty dict — when the map cannot be built or comes back empty,
    and that distinction is load-bearing rather than stylistic.
    :func:`compact_txn._build_authorship_ledger` rebuilds its own per-ticket map only when the
    argument ``is None``; an empty dict is a perfectly valid map that simply misses every
    lookup, so passing ``{}`` would send every event down the per-EVENT fallback
    (``resolve_event_commit``, then ``resolve_position_commit``) INSIDE the store write lock.
    That is not a degradation to the old behaviour — it is the full bug-7084 pathology, worse
    than the per-ticket walk this function exists to remove. ``None`` degrades honestly: each
    fold rebuilds its own per-ticket map, exactly as before this optimisation."""
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
    # it also covers a direct `rebar compact-all` invocation, not only the detached trigger.
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
    """``rebar compact-all`` entry — the recurring store-wide compaction sweep.

    Selects every ticket whose FOLDABLE event count exceeds ``compact.threshold`` and folds
    it, so a ticket that was compacted before and has since grown is folded again. Backfilling
    a ticket that has no SNAPSHOT yet is the same rule applied to a whole log. Since
    compaction left the close path (bug choosy-arthrodic-barbet) this sweep is the store's
    only standing trigger, and it is meant to run OUT OF BAND — in a disposable clone, on a
    schedule — so it never contends for an interactive session's store lock."""

    dry_run, limit, no_commit, include_archived, early_rc = _compact_all_parse(argv)
    if early_rc is not None:
        return early_rc

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        sys.stderr.write(f"Error: tracker dir not found: {tracker}\n")
        return 1

    try:
        _cfg = config.compose_config(repo_root).compact
        threshold, horizon = _cfg.threshold, _cfg.COMPACTION_HORIZON_NS
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
        tracker, threshold, horizon, include_archived=include_archived
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
