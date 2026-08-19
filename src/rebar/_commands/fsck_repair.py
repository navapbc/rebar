"""``fsck --repair`` — the live-store remediation cluster (Tier E E4, A3 34b1).

Extracted from ``fsck.py`` (the diagnostic scanner) as a one-way leaf: it imports
nothing from ``fsck``. The shared filesystem helpers ``_ticket_dirs``,
``_dir_is_archived`` and ``_resolve_tracker_git_dir`` now live in
:mod:`rebar._store.gitutil` (ticket b432-c9dc-c1b4-4a45) — they were never about repair,
and keeping them here forced the store layer to defer-import a command module. They are
re-imported here at module level, NOT for convenience: several tests bind them as
``fsck_repair`` module ATTRIBUTES (``monkeypatch.setattr(fsck_repair, "_ticket_dirs", …)``),
and this module's own call sites resolve them through the module global, so the re-import is
what keeps those patches effective. Do not convert it to a qualified ``gitutil._ticket_dirs``
call.

The ``--repair`` path drives the store to fsck-zero, safely and resumably: retire
still-present folded sources (SNAPSHOT_INCONSISTENT), rebuild snapshots that dropped
an AUTO-RECOVER orphan, and surface order-sensitive orphans for human triage — all
under the store write lock, pre-tagged for rollback, batched + committed + pushed,
and aborted if a reconciler pass is (or may be) in flight.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from rebar._commands._repair_pause import RepairPauseError, owned_repair_pause
from rebar._store import compat, lock
from rebar._store.gitutil import (  # noqa: F401  (compat re-export — see the module docstring)
    _dir_is_archived,
    _resolve_tracker_git_dir,
    _ticket_dirs,
    run_git,
)
from rebar.reducer import KNOWN_EVENT_TYPES
from rebar.reducer._cache import RETIRED_SUFFIX, is_active_event

logger = logging.getLogger(__name__)

# ── A3 (34b1) live-store remediation: orphan disposition ─────────────────────
# Routed BY EVENT TYPE. Additive/commutative orphans are safe to AUTO-RECOVER via a
# full-log rebuild (the fold order does not change their effect). Order-sensitive
# orphans are surfaced for HUMAN-TRIAGE — an auto-rebuild could pick a wrong order.
# CREATE (genesis) and SNAPSHOT (the fold marker) are never orphan-classified; the
# two sets below cover every other KNOWN_EVENT_TYPE (asserted in tests).
_AUTO_RECOVER_ORPHAN_TYPES = frozenset(
    {"COMMENT", "LINK", "UNLINK", "TAG_DELTA", "COMMITS", "BRIDGE_ALERT", "REVERT"}
)
_HUMAN_TRIAGE_ORPHAN_TYPES = frozenset(
    {
        "STATUS",
        "EDIT",
        "FILE_IMPACT",
        "VERIFY_COMMANDS",
        "SIGNATURE",
        "WORKFLOW_RUN",
        "WORKFLOW_STEP",
        "ARCHIVED",
        # Identity key lifecycle (epic gnu-whale-ichor / e165): a KEY_ADD/KEY_REVOKE lands
        # on an identity, not the ticket graph, so it is never a graph orphan — but it is
        # epoch-order-sensitive (a blind rebuild could reorder add/revoke), so human-triage.
        "KEY_ADD",
        "KEY_REVOKE",
    }
)


def is_snapshot_orphan(
    name: str,
    etype: str,
    event_uuid: str,
    snapshot_filename: str,
    source_uuids: set[str],
) -> bool:
    """fsck's ORPHAN_EVENT predicate, anchored to one snapshot — THE shared definition.

    True when an ACTIVE event file is pre-snapshot loss: a KNOWN-type, non-SNAPSHOT
    event whose filename sorts before ``snapshot_filename`` and whose uuid is absent
    from that snapshot's ``source_event_uuids``. Non-KNOWN types are *correctly*
    uncited (compaction never folds them), and snapshots are never orphan-classified.

    Shared by fsck's scan (``_check_snapshot``) AND the compaction fold's exclusion
    guard (``compact_txn``), so the two can never disagree about what an orphan is —
    a fold that retired + cited an event fsck called ORPHAN_EVENT would launder the
    loss into an undetectable, unrepairable state (bug f96b-3498-8f04-40b0).
    """
    return (
        etype in KNOWN_EVENT_TYPES
        and "-SNAPSHOT.json" not in name
        and name < snapshot_filename
        and event_uuid not in source_uuids
    )


# raw-git-ok: store-maintenance command, seam-internal
def _git(tracker: str, *args: str) -> subprocess.CompletedProcess:
    return run_git(tracker, *args, check=False)


# The a3-remediation push is an INCREMENTAL push of a batch of ticket events against an
# already-warm clone, so bound it with the _store incremental precedent
# (_store/push.py._GIT_TIMEOUT = 30), NOT the 300s cold-materialize one. A timeout
# surfaces as a synthetic failed CompletedProcess(124) naming the op + bound, which the
# caller's existing ABORT path reports (never a bare TimeoutExpired, never a hang) —
# mirroring _store/push.py._git (bug 983f).
_PUSH_TIMEOUT = 30


# raw-git-ok: store-maintenance command, seam-internal
def _git_push(tracker: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return run_git(tracker, *args, check=False, timeout=_PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", *args],
            124,
            "",
            f"git {' '.join(args)} timed out after {_PUSH_TIMEOUT}s",
        )


def _active_snapshots(ticket_dir: str) -> list[str]:
    return sorted(
        n for n in os.listdir(ticket_dir) if n.endswith("-SNAPSHOT.json") and not n.startswith(".")
    )


def _has_retired_create(ticket_dir: str) -> bool:
    """Return whether a folded CREATE remains available for a full-log rebuild."""
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return False
    return any(n.endswith("-CREATE.json" + RETIRED_SUFFIX) for n in names)


def _is_stale_channel_snapshot(ticket_dir: str, snapshot_filename: str) -> bool:
    """Return whether a SNAPSHOT predates persisted ``creation_channel`` support."""
    try:
        with open(os.path.join(ticket_dir, snapshot_filename), encoding="utf-8") as f:
            snapshot = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    compiled = snapshot.get("data", {}).get("compiled_state")
    return (
        isinstance(compiled, dict)
        and "creation_channel" not in compiled
        and _has_retired_create(ticket_dir)
    )


def _repair_plan(ticket_dir: str, _ticket_id: str) -> dict:
    """Derive a per-ticket repair plan mirroring _check_snapshot's detection.

    Returns {"retire": [filenames], "auto_orphans": [(name,type)],
    "triage_orphans": [(name,type)], "stale_channel": [filenames]}. ``retire`` are
    still-present folded sources (SNAPSHOT_INCONSISTENT → rename to .retired, NOT a
    rebuild); ``auto_orphans`` are AUTO-RECOVER pre-snapshot orphans (→ full-log
    rebuild); ``triage_orphans`` are order-sensitive orphans surfaced for a human;
    ``stale_channel`` snapshots are selected only by the narrow repair mode.
    """
    snaps = _active_snapshots(ticket_dir)
    plan: dict[str, list] = {
        "retire": [],
        "auto_orphans": [],
        "triage_orphans": [],
        "stale_channel": [],
    }
    if not snaps:
        return plan
    latest_snap = snaps[-1]

    # uuid -> (filename, event_type) for active events. Older SNAPSHOT files ARE
    # included (only the horizon `latest_snap` is excluded): a re-compaction folds a
    # prior snapshot INTO the newer one's source_event_uuids, so a still-present older
    # snapshot is a SNAPSHOT_INCONSISTENT source that must be retired too — mirroring
    # _check_snapshot, which excludes only the snapshot it is checking.
    event_files: dict[str, tuple[str, str]] = {}
    for name in sorted(os.listdir(ticket_dir)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        if not is_active_event(name) or name == latest_snap:
            continue
        parts = name.split("-", 1)
        if len(parts) < 2:
            continue
        type_split = parts[1].rsplit(".json", 1)[0].rsplit("-", 1)
        if len(type_split) < 2:
            continue
        event_files[type_split[0]] = (name, type_split[1])

    all_sources: set[str] = set()
    for snap in snaps:
        try:
            with open(os.path.join(ticket_dir, snap), encoding="utf-8") as f:
                sources = json.load(f).get("data", {}).get("source_event_uuids", [])
        except (json.JSONDecodeError, OSError):
            continue
        if _is_stale_channel_snapshot(ticket_dir, snap):
            plan["stale_channel"].append(snap)
        all_sources.update(sources)
        # SNAPSHOT_INCONSISTENT: a folded source still present as an active file.
        for u in sources:
            if u in event_files and event_files[u][0] not in plan["retire"]:
                plan["retire"].append(event_files[u][0])

    for file_uuid, (name, etype) in event_files.items():
        if etype not in KNOWN_EVENT_TYPES or name in plan["retire"]:
            continue
        if name.endswith("-SNAPSHOT.json"):
            continue  # snapshots are never orphan-classified (symmetry with _check_snapshot)
        if name < latest_snap and file_uuid not in all_sources:
            if etype in _AUTO_RECOVER_ORPHAN_TYPES:
                plan["auto_orphans"].append((name, etype))
            else:  # HUMAN-TRIAGE (order-sensitive) — surfaced, never auto-rebuilt.
                plan["triage_orphans"].append((name, etype))
    return plan


def _repair_ticket(
    tracker: str,
    ticket_id: str,
    ticket_dir: str,
    *,
    dry_run: bool,
    repair_stale_channel: bool = False,
    no_commit: bool = False,
) -> dict:
    """Apply (or, in dry-run, describe) a ticket's _repair_plan. Retires still-present
    folded sources under the write lock, then rebuilds if any AUTO-RECOVER orphan
    remains. HUMAN-TRIAGE orphans and MISSING_CREATE are surfaced, never auto-written.
    Returns the executed disposition."""
    plan = _repair_plan(ticket_dir, ticket_id)
    skipped: list[str] = []
    disp: dict = {
        "ticket": ticket_id,
        "retired": list(plan["retire"]),
        "rebuilt": False,
        "triage": [f"{n} ({t})" for n, t in plan["triage_orphans"]],
        "stale_channel": list(plan["stale_channel"]),
        "skipped": skipped,
        "restored": [],
    }
    if dry_run:
        disp["rebuilt"] = bool(plan["auto_orphans"]) or (
            repair_stale_channel and bool(plan["stale_channel"])
        )
        return disp

    if plan["retire"]:
        try:
            handle = lock.acquire(tracker, timeout=30, attempts=2, dual_window=True)
        except lock.LockTimeout:
            disp["error"] = "lock-timeout"
            return disp
        except compat.StoreIncompatibleError as exc:
            # Story 21dd: fail closed on an incompatible store — repair is a mutation.
            disp["error"] = f"store-incompatible: {exc}"
            return disp
        try:
            for name in plan["retire"]:
                fp = os.path.join(ticket_dir, name)
                retired = fp + RETIRED_SUFFIX
                if os.path.exists(retired):
                    # The source was already folded to *.retired (b306) and has been
                    # RESURRECTED as a live .json by a delete/add reconciliation (RC1) —
                    # the .json is a byte-identical duplicate of the preserved .retired,
                    # so dropping it resolves SNAPSHOT_INCONSISTENT with no data loss.
                    try:
                        os.remove(fp)
                    except OSError:
                        skipped.append(name)
                    continue
                try:
                    os.rename(fp, retired)
                except OSError:
                    skipped.append(name)
        finally:
            handle.release()

    if plan["auto_orphans"] or (repair_stale_channel and plan["stale_channel"]):
        # Task 08c8: go through the COMPOSED helper, not the bare rebuild — otherwise a
        # ticket whose source a legacy (delete-style) compaction dropped is never
        # recovered on this path and the b636 guard just refuses the rebuild. Imported
        # locally because ``fsck_restore`` is imported at the BOTTOM of this module (it
        # lazily imports ``snapshot_missing_sources`` back from here).
        from rebar._commands.fsck_restore import rebuild_with_restore as _rebuild_with_restore

        rebuilt, restored = _rebuild_with_restore(
            tracker, ticket_id, ticket_dir, no_commit=no_commit
        )
        disp["rebuilt"] = rebuilt
        disp["restored"] = restored
    return disp


def _has_remote(tracker: str) -> bool:
    return bool(_git(tracker, "remote").stdout.strip())


def _reconciler_in_flight(repo_root=None) -> bool:
    """Return True if a reconciler pass is (or may be) mid-flight — the in-flight guard the
    destructive live repair runs AFTER acquiring its durable pause (which stops the NEXT pass,
    not one already running). The pass holds the leased CAS ``refs/reconciler/lock``, so
    ``check_pass_lock`` is the probe. Fail-CLOSED: an unreadable lock (``ReconcileLockError``)
    or an un-importable advisory module reports in-flight=True so an indeterminate state aborts
    the repair rather than writing under a possibly-live reconciler; a never-reconciled repo
    (ref absent) reads free → False → repair proceeds."""
    root = Path(repo_root) if repo_root is not None else Path(".")
    try:
        from rebar._engine import engine_dir

        eng = str(engine_dir())
        if eng not in sys.path:
            sys.path.insert(0, eng)  # so the top-level rebar_reconciler package resolves
        from rebar_reconciler import _advisory_lock as advisory
    except Exception:  # noqa: BLE001 — any import/asset failure → can't prove it's free → fail-closed
        return True
    try:
        return advisory.check_pass_lock(root)
    except advisory.ReconcileLockError:
        return True  # indeterminate lock state → fail-closed (do not repair)


# raw-git-ok: store-maintenance command, seam-internal
def _repair_run(
    tracker: str,
    *,
    dry_run: bool,
    limit: int | None = None,
    repo_root=None,
    only: str | None = None,
    include_archived: bool = False,
    _pause_owned: bool = False,
) -> tuple[list[str], int]:
    """A3 remediation: drive the store to fsck-zero, safely and resumably.

    fsck itself is the authoritative resumability check (only tickets it still flags are
    repaired); a ``.git/a3-repaired/<id>`` marker is a local, never-committed optimization.
    The caller-owned durable pause covers the live run, which pre-tags for rollback and
    commits+pushes each batch — a push failure ABORTS and surfaces the error. Dry-run
    writes nothing.
    Returns (report_lines, unresolved_fault_count).
    """
    lines: list[str] = []
    flagged: list[tuple[str, dict]] = []
    for tid in _ticket_dirs(tracker, include_archived=include_archived):
        plan = _repair_plan(os.path.join(tracker, tid), tid)
        if only == "stale-channel":
            if plan["stale_channel"]:
                flagged.append((tid, plan))
        elif plan["retire"] or plan["auto_orphans"] or plan["triage_orphans"]:
            flagged.append((tid, plan))
    if only == "stale-channel":
        mixed = False
        for tid, plan in flagged:
            kinds: list[str] = []
            if plan["retire"]:
                kinds.append("SNAPSHOT_INCONSISTENT")
            if plan["auto_orphans"] or plan["triage_orphans"]:
                kinds.append("ORPHAN_EVENT")
            if kinds:
                lines.append(f"REFUSE {tid}: {', '.join(kinds)}")
                mixed = True
        if mixed:
            lines.append("ABORT: stale-channel repair refuses mixed faults before mutation")
            return lines, -1
    total = len(flagged)
    if limit is not None:
        flagged = flagged[:limit]
    if not flagged:
        lines.append("a3-remediation: no repairable faults")
        return lines, 0

    if dry_run:
        for tid, plan in flagged:
            if only == "stale-channel":
                lines.append(f"DRY-RUN {tid}: stale_channel={len(plan['stale_channel'])}")
            else:
                lines.append(
                    f"DRY-RUN {tid}: retire={len(plan['retire'])} "
                    f"rebuild={len(plan['auto_orphans'])} triage={len(plan['triage_orphans'])}"
                )
        triage = sum(len(p["triage_orphans"]) for _, p in flagged)
        lines.append(
            f"a3-remediation DRY-RUN: {len(flagged)}/{total} ticket(s) would be repaired "
            "— 0 file writes, 0 commits"
        )
        return lines, triage

    if not _pause_owned:
        try:
            with owned_repair_pause("fsck", repo_root, in_flight_probe=_reconciler_in_flight):
                return _repair_run(
                    tracker,
                    dry_run=False,
                    limit=limit,
                    repo_root=repo_root,
                    only=only,
                    _pause_owned=True,
                )
        except RepairPauseError as exc:
            return [exc.legacy_report_line or exc.message], -1

    # ── LIVE run ──
    pre_oid = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    _git(tracker, "tag", "-f", "pre-a3-remediation", pre_oid)
    lines.append(f"a3-remediation: pre-tag pre-a3-remediation @ {pre_oid[:12]}")

    # Markers live under the resolved git dir (never the committed tree, so `git add`
    # never picks them up) — .git may be a worktree pointer FILE, not a directory.
    git_dir = _resolve_tracker_git_dir(tracker)
    marker_dir = os.path.join(git_dir or tracker, "a3-repaired")
    try:
        os.makedirs(marker_dir, exist_ok=True)
    except OSError:
        marker_dir = ""

    lines.append("a3-remediation: reconciler paused")
    batch = 200
    for i, (tid, _plan) in enumerate(flagged):
        disp = _repair_ticket(
            tracker,
            tid,
            os.path.join(tracker, tid),
            dry_run=False,
            repair_stale_channel=only == "stale-channel",
            no_commit=True,
        )
        if disp.get("error"):
            lines.append(f"SKIP {tid}: {disp['error']}")  # per-ticket failure: log + skip
        elif marker_dir:
            try:
                open(os.path.join(marker_dir, tid), "w").close()
            except OSError:
                pass
        if (i + 1) % batch == 0 or i == len(flagged) - 1:
            add = _git(tracker, "add", "-A")
            if add.returncode != 0:
                lines.append("ABORT: git add failed")
                return lines, -1
            if _git(tracker, "diff", "--cached", "--quiet").returncode != 0:
                n = i // batch + 1
                commit = _git(tracker, "commit", "--no-verify", "-m", f"a3-remediation: batch {n}")
                if commit.returncode != 0:
                    lines.append("ABORT: commit failed while holding batch")
                    return lines, -1
                if _has_remote(tracker):
                    push = _git_push(tracker, "push", "origin", "HEAD:tickets")
                    if push.returncode != 0:
                        lines.append(f"ABORT: push failed for batch {n}: {push.stderr.strip()}")
                        return lines, -1
    lines.append("a3-remediation: reconciler re-enabled")

    if only == "stale-channel":
        remaining = sum(
            1
            for tid in _ticket_dirs(tracker, include_archived=include_archived)
            if _repair_plan(os.path.join(tracker, tid), tid)["stale_channel"]
        )
        lines.append(
            f"a3-remediation: {len(flagged)} ticket(s) processed; "
            f"{remaining} stale-channel fault(s) remain"
        )
        return lines, remaining

    remaining = sum(
        1
        for tid in _ticket_dirs(tracker, include_archived=include_archived)
        if (p := _repair_plan(os.path.join(tracker, tid), tid))["retire"] or p["auto_orphans"]
    )
    triage = sum(
        len(_repair_plan(os.path.join(tracker, tid), tid)["triage_orphans"])
        for tid in _ticket_dirs(tracker, include_archived=include_archived)
    )
    lines.append(
        f"a3-remediation: {len(flagged)} ticket(s) processed; {remaining} auto-fault(s) remain, "
        f"{triage} orphan(s) await human triage"
    )
    return lines, remaining


def _abbrev(uuids: list[str], limit: int = 3) -> str:
    """``a, b, c...`` — the first ``limit`` uuids, ellipsised when there are more."""
    head = ", ".join(uuids[:limit])
    return f"{head}..." if len(uuids) > limit else head


def snapshot_missing_sources(ticket_dir: str) -> list[str]:
    """UUIDs cited by the newest active SNAPSHOT that have NO event file on disk (bug b636).

    Compaction before the I1 non-destructive rename (story tricolour-head-ratfish) DELETED
    its folded sources, so for those tickets the cited state survives only inside the
    SNAPSHOT's ``compiled_state``. A non-empty return is therefore PROOF that the raw log is
    incomplete: a from-zero replay (even ``include_retired=True``) would reconstruct a partial
    history and whatever status happened to survive would win.

    Best-effort — an unreadable or absent snapshot yields ``[]`` (nothing proven missing).
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
    present = {u for u in cited if any(u in name for name in names)}
    return [u for u in cited if u not in present]


def missing_sources_finding(
    ticket_dir: str, ticket_id: str, snapshot_filename: str, source_uuids: list
) -> list[str]:
    """The ``SNAPSHOT_MISSING_SOURCES`` finding, so the latent population is visible BEFORE a
    repair runs. Such a ticket is NOT safely rebuildable: a from-zero replay would drop the
    state those deleted events carried."""
    try:
        names = sorted(os.listdir(ticket_dir))
    except OSError:
        names = []
    absent = [u for u in source_uuids if not any(u in n for n in names)]
    if not absent:
        return []
    return [
        f"SNAPSHOT_MISSING_SOURCES: {ticket_id}/{snapshot_filename} — "
        f"{len(absent)} cited source event(s) absent from disk ({_abbrev(absent)}); "
        "raw log incomplete — NOT auto-rebuildable, human triage required"
    ]


def rebuild_source_state(ticket_id: str, ticket_dir: str):
    """The state a rebuild would persist, or ``None`` when it must NOT proceed (bug b636).

    FAIL CLOSED first: a from-zero replay is sound only when the raw log is COMPLETE, so a
    prior SNAPSHOT citing sources absent from disk aborts the rebuild rather than
    reconstructing a partial history. Ticket 34b1 already classifies STATUS and the other
    order-sensitive kinds as HUMAN-TRIAGE, never auto-rebuilt — but that classification gates
    only WHICH ORPHAN TYPE TRIGGERS a rebuild, and a rebuild is whole-ticket. This moves the
    guard from the trigger to the blast radius.

    Then the usual reduce: full raw-history state (active + retired, snapshots stripped),
    INCLUDING the merged-in orphan the stale snapshot's positional skip had dropped.
    """
    from rebar.reducer import reduce_ticket

    missing = snapshot_missing_sources(ticket_dir)
    if missing:
        logger.warning(
            "fsck: REFUSING snapshot rebuild for %s — prior SNAPSHOT cites %d source event(s) "
            "absent from disk (%s); the raw log is incomplete and a rebuild would drop state "
            "held only in the snapshot. Human triage required.",
            ticket_id,
            len(missing),
            _abbrev(missing),
        )
        return None
    state = reduce_ticket(ticket_dir, include_retired=True)
    if state is None or state.get("status") in ("error", "fsck_needed"):
        return None
    return state


def repair_or_plan(
    tracker: str,
    ticket_id: str,
    ticket_dir: str,
    findings: list[str],
    rescan,
    *,
    no_mutate: bool,
    dry_run: bool,
) -> tuple[list[str], list[str]]:
    """RC2b Option 1: rebuild a stale snapshot that dropped a merged-in orphan, then re-check
    (folds the orphan back in). Returns ``(lines_to_emit, findings_after)``.

    Under ``dry_run`` it only PLANS (bug b636): ``--repair-snapshots`` previously ignored
    ``--dry-run`` entirely and MUTATED the store, so the broad legacy rebuild could not be
    previewed at all.
    """
    rebuildable = any(
        "SNAPSHOT_INCONSISTENT" in f
        or "ORPHAN_EVENT" in f
        or "SNAPSHOT_STALE_CHANNEL" in f
        or "SNAPSHOT_MISSING_SOURCES" in f
        for f in findings
    )
    if not rebuildable:
        return [], findings
    if dry_run:
        return _dry_run_plan(tracker, ticket_id, ticket_dir), findings
    if no_mutate:
        return [], findings

    # Routing parity with `fsck --repair` (bug f96b): an order-sensitive orphan is
    # HUMAN-TRIAGE, never auto-rebuilt — the rebuild is whole-ticket, so it would
    # silently absorb the orphan in log order. Its presence blocks this ticket's
    # rebuild; findings stay (the damage is real and still unrepaired).
    triage = _repair_plan(ticket_dir, ticket_id)["triage_orphans"]
    if triage:
        return [
            f"TRIAGE: {ticket_id}/{name} ({etype}) — order-sensitive orphan; "
            "not auto-rebuilt (routing parity with fsck --repair)"
            for name, etype in triage
        ], findings

    rebuilt, restored = rebuild_with_restore(tracker, ticket_id, ticket_dir)
    if not rebuilt:
        return [], findings
    post = rescan()
    resolved = len(findings) - len(post)
    if resolved <= 0:
        return [], post
    detail = f" after restoring {len(restored)} deleted event(s)" if restored else ""
    return [
        f"FIXED: rebuilt SNAPSHOT for {ticket_id}{detail} ({resolved} finding(s) resolved)"
    ], post


def _dry_run_plan(tracker: str, ticket_id: str, ticket_dir: str) -> list[str]:
    """The per-ticket line a dry run prints instead of writing anything (bug b636)."""
    absent = snapshot_missing_sources(ticket_dir)
    if not absent:
        return [f"DRY-RUN: would rebuild SNAPSHOT for {ticket_id}"]
    candidates = restore_deleted_sources(tracker, ticket_id, ticket_dir, dry_run=True)
    if len(candidates) >= len(absent):
        return [
            f"DRY-RUN: would restore {len(candidates)} deleted event(s) from history for "
            f"{ticket_id}, then rebuild"
        ]
    return [
        f"DRY-RUN: would SKIP {ticket_id} — {len(absent)} cited source event(s) absent from "
        f"disk, only {len(candidates)} recoverable from history (not safely rebuildable)"
    ]


# Restore leaf (module-size seam): recovering deleted event bytes from tickets history lives
# in ``fsck_restore``. Re-exported so ``fsck_repair.<name>`` keeps resolving for compact/fsck
# and the tests. Imported at the BOTTOM so the leaf can lazily import this module's
# ``snapshot_missing_sources`` without an import cycle.
from rebar._commands.fsck_restore import (  # noqa: E402
    rebuild_with_restore,
    restore_deleted_sources,
)
