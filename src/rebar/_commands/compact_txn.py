"""The compaction TRANSACTION — the locked critical section that folds a ticket's
event log into one SNAPSHOT.

Extracted from ``compact.py`` (task b2bb) so the transaction's change-driver — write
safety under the store lock: horizon partitioning, snapshot placement, atomic retire
with rollback — has a home separate from the CLI surface and from the fsck rebuild
engine. ``compact.py`` is now a thin CLI facade over this module and
``compact_rebuild``.

Layering: ``compact`` -> ``compact_txn``, and ``compact_rebuild`` -> ``compact_txn``.
This module imports NEITHER of them, which is why the SHARED snapshot primitives
(``_git``, ``_git_author``, ``_snapshot_strip_keys``, ``_build_authorship_ledger``)
live here rather than in ``compact_rebuild``: both the normal fold and the rebuild
path need them, and hosting them in the repair module would make the dependency
circular. It DOES import ``fsck_repair.is_snapshot_orphan`` (a leaf predicate;
``fsck_repair`` imports no compact module, so this stays acyclic) — the fold's
orphan-exclusion guard and fsck's scan must share ONE orphan definition (bug f96b).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from rebar._commands import _seam, compact_recovery
from rebar._commands._compact_policy import is_foldable
from rebar._commands.fsck_repair import is_snapshot_orphan
from rebar._store import compat, event_append, fsutil, hlc, lock
from rebar._store.canonical import canonical_str
from rebar._store.gitutil import run_git_write
from rebar.reducer import KNOWN_EVENT_TYPES, reduce_ticket
from rebar.reducer._cache import RETIRED_SUFFIX

logger = logging.getLogger(__name__)


# raw-git-ok: store-maintenance command, seam-internal
def _git(tracker: str, *args: str):
    return run_git_write(tracker, *args, check=False)


def _git_author() -> str:
    cp = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    if cp.returncode != 0:
        return "system"
    return cp.stdout.strip()


def _sync_before_compact(tracker: str) -> None:
    """Pull the latest tickets before compacting (best-effort, in-process) so a
    remote SNAPSHOT written by another agent is visible and local compaction can
    defer to it. Honors the ``sync.pull`` policy and is fully best-effort (every
    fetch failure is swallowed). Replaces the former dead ``ticket sync`` shell-out
    (no such subcommand existed; ``shell=True`` injection smell)."""
    from rebar._engine_support import reads

    reads.ensure_fresh(tracker)


def _snapshot_strip_keys() -> set[str]:
    """Keys to drop from a SNAPSHOT's compiled_state before persisting. ``updated_at`` is
    a derived presentation field re-computed every replay. The legacy ``signature`` mirror
    is ALWAYS dropped (task 7ed9 hardcoded never-emit) — new snapshots carry only the
    kind-keyed ``attestations`` map. The mirror is still re-derived in memory on replay
    (reducer ``process_signature``), so verification keeps working on a compacted ticket."""
    return {"updated_at", "signature"}


def _build_authorship_ledger(
    event_paths: list[str], repo_root, *, position_commits: dict[str, str] | None = None
) -> list[dict]:
    """Independently scan the folded event files and build the SNAPSHOT authorship ledger
    (epic gnu-whale-ichor / 117b): one ``{event_uuid, content_hash, signature, signer_pubkey,
    position}`` record per folded event that carries an ``author_sig``. This preserves the
    signed events so the merge-gate ``rebar verify-authorship`` can re-verify them AFTER the
    raw event files are retired (folded into the SNAPSHOT).

    Everything the gate needs is captured at compaction time (when the raw file + git history
    are still present): ``content_hash`` (the canonical content binding), the ``signature``
    envelope, the ``signer_pubkey`` that actually verifies it (``identify_signer``; ``null`` for
    a forged/foreign sig — the entry is STILL recorded so the gate can flag it), and the
    ``position`` (the ``{timestamp}-{uuid}`` string plus its resolved introducing
    ``commit_sha``) for the commit-ancestry era check. Unsigned events are omitted (the
    presence-only count lives in ``compiled_state`` already). Best-effort throughout — a
    lookup/decode failure records ``null`` rather than raising.

    ``position_commits`` is an OPTIONAL prebuilt position->commit map. Supplied, it is used
    verbatim and NO ``git log`` runs here at all — which is the whole point for the store-wide
    sweep: :func:`compact_all_cli` builds ONE map for the entire run
    (:func:`~rebar.attest.authorship.build_position_commit_map`) before taking any lock, so the
    per-ticket walk below never executes inside the store write lock. It MUST be the
    POSITION-keyed map: the lookup below is ``position_commits.get(position_str)``, so the
    path-keyed :func:`~rebar.attest.authorship.build_introducing_commit_map` would miss every
    entry and fall silently back to the per-event resolver — correct output, unchanged cost,
    nothing to notice.

    Left ``None`` (the single-ticket ``rebar compact`` path, and the fsck rebuild in
    ``compact_rebuild``) it builds its own per-ticket map exactly as before, which is the right
    cost when folding ONE ticket.

    The introducing commits are otherwise resolved for the WHOLE ticket in ONE directory-scoped
    ``git log`` walk (:func:`~rebar.attest.authorship.build_ticket_position_commit_map`),
    not one full-history walk per signed event. That per-event form was 99.2% of a
    measured 48.1s ``compact-on-close`` — all of it inside the store write lock, starving
    every concurrent writer (bug 7084 / remediation R1). Attribution is unchanged: a
    position the map misses still falls back to the per-event resolver and then to the
    global one, exactly as before, so the batching can only ever cost time — never a
    wrong or missing commit in the attestation chain."""
    from rebar.attest import authorship, dsse

    if position_commits is None:
        position_commits = {}
        if event_paths:
            position_commits = authorship.build_ticket_position_commit_map(
                os.path.dirname(event_paths[0]), repo_root=repo_root
            )

    ledger: list[dict] = []
    for path in event_paths:
        try:
            with open(path, encoding="utf-8") as f:
                ev = json.load(f)
        except (OSError, ValueError):
            continue
        sig = ev.get("author_sig")
        if not sig:
            continue
        author_id = ev.get("author_id")
        position_str = f"{ev.get('timestamp')}-{ev.get('uuid')}"
        ticket_dir = os.path.dirname(path)

        signer_pubkey = None
        try:
            envelope = dsse.decode(sig if isinstance(sig, str) else "")
            signer_pubkey = authorship.identify_signer(
                envelope, str(author_id), repo_root=repo_root
            )
        except Exception:  # noqa: BLE001 — a decode/lookup failure records a null signer
            signer_pubkey = None

        commit_sha = position_commits.get(position_str)
        if commit_sha is None:
            # Map miss (an empty map from a git failure, or a file introduced only inside
            # a merge commit): fall back to the per-event resolver, which is what this
            # loop used to call unconditionally.
            commit_sha = authorship.resolve_event_commit(
                position_str, ticket_dir, repo_root=repo_root
            )
        if commit_sha is None:
            # resolve_event_commit can return None at fold time (its ticket-scoped pathspec
            # may miss the introducing commit). Fall back to the GLOBAL position resolver so
            # we persist the REAL introducing commit rather than a null the merge-gate later
            # fail-closes to ``key_not_valid_at_era`` (bug B). Only persist null if BOTH fail.
            tracker = os.path.dirname(ticket_dir)
            commit_sha = authorship.resolve_position_commit(
                position_str, tracker, repo_root=repo_root
            )

        ledger.append(
            {
                "event_uuid": ev.get("uuid"),
                "content_hash": authorship.authorship_content_hash(ev),
                "signature": sig,
                "signer_pubkey": signer_pubkey,
                "position": {"commit_sha": commit_sha, "position": position_str},
            }
        )
    return ledger


def _maybe_pause_at_rename_barrier(n_renamed: int) -> None:
    """Test-only failpoint (inert in production). When the environment variable
    ``REBAR_TEST_COMPACT_RENAME_BARRIER`` names a directory, pause after the FIRST
    source rename (``n_renamed == 1``) so a test can reliably SIGKILL a real
    compactor process in the mid-retirement window — SNAPSHOT already written, one
    source ``*.retired``, the rest still active, nothing committed and the write lock
    still held. The hook writes a ``reached`` marker (its PID) and then blocks until a
    ``release`` file appears, so the test controls the kill point deterministically
    with no timing race. Guarded entirely by the env var: unset ⇒ immediate return."""
    barrier = os.environ.get("REBAR_TEST_COMPACT_RENAME_BARRIER")  # read-via: test-failpoint
    if not barrier or n_renamed != 1:
        return
    bdir = Path(barrier)
    (bdir / "reached").write_text(str(os.getpid()), encoding="utf-8")
    release = bdir / "release"
    while not release.exists():
        time.sleep(0.02)


def _parse_candidate_events(ticket_dir: str) -> list[tuple[str, str, int | None]]:
    """Re-list the ticket's event files inside the lock (authoritative) and parse each
    once into ``(path, uuid, timestamp)``.

    Excludes ``-SYNC.json`` (bridge metadata that must survive compaction) and drops
    forward-compat unknown-type events (written by a newer clone), which are preserved
    untouched — never snapshotted or deleted. An unreadable/undecodable file is kept as a
    candidate with a basename stand-in for its uuid and no timestamp, exactly as before."""
    candidates = sorted(
        os.path.join(ticket_dir, f)
        for f in os.listdir(ticket_dir)
        if f.endswith(".json") and not f.startswith(".") and not f.endswith("-SYNC.json")
    )
    parsed: list[tuple[str, str, int | None]] = []  # (path, uuid, ts)
    for fp in candidates:
        try:
            with open(fp, encoding="utf-8") as f:
                ev = json.load(f)
            etype = ev.get("event_type", "")
            euuid = ev.get("uuid", os.path.basename(fp))
            raw_ts = ev.get("timestamp")
            ets = raw_ts if isinstance(raw_ts, int) else None
        except (json.JSONDecodeError, OSError):
            etype, euuid, ets = "", os.path.basename(fp), None
        if etype and etype not in KNOWN_EVENT_TYPES:
            continue
        parsed.append((fp, euuid, ets))
    return parsed


def _is_snapshot_path(fp: str) -> bool:
    """True when *fp* names a SNAPSHOT event file (live or ``.retired``)."""
    return os.path.basename(fp).removesuffix(RETIRED_SUFFIX).endswith("-SNAPSHOT.json")


def _defer_presnapshot_foldables(
    old: list[tuple[str, str, int | None]],
    young: list[tuple[str, str, int | None]],
) -> list[tuple[str, str, int | None]]:
    """Drop foldables governed by a SNAPSHOT that is NOT folding this pass.

    A fold-horizon race can leave a pre-snapshot event live (see
    ``fsck_repair.is_snapshot_orphan``). If its governing snapshot is still young
    (inside the horizon) when the event ages out, folding the event WITHOUT the
    snapshot would place the new SNAPSHOT's timestamp before the governing one —
    positionally buried on replay, i.e. the same silent loss this fix exists to
    prevent. Defer such events until the governing snapshot folds with them.
    """
    snaps = [os.path.basename(fp) for (fp, _u, _ts) in young if _is_snapshot_path(fp)]
    if not snaps:
        return old
    governing = max(snaps)
    return [(fp, u, ts) for (fp, u, ts) in old if os.path.basename(fp) >= governing]


def _exclude_snapshot_orphans(
    old: list[tuple[str, str, int | None]],
) -> list[tuple[str, str, int | None]]:
    """Exclude fsck-orphans from the fold set so the fold cannot LAUNDER them.

    Anchor: the latest prior SNAPSHOT present in the fold set. An active event that
    sorts before that snapshot and is absent from its ``source_event_uuids`` was
    never applied to its compiled_state (the fold-horizon race — the union merge
    landed it after the fold enumerated its inputs). Retiring + citing it here
    would erase the ORPHAN_EVENT finding while its effect stays lost — turning
    repairable damage into silent, permanent loss (bug f96b). Instead the orphan
    stays a live, un-cited file: the finding and the ``fsck --repair-snapshots``
    window survive indefinitely; the fold does NOT absorb (repair owns that, with
    its auto-recover/human-triage type routing). The predicate is fsck's own
    (``fsck_repair.is_snapshot_orphan``), so fold and scan cannot disagree.

    A first fold (no prior snapshot in the set) excludes nothing. An unreadable
    anchor snapshot excludes nothing — fsck cannot classify orphans against it
    either (``_check_snapshot`` returns no findings for it).
    """
    snaps = [fp for (fp, _u, _ts) in old if _is_snapshot_path(fp)]
    if not snaps:
        return old
    anchor = max(snaps, key=os.path.basename)
    try:
        with open(anchor, encoding="utf-8") as f:
            sources = set(json.load(f).get("data", {}).get("source_event_uuids", []))
    except (json.JSONDecodeError, OSError):
        return old
    anchor_name = os.path.basename(anchor)
    kept: list[tuple[str, str, int | None]] = []
    for fp, u, ts in old:
        name = os.path.basename(fp)
        etype = name.removesuffix(".json").rsplit("-", 1)[-1]
        if is_snapshot_orphan(name, etype, u, anchor_name, sources):
            logger.warning(
                "compact: excluding pre-snapshot orphan %s from the fold — not captured by "
                "%s; left live for fsck --repair-snapshots",
                name,
                anchor_name,
            )
            continue
        kept.append((fp, u, ts))
    return kept


def _build_snapshot_event(
    tracker: str,
    ticket_dir: str,
    compiled_state: dict,
    source_uuids: list[str],
    snapshot_ts: int,
) -> tuple[dict, str]:
    """Assemble the SNAPSHOT event envelope and its destination path.

    Denormalized author attribution (epic gnu-whale-ichor) is stamped onto the envelope.
    There is no ``repo_root`` parameter on this path, so it is derived from the tracker —
    mirroring the derivation in ``compact_rebuild.rebuild_snapshot_from_full_log``."""
    snapshot_uuid = str(uuid.uuid4())
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
    snapshot_event.update(_seam.attribution_fields(os.path.dirname(os.path.realpath(tracker))))
    final_path = os.path.join(
        ticket_dir, event_append.event_filename(snapshot_ts, snapshot_uuid, "SNAPSHOT")
    )
    return snapshot_event, final_path


def _retire_folded_sources(fold_files: list[str], ticket_id: str, final_path: str) -> int:
    """I1: RENAME folded sources to ``*.retired`` rather than deleting them. Returns 0 on
    success, 1 on failure (having already written the operator-facing stderr line).

    The SNAPSHOT is written atomically FIRST by the caller, so a crash mid-rename leaves a
    valid SNAPSHOT plus some already-retired sources; the SNAPSHOT-present short-circuit
    makes a re-compact a no-op, and an existing ``.retired`` target is skipped (idempotent).
    A rename failure is logged (never swallowed) and every completed rename is reversed
    before we abort, so the fold is atomic: either all sources are retired or none are."""
    renamed: list[tuple[str, str]] = []
    try:
        for fp in fold_files:
            retired = fp + RETIRED_SUFFIX
            if os.path.exists(retired):
                continue  # idempotent re-run: source already retired
            os.rename(fp, retired)
            renamed.append((fp, retired))
            logger.info("compact: retired folded event %s", os.path.basename(fp))
            _maybe_pause_at_rename_barrier(len(renamed))
    except OSError:
        logger.warning(
            "compact: failed to retire a folded event for %s — reversing %d rename(s)",
            ticket_id,
            len(renamed),
            exc_info=True,
        )
        rollback_clean = True
        for orig, retired in reversed(renamed):
            try:
                os.rename(retired, orig)
            except OSError:
                rollback_clean = False
                logger.warning(
                    "compact: could not reverse rename %s -> %s", retired, orig, exc_info=True
                )
        if rollback_clean:
            # CLEAN rollback: every completed rename was reversed, so the store is
            # back to its exact pre-fold state and the uncommitted SNAPSHOT is a
            # stray artifact — remove it. (Preserves the original behavior.)
            try:
                os.remove(final_path)
            except OSError:
                logger.warning("compact: could not remove uncommitted SNAPSHOT %s", final_path)
            sys.stderr.write("Error: failed to retire folded events while holding lock\n")
            return 1
        # INCOMPLETE rollback: at least one reverse-rename failed, so a source is
        # stuck as ``*.retired`` while its folded effect lives ONLY in the SNAPSHOT
        # we wrote. We MUST intentionally RETAIN the SNAPSHOT here — removing it
        # would drop that source's effect from BOTH an active event and the
        # snapshot (silent data loss, the hazard this branch exists to avoid).
        # Retaining it leaves a SNAPSHOT_INCONSISTENT state (a SNAPSHOT plus a
        # reversed-to-active source) that ``fsck --repair-snapshots`` already
        # rebuilds. Reads are already correct in this mixed window: the
        # reversed-to-active source keeps its original (pre-snapshot) filename, so
        # it is positionally skipped during replay and never double-counted. Do NOT
        # "simplify" this back into an unconditional ``os.remove(final_path)``.
        logger.warning(
            "compact: rollback incomplete for %s — SNAPSHOT %s retained; run fsck",
            ticket_id,
            final_path,
            exc_info=True,
        )
        sys.stderr.write(
            "Error: failed to retire folded events while holding lock; rollback "
            "incomplete (a folded source is stranded) — the SNAPSHOT is retained "
            "to avoid data loss. Run `rebar fsck --repair-snapshots` to reconcile.\n"
        )
        return 1
    return 0


# raw-git-ok: store-maintenance command, seam-internal
def _commit_compaction(tracker: str, ticket_id: str) -> int:
    """Stage and commit the ticket's compacted directory. Returns 0 on success (including
    the nothing-staged case), 1 on a git failure, having written the operator-facing line."""
    add = _git(tracker, "add", "-A", f"{ticket_id}/")
    if add.returncode != 0:
        # Include git's stderr: the seam's lock-exhaustion guidance rides in it (9305).
        sys.stderr.write(f"Error: git operation failed while holding lock: {add.stderr}\n")
        return 1
    staged = _git(tracker, "diff", "--cached", "--quiet")
    if staged.returncode != 0:
        commit = _git(tracker, "commit", "-q", "--no-verify", "-m", f"ticket: COMPACT {ticket_id}")
        if commit.returncode != 0:
            sys.stderr.write(f"Error: git operation failed while holding lock: {commit.stderr}\n")
            return 1
    return 0


def _rollback_fold(tracker: str, ticket_id: str, final_path: str, fold_files: list[str]) -> None:
    """Best-effort WHOLE-fold rollback to the pre-fold state (bug
    compulsory-pernickety-mantis): rename every ``*.retired`` source back, remove the
    uncommitted SNAPSHOT, and unstage the ticket dir (a death between ``git add`` and
    ``git commit`` leaves a dirty INDEX that aborts the union merge just like dirty files).

    Mirrors ``_retire_folded_sources``' data-loss guard: the SNAPSHOT is removed ONLY when
    every reverse-rename succeeded. A source stuck as ``*.retired`` has its folded effect
    living ONLY in the SNAPSHOT, so removing it then would silently drop that event —
    instead the SNAPSHOT is retained as the documented SNAPSHOT_INCONSISTENT state that
    ``fsck --repair-snapshots`` owns (the caller discards the intent journal, so the
    recovery preamble never touches that retained state)."""
    rollback_clean = True
    for fp in fold_files:
        retired = fp + RETIRED_SUFFIX
        if os.path.exists(retired) and not os.path.exists(fp):
            try:
                os.rename(retired, fp)
            except OSError:
                rollback_clean = False
                logger.warning("compact: could not restore %s during rollback", fp, exc_info=True)
    if rollback_clean:
        try:
            os.remove(final_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("compact: could not remove uncommitted SNAPSHOT %s", final_path)
    else:
        logger.warning(
            "compact: rollback incomplete for %s — SNAPSHOT %s retained; run "
            "`rebar fsck --repair-snapshots`",
            ticket_id,
            final_path,
        )
    # unstage the aborted fold's own index entries
    _git(tracker, "reset", "-q", "--", f"{ticket_id}/")  # raw-git-ok: fold rollback


def _abort_fold(
    tracker: str, ticket_id: str, final_path: str, fold_files: list[str], journal: str | None
) -> None:
    """The fold's single crash-exit seam: whole-fold rollback, then journal discard. One
    function so a test can emulate SIGKILL (where NO cleanup code runs) by disabling
    exactly this and nothing else."""
    _rollback_fold(tracker, ticket_id, final_path, fold_files)
    compact_recovery.discard(journal)


def _apply_fold(
    tracker: str,
    ticket_id: str,
    ticket_dir: str,
    snapshot_event: dict,
    final_path: str,
    fold_files: list[str],
    no_commit: bool,
) -> int:
    """Execute the fold's worktree mutation + commit as ONE crash-guarded unit.

    The intent journal (commit-pending sentinel — see :mod:`compact_recovery`) is written
    BEFORE the first worktree mutation and discarded on every completed outcome, so a
    worker that dies anywhere inside this function leaves a journal the next recovery
    preamble converges. In-process failures do not even need the journal: any exception or
    a failed commit triggers :func:`_abort_fold`, restoring the exact pre-fold tree.

    ``no_commit`` (an explicit operator request for an uncommitted fold) skips the journal
    on purpose: journaling it would make the next recovery preamble revert state the
    operator asked for."""
    journal: str | None = None
    if not no_commit:
        try:
            journal = compact_recovery.write_intent(tracker, ticket_id, final_path, fold_files)
        except OSError:
            logger.warning("compact: cannot write the intent journal; refusing to fold")
            sys.stderr.write(
                "Error: cannot journal the compaction fold — refusing to mutate the "
                "store without its crash sentinel\n"
            )
            return 1
    try:
        fsutil.atomic_write(final_path, canonical_str(snapshot_event), encoding="utf-8")
        if _retire_folded_sources(fold_files, ticket_id, final_path) != 0:
            # Its per-step rollback already ran — a clean revert, or the intentionally
            # retained SNAPSHOT_INCONSISTENT state that fsck --repair-snapshots owns.
            # Either way the fold is OVER, so the sentinel must not outlive it (a live
            # journal would make the recovery preamble revert fsck's territory).
            compact_recovery.discard(journal)
            return 1
        try:
            os.remove(os.path.join(ticket_dir, ".cache.json"))
        except OSError:
            pass
        if not no_commit and _commit_compaction(tracker, ticket_id) != 0:
            _abort_fold(tracker, ticket_id, final_path, fold_files, journal)
            return 1
    except BaseException:
        _abort_fold(tracker, ticket_id, final_path, fold_files, journal)
        raise
    compact_recovery.discard(journal)
    return 0


# raw-git-ok: store-maintenance command, seam-internal
def _compact_locked(
    tracker: str,
    ticket_id: str,
    ticket_dir: str,
    threshold: int,
    no_commit: bool,
    horizon: int = 0,
    position_commits: dict[str, str] | None = None,
) -> int:
    """The locked compaction critical section. Returns 0 on success (prints
    EVENT_COUNT + the compacted line), 0 on below-threshold-inside-lock (prints the
    skip line), 1 on lock timeout / reducer / state / git failure."""
    try:
        handle = lock.acquire(tracker, timeout=30, attempts=2, dual_window=True)
    except lock.LockTimeout as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except compat.StoreIncompatibleError as exc:
        # Story 21dd: fail closed (non-zero) on an incompatible store before compaction.
        sys.stderr.write(str(exc) + "\n")
        return exc.returncode
    try:
        # Crash-recovery preamble (bug compulsory-pernickety-mantis): converge any
        # abandoned partial fold BEFORE reading the event list, so this fold plans
        # against the pre-crash truth rather than half-retired residue. Cheap when no
        # journal is pending (one listdir); we already hold the store write lock.
        compact_recovery.recover_abandoned_folds(tracker)
        parsed = _parse_candidate_events(ticket_dir)
        event_count = len(parsed)

        if event_count <= threshold:
            sys.stdout.write("below threshold (re-checked inside flock) — skipping compaction\n")
            return 0

        # RC2b Option 3 (conservative horizon): only FOLD events older than the
        # horizon. Younger "hot-edge" events stay live ``.json`` and — because the
        # SNAPSHOT is timestamped just after the newest folded event and before the
        # youngest live one — sort AFTER the snapshot and replay on top. So a
        # concurrent sub-horizon append that merges in later is NOT silently dropped by
        # the snapshot's positional skip. horizon<=0 folds everything (the pre-RC2b
        # behavior; the offline test suite defaults to 0).
        now = hlc.physical_now()

        old = [(fp, u, ts) for (fp, u, ts) in parsed if is_foldable(ts, now, horizon)]
        young = [(fp, u, ts) for (fp, u, ts) in parsed if not is_foldable(ts, now, horizon)]

        if not old:
            sys.stdout.write("all events within the compaction horizon — nothing to fold\n")
            return 0

        old = _defer_presnapshot_foldables(old, young)
        if not old:
            sys.stdout.write(
                "pre-snapshot foldables deferred until their governing SNAPSHOT folds\n"
            )
            return 0
        old = _exclude_snapshot_orphans(old)

        fold_files = [fp for (fp, _u, _ts) in old]

        # Pick a SNAPSHOT timestamp strictly between the newest folded event and the
        # youngest live one, so folded events sort before it (positionally skipped,
        # their state in compiled_state) and live events sort after it (replayed).
        if young:
            old_ts = [ts for (_fp, _u, ts) in old if ts is not None]
            young_ts = [ts for (_fp, _u, ts) in young if ts is not None]
            max_old = max(old_ts) if old_ts else now
            snapshot_ts = max_old + 1
            if young_ts and snapshot_ts >= min(young_ts):
                # No safe placement gap (adjacent straddling timestamps) — defer folding
                # this pass rather than risk a mis-sorted snapshot.
                sys.stdout.write("no safe horizon gap for a SNAPSHOT timestamp — deferring\n")
                return 0
            compiled_state = reduce_ticket(ticket_dir, event_files_override=fold_files)
        else:
            snapshot_ts = hlc.next_tick(tracker, ticket_id)
            compiled_state = reduce_ticket(ticket_dir)

        if compiled_state is None:
            sys.stderr.write(
                f"Error: reducer failed for ticket {ticket_id} (corrupt or ghost ticket)\n"
            )
            return 1
        # ``updated_at`` is a derived presentation field (P1.1), re-computed on
        # every replay. It must NOT enter the SNAPSHOT's compiled_state, or it
        # would (a) ride into event-log bytes and (b) be restored stale by
        # process_snapshot. Copy-and-drop it so the cache object is untouched.
        # The legacy ``signature`` mirror is dropped here too (task 7ed9 never-emit) —
        # new snapshots carry only ``attestations``.
        _strip = _snapshot_strip_keys()
        compiled_state = {k: v for k, v in compiled_state.items() if k not in _strip}
        # Authorship ledger (epic gnu-whale-ichor / 3183): independently scan the folded
        # events and preserve each SIGNED one so verify-authorship can re-verify it after
        # the raw files are retired. Derive repo_root from the tracker (no repo_root here).
        compiled_state["authorship_ledger"] = _build_authorship_ledger(
            fold_files,
            os.path.dirname(os.path.realpath(tracker)),
            position_commits=position_commits,
        )
        status = compiled_state.get("status", "")
        if status in ("error", "fsck_needed"):
            sys.stderr.write(f"Error: ticket {ticket_id} has status '{status}' — cannot compact\n")
            return 1

        # A folded prior SNAPSHOT is NOT a source event (bug aea0). Its entire content IS
        # its compiled_state, which this snapshot absorbs, so nothing is lost when the file
        # goes away — but citing it makes fsck's snapshot_missing_sources check report a
        # perfectly healthy ticket as damaged (and, post b636, as un-rebuildable) the
        # moment it does. `parsed` admits SNAPSHOT because it is a KNOWN_EVENT_TYPE, which
        # is how it ended up in this list. The REBUILD path in compact_rebuild already
        # skips snapshots when building its source list; this makes the two agree.
        source_uuids = [
            u
            for (fp, u, _ts) in old
            if not os.path.basename(fp).removesuffix(RETIRED_SUFFIX).endswith("-SNAPSHOT.json")
        ]

        snapshot_event, final_path = _build_snapshot_event(
            tracker, ticket_dir, compiled_state, source_uuids, snapshot_ts
        )
        if (
            _apply_fold(
                tracker, ticket_id, ticket_dir, snapshot_event, final_path, fold_files, no_commit
            )
            != 0
        ):
            return 1

        sys.stdout.write(f"EVENT_COUNT={event_count}\n")
        sys.stdout.write(f"compacted events into SNAPSHOT for {ticket_id}\n")
        return 0
    finally:
        handle.release()
