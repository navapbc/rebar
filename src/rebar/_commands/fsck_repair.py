"""``fsck --repair`` — the live-store remediation cluster (Tier E E4, A3 34b1).

Extracted from ``fsck.py`` (the diagnostic scanner) as a one-way leaf: it imports
nothing from ``fsck``. The two shared filesystem helpers ``_ticket_dirs`` and
``_resolve_tracker_git_dir`` live HERE and are re-imported BY ``fsck``, so the
dependency runs one way only (diagnostic → repair), never back.

The ``--repair`` path drives the store to fsck-zero, safely and resumably: retire
still-present folded sources (SNAPSHOT_INCONSISTENT), rebuild snapshots that dropped
an AUTO-RECOVER orphan, and surface order-sensitive orphans for human triage — all
under the store write lock, pre-tagged for rollback, batched + committed + pushed,
and aborted if a reconciler pass is (or may be) in flight.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from rebar._store import compat, lock
from rebar._store.gitutil import run_git
from rebar.reducer import KNOWN_EVENT_TYPES
from rebar.reducer._cache import RETIRED_SUFFIX, is_active_event

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


def _git(tracker: str, *args: str) -> subprocess.CompletedProcess:
    return run_git(tracker, *args, check=False)


def _resolve_tracker_git_dir(tracker: str) -> str:
    tracker_git = os.path.join(tracker, ".git")
    if os.path.isfile(tracker_git):
        with open(tracker_git, encoding="utf-8") as f:
            gitdir = f.read().strip()
        gitdir = gitdir[len("gitdir: ") :] if gitdir.startswith("gitdir: ") else gitdir
        if not gitdir.startswith("/"):
            gitdir = os.path.join(tracker, gitdir)
        return gitdir
    if os.path.isdir(tracker_git):
        return tracker_git
    return ""


def _ticket_dirs(tracker: str) -> list[str]:
    # Skip hidden dirs (.git, .bridge_state, …): the bash `"$TRACKER_DIR"/*/` glob
    # never matches dot-dirs, and ticket ids never start with '.'.
    return sorted(
        d
        for d in os.listdir(tracker)
        if not d.startswith(".") and os.path.isdir(os.path.join(tracker, d))
    )


def _active_snapshots(ticket_dir: str) -> list[str]:
    return sorted(
        n for n in os.listdir(ticket_dir) if n.endswith("-SNAPSHOT.json") and not n.startswith(".")
    )


def _repair_plan(ticket_dir: str, ticket_id: str) -> dict:
    """Derive a per-ticket repair plan mirroring _check_snapshot's detection.

    Returns {"retire": [filenames], "auto_orphans": [(name,type)],
    "triage_orphans": [(name,type)]}. ``retire`` are still-present folded sources
    (SNAPSHOT_INCONSISTENT → rename to .retired, NOT a rebuild); ``auto_orphans`` are
    AUTO-RECOVER pre-snapshot orphans (→ full-log rebuild); ``triage_orphans`` are
    order-sensitive orphans surfaced for a human.
    """
    snaps = _active_snapshots(ticket_dir)
    plan: dict[str, list] = {"retire": [], "auto_orphans": [], "triage_orphans": []}
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


def _repair_ticket(tracker: str, ticket_id: str, ticket_dir: str, *, dry_run: bool) -> dict:
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
        "skipped": skipped,
    }
    if dry_run:
        disp["rebuilt"] = bool(plan["auto_orphans"])
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

    if plan["auto_orphans"]:
        from rebar._commands.compact import rebuild_snapshot_from_full_log

        disp["rebuilt"] = rebuild_snapshot_from_full_log(tracker, ticket_id, ticket_dir)
    return disp


def _has_remote(tracker: str) -> bool:
    return bool(_git(tracker, "remote").stdout.strip())


def _reconciler_pause(repo_root=None) -> bool:
    """Best-effort: disable the reconcile-bridge GHA schedule for the repair window (the
    leased CAS ``refs/reconciler/lock`` expires, so it is not the pause mechanism). Returns
    True iff we disabled it (→ re-enable in a failsafe); a missing/unauthenticated ``gh``
    returns False (the batched, pre-tagged, committed design keeps a stray write recoverable)."""
    cp = subprocess.run(
        ["gh", "workflow", "disable", "reconcile-bridge.yml"], capture_output=True, text=True
    )
    return cp.returncode == 0


def _reconciler_resume() -> None:
    subprocess.run(
        ["gh", "workflow", "enable", "reconcile-bridge.yml"], capture_output=True, text=True
    )


def _reconciler_in_flight(repo_root=None) -> bool:
    """Return True if a reconciler pass is (or may be) mid-flight — the in-flight guard the
    destructive live repair runs AFTER disabling the schedule (which stops the NEXT pass, not
    one already running). The pass holds the leased CAS ``refs/reconciler/lock``, so
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


def _repair_run(
    tracker: str, *, dry_run: bool, limit: int | None = None, repo_root=None
) -> tuple[list[str], int]:
    """A3 remediation: drive the store to fsck-zero, safely and resumably.

    fsck itself is the authoritative resumability check (only tickets it still flags are
    repaired); a ``.git/a3-repaired/<id>`` marker is a local, never-committed optimization.
    The live run pre-tags for rollback, pauses the reconciler, and commits+pushes each
    batch — a push failure ABORTS and surfaces the error. Dry-run writes nothing.
    Returns (report_lines, unresolved_fault_count).
    """
    lines: list[str] = []
    flagged: list[tuple[str, dict]] = []
    for tid in _ticket_dirs(tracker):
        plan = _repair_plan(os.path.join(tracker, tid), tid)
        if plan["retire"] or plan["auto_orphans"] or plan["triage_orphans"]:
            flagged.append((tid, plan))
    total = len(flagged)
    if limit is not None:
        flagged = flagged[:limit]
    if not flagged:
        lines.append("a3-remediation: no repairable faults")
        return lines, 0

    if dry_run:
        for tid, plan in flagged:
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

    paused = _reconciler_pause(repo_root)
    lines.append(f"a3-remediation: reconciler {'paused' if paused else 'pause skipped'}")
    batch = 200
    try:
        # In-flight guard: disabling the schedule stops the NEXT pass, not one already
        # running. Abort BEFORE any write if a pass holds refs/reconciler/lock (or its
        # state is indeterminate) — the finally re-enables the schedule we just disabled.
        if _reconciler_in_flight(repo_root):
            lines.append(
                "ABORT: a reconciler pass is in flight (refs/reconciler/lock held or "
                "unreadable) — refusing to repair; retry once the pass completes"
            )
            return lines, -1
        for i, (tid, _plan) in enumerate(flagged):
            disp = _repair_ticket(tracker, tid, os.path.join(tracker, tid), dry_run=False)
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
                    commit = _git(
                        tracker, "commit", "--no-verify", "-m", f"a3-remediation: batch {n}"
                    )
                    if commit.returncode != 0:
                        lines.append("ABORT: commit failed while holding batch")
                        return lines, -1
                    if _has_remote(tracker):
                        push = _git(tracker, "push", "origin", "HEAD:tickets")
                        if push.returncode != 0:
                            lines.append(f"ABORT: push failed for batch {n}: {push.stderr.strip()}")
                            return lines, -1
    finally:
        if paused:
            _reconciler_resume()
            lines.append("a3-remediation: reconciler re-enabled")

    remaining = sum(
        1
        for tid in _ticket_dirs(tracker)
        if (p := _repair_plan(os.path.join(tracker, tid), tid))["retire"] or p["auto_orphans"]
    )
    triage = sum(
        len(_repair_plan(os.path.join(tracker, tid), tid)["triage_orphans"])
        for tid in _ticket_dirs(tracker)
    )
    lines.append(
        f"a3-remediation: {len(flagged)} ticket(s) processed; {remaining} auto-fault(s) remain, "
        f"{triage} orphan(s) await human triage"
    )
    return lines, remaining
