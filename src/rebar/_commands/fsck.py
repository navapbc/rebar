"""In-process ``fsck`` — non-destructive store integrity validator (Tier E E4).

Ports ticket-fsck.sh. Runs five checks over the tracker:
  1. JSON validity of event files
  2. CREATE event presence (via the reducer)
  3. Stale ``.git/index.lock`` cleanup (>5min; the ONLY mutation, suppressed by
     the ``no_mutate=True`` argument for read-only surfaces)
  4. SNAPSHOT ``source_event_uuids`` consistency (4a still-on-disk, 4b orphans)
  4.5 Push-pending notice (local ahead of origin/tickets; informational)

Text mode emits tagged lines + a summary; ``--output json`` derives
``{issues:[{kind,ticket_id?,filename?,detail}], fixed[], issue_count}`` from the
SAME text via the dispatcher's regex transform (kept identical for byte-parity).
Exit 0 = no issues, 1 = issues found. Byte-parity pinned by
``tests/interfaces/test_e4_fsck.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rebar import config
from rebar._engine_support.output import OutputFormatError, parse_output
from rebar._store import lock
from rebar._store.gitutil import run_git
from rebar.reducer import KNOWN_EVENT_TYPES, reduce_ticket
from rebar.reducer._cache import RETIRED_SUFFIX, is_active_event

_STRUCTURED_KINDS = {
    "corrupt",
    "corrupt_create",
    "missing_create",
    "snapshot_inconsistent",
    "orphan_event",
    "status_fork_resolved",
}

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


def _scan(
    tracker: str, no_mutate: bool, repo_root=None, *, repair_snapshots: bool = False
) -> tuple[list[str], int]:
    lines: list[str] = []
    issue_count = 0

    # ── Check 1: JSON validity ───────────────────────────────────────────────
    for ticket_id in _ticket_dirs(tracker):
        ticket_dir = os.path.join(tracker, ticket_id)
        for filename in sorted(os.listdir(ticket_dir)):
            if not filename.endswith(".json") or filename.startswith("."):
                continue
            try:
                with open(os.path.join(ticket_dir, filename), encoding="utf-8") as f:
                    json.load(f)
            except (json.JSONDecodeError, ValueError, OSError):
                lines.append(f"CORRUPT: {ticket_id}/{filename} — invalid JSON")
                issue_count += 1

    # ── Check 2: CREATE event presence ───────────────────────────────────────
    # The reducer warns to stderr on corrupt events; the dispatcher ran it with
    # 2>/dev/null, so silence its stderr here for byte-parity.
    import contextlib
    import io

    for ticket_id in _ticket_dirs(tracker):
        ticket_dir = os.path.join(tracker, ticket_id)
        with contextlib.redirect_stderr(io.StringIO()):
            state = reduce_ticket(ticket_dir)
        # reduce_ticket returns None (no CREATE) or a state dict; an error/ghost
        # ticket reduces to status 'fsck_needed' or 'error'.
        if state is None:
            lines.append(f"MISSING_CREATE: {ticket_id} — no CREATE event found")
            issue_count += 1
        elif state.get("status") == "fsck_needed":
            lines.append(
                f"CORRUPT_CREATE: {ticket_id} — CREATE event present but missing "
                "required fields (ticket_type or title)"
            )
            issue_count += 1
        elif state.get("status") == "error":
            lines.append(f"MISSING_CREATE: {ticket_id} — no CREATE event found")
            issue_count += 1
        # Surface a resolved cross-clone STATUS/claim race (audit reliability #1, story
        # 3003). The reducer records these in derived state; report the most recent one.
        if state and state.get("status_fork_resolutions"):
            last = state["status_fork_resolutions"][-1]
            lines.append(
                f"STATUS_FORK_RESOLVED: {ticket_id} — concurrent claim/status race resolved "
                f"(dropped uuid={last.get('dropped_uuid')})"
            )
            issue_count += 1

    # ── Check 3: stale .git/index.lock cleanup ───────────────────────────────
    git_dir = _resolve_tracker_git_dir(tracker)
    if git_dir:
        lock_file = os.path.join(git_dir, "index.lock")
        if os.path.isfile(lock_file):
            stale = False
            try:
                stale = (time.time() - os.path.getmtime(lock_file)) > 300
            except OSError:
                stale = False
            if stale:
                if no_mutate:
                    lines.append(
                        "WARN: stale .git/index.lock present (older than 5 minutes) "
                        "— not removed (read-only)"
                    )
                else:
                    try:
                        os.remove(lock_file)
                    except OSError:
                        pass
                    lines.append("FIXED: removed stale .git/index.lock (older than 5 minutes)")
            else:
                lines.append("WARN: .git/index.lock exists (younger than 5 minutes) — not removed")

    # ── Check 4: SNAPSHOT source_event_uuids consistency ─────────────────────
    for ticket_id in _ticket_dirs(tracker):
        ticket_dir = os.path.join(tracker, ticket_id)

        def _snap_findings(_dir: str = ticket_dir, _tid: str = ticket_id) -> list[str]:
            out: list[str] = []
            for snap_name in sorted(
                n
                for n in os.listdir(_dir)
                if n.endswith("-SNAPSHOT.json") and not n.startswith(".")
            ):
                out.extend(_check_snapshot(_dir, _tid, snap_name))
            return out

        findings = _snap_findings()
        # RC2b Option 1: rebuild a stale snapshot that dropped a merged-in orphan, then
        # re-check. A rebuild folds the orphan back in (SNAPSHOT_INCONSISTENT / a KNOWN
        # ORPHAN_EVENT before the snapshot) — the remediation A3 runs against the live store.
        rebuildable = any("SNAPSHOT_INCONSISTENT" in f or "ORPHAN_EVENT" in f for f in findings)
        if repair_snapshots and not no_mutate and rebuildable:
            from rebar._commands.compact import rebuild_snapshot_from_full_log

            if rebuild_snapshot_from_full_log(tracker, ticket_id, ticket_dir):
                post = _snap_findings()
                resolved = len(findings) - len(post)
                if resolved > 0:
                    lines.append(
                        f"FIXED: rebuilt SNAPSHOT for {ticket_id} ({resolved} finding(s) resolved)"
                    )
                findings = post

        lines.extend(findings)
        issue_count += len(findings)

    # ── Check 4.5: push-pending (informational; no issue_count) ──────────────
    pp = _push_pending(tracker)
    if pp:
        lines.append(pp)

    # ── Check 4.6: configured-vs-mounted branch mismatch (informational) ──────
    bm = _branch_mismatch(tracker, repo_root)
    if bm:
        lines.append(bm)

    # ── Check 5: forward-compat — event types newer than this binary (P2.3) ───
    # Informational WARN (no issue_count, like push-pending): an unknown event_type
    # is preserved-and-ignored by replay, so the store is NOT corrupt — but the
    # event's effect is INVISIBLE until this binary is upgraded (e.g. a reconcile
    # host on an old binary would reduce without it and push a stale tag set to
    # Jira). Surface it so the otherwise-silent rollout window is detectable.
    # Generic over KNOWN_EVENT_TYPES — not specific to any one new type. The
    # event_type is read from the canonical filename suffix (``{ts}-{uuid}-{TYPE}``,
    # uuid hyphens precede it), matching reducer/_sort.event_sort_key.
    from rebar.reducer._version import is_unknown_newer_type

    unknown_types: set[str] = set()
    for ticket_id in _ticket_dirs(tracker):
        ticket_dir = os.path.join(tracker, ticket_id)
        for filename in os.listdir(ticket_dir):
            if not filename.endswith(".json") or filename.startswith("."):
                continue
            etype = filename[: -len(".json")].rsplit("-", 1)[-1]
            if is_unknown_newer_type(etype):
                unknown_types.add(etype)
    if unknown_types:
        lines.append(
            "WARN: store contains event types newer than this rebar understands: "
            f"{', '.join(sorted(unknown_types))} — upgrade rebar. These events are "
            "preserved on disk but their effect is invisible until you upgrade (a "
            "reconcile host on an old binary may push stale state)."
        )

    # Informational ensure-registry status (epic odd-vortex-elbow / WS3), derived
    # WITHOUT running the sweep: M = registered units, N = applied units present in
    # the git-ignored .ensure-applied marker (intersected with the registry so a
    # stale marker id can't inflate N). Lowercase tag ⇒ text-only, like
    # a3-remediation:/fsck complete — intentionally NOT lifted into --output json,
    # so it never inflates issue_count or flips the exit code.
    from rebar._store import ensures as _ensures

    registry = _ensures.registry_ids()
    applied_n = len(_ensures.applied_ids(tracker) & registry)
    ensures_line = f"ensures: {applied_n}/{len(registry)} applied"
    if applied_n < len(registry):
        ensures_line += " — run `rebar fsck --repair` to converge"
    lines.append(ensures_line)

    return lines, issue_count


def _check_snapshot(ticket_dir: str, ticket_id: str, snapshot_filename: str) -> list[str]:
    out: list[str] = []
    try:
        with open(os.path.join(ticket_dir, snapshot_filename), encoding="utf-8") as f:
            snapshot = json.load(f)
    except (json.JSONDecodeError, OSError):
        return out
    source_uuids = snapshot.get("data", {}).get("source_event_uuids", [])
    if not source_uuids:
        return out

    # Map uuid -> (filename, event_type). The type is parsed from the filename
    # suffix (``{ts}-{uuid}-{TYPE}.json``, written by event_filename from the event's
    # own event_type), so it agrees with the event body without a second file read.
    event_files: dict[str, tuple[str, str]] = {}
    for name in sorted(os.listdir(ticket_dir)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        # I1: a folded source renamed to ``*.retired`` is NOT a live event — it must
        # never read as "source UUID still exists" (SNAPSHOT_INCONSISTENT). The
        # ``.json`` filter above already excludes ``*.json.retired``; this guard makes
        # the intent explicit and keeps fsck correct if the suffix scheme ever changes.
        if not is_active_event(name):
            continue
        if name == snapshot_filename:
            continue
        parts = name.split("-", 1)
        if len(parts) < 2:
            continue
        rest_no_ext = parts[1].rsplit(".json", 1)[0]
        type_split = rest_no_ext.rsplit("-", 1)
        if len(type_split) < 2:
            continue
        event_files[type_split[0]] = (name, type_split[1])

    source_uuid_set = set(source_uuids)
    for u in source_uuids:
        if u in event_files:
            out.append(
                f"SNAPSHOT_INCONSISTENT: {ticket_id}/{snapshot_filename} — source UUID "
                f"{u} still exists as {event_files[u][0]}"
            )
    for file_uuid, (name, etype) in event_files.items():
        # Compaction folds ONLY KNOWN_EVENT_TYPES into source_event_uuids
        # (_commands/compact.py excludes any other type from both deletion and the
        # source list). A pre-snapshot event of a non-KNOWN type (e.g. the
        # reducer-ignored REVIEW_RESULT, or a forward-compat type from a newer
        # clone) is therefore *correctly* absent from source_event_uuids — flagging
        # it ORPHAN_EVENT is a false positive. Stay symmetric with compaction: only
        # a genuinely orphaned KNOWN-type event is real data loss.
        if etype not in KNOWN_EVENT_TYPES:
            continue
        if name < snapshot_filename and "-SNAPSHOT.json" not in name:
            if file_uuid not in source_uuid_set:
                out.append(
                    f"ORPHAN_EVENT: {ticket_id}/{name} — pre-snapshot event not "
                    "captured in source_event_uuids"
                )
    return out


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


def _branch_mismatch(tracker: str, repo_root=None) -> str | None:
    """Informational WARN when the tracker worktree's actually-checked-out branch
    differs from the configured ``tracker.branch``. 'configured' = the precedence-
    resolved config (from ``repo_root`` when known, else the MAIN repo = the tracker's
    parent); 'mounted' = the branch the worktree has checked out. This catches a
    ``tracker.branch`` changed in project config AFTER init: the store is NOT
    auto-migrated, so it stays on the old branch. Best-effort: skip on a malformed
    config or a detached/unreadable HEAD."""
    root = repo_root if repo_root is not None else os.path.dirname(os.path.realpath(tracker))
    try:
        configured = config.tickets_branch(root)
    except config.ConfigError:
        return None
    cp = subprocess.run(
        ["git", "-C", tracker, "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    mounted = cp.stdout.strip()
    if cp.returncode != 0 or not mounted or mounted == configured:
        return None  # detached/unreadable, or a match — nothing to report
    return (
        f"WARN: configured tracker.branch '{configured}' does not match the mounted "
        f"branch '{mounted}' — the store was initialized on '{mounted}' and is NOT "
        "auto-migrated. Revert the config, or re-init on the new branch."
    )


def _push_pending(tracker: str) -> str | None:
    def _git(*args: str) -> subprocess.CompletedProcess:
        return run_git(tracker, *args, check=False)

    # Branch + remote resolved from the MAIN repo config (the tracker's parent);
    # best-effort: a malformed config yields no push-pending notice rather than a crash.
    try:
        base = os.path.dirname(os.path.realpath(tracker))
        branch = config.tickets_branch(base)
        remote = config.tickets_remote(base)
    except config.ConfigError:
        return None
    remote_ref = f"{remote}/{branch}"
    if _git("remote", "get-url", remote).returncode != 0:
        return None
    if _git("rev-parse", "--verify", remote_ref).returncode != 0:
        return None
    cp = _git("rev-list", f"{remote_ref}..HEAD", "--count")
    try:
        ahead = int((cp.stdout or "0").strip() or "0")
    except ValueError:
        ahead = 0
    if ahead > 0:
        return (
            f"PUSH_PENDING: local '{branch}' branch is ahead of {remote_ref} by {ahead} "
            "commit(s) — push pending (run a ticket write to retry the push, or check "
            "connectivity to origin)"
        )
    return None


def _transform_json(text: str) -> str:
    """Port of the dispatcher's text→json transform (ticket-fsck.sh lines 37-69)."""
    issues, fixed = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("FIXED:"):
            fixed.append(line[len("FIXED:") :].strip())
            continue
        if line.startswith("fsck complete"):
            continue
        m = re.match(r"^([A-Z_]+):\s*(.*)$", line)
        if not m:
            continue
        kind, rest = m.group(1).lower(), m.group(2)
        item = {"kind": kind}
        head, sep, detail = rest.partition(" — ")
        if sep and kind in _STRUCTURED_KINDS:
            if "/" in head:
                tid, _, fn = head.partition("/")
                item["ticket_id"], item["filename"] = tid, fn
            else:
                item["ticket_id"] = head
            item["detail"] = detail
        else:
            item["detail"] = rest
        issues.append(item)
    return json.dumps({"issues": issues, "fixed": fixed, "issue_count": len(issues)})


def _has_remote(tracker: str) -> bool:
    return bool(_git(tracker, "remote").stdout.strip())


def _reconciler_pause(repo_root=None) -> bool:
    """Best-effort: disable the reconcile-bridge GHA schedule for the repair window and
    confirm no pass is mid-flight (the leased CAS ``refs/reconciler/lock`` would expire,
    so it is not the pause mechanism). Returns True iff we disabled it (→ re-enable in a
    failsafe). A missing/unauthenticated ``gh`` just logs and returns False (the batched,
    pre-tagged, committed design keeps a stray write recoverable)."""
    cp = subprocess.run(
        ["gh", "workflow", "disable", "reconcile-bridge.yml"], capture_output=True, text=True
    )
    return cp.returncode == 0


def _reconciler_resume() -> None:
    subprocess.run(
        ["gh", "workflow", "enable", "reconcile-bridge.yml"], capture_output=True, text=True
    )


def _reconciler_in_flight(repo_root=None) -> bool:
    """Return True if a reconciler pass is (or may be) mid-flight — the in-flight guard
    the destructive live repair runs AFTER disabling the schedule.

    Disabling the GHA schedule stops the NEXT pass, not one already running; a pass that
    started before we disabled would race our batched writes. The pass holds the leased
    CAS ``refs/reconciler/lock`` for its duration, so ``check_pass_lock`` is the mid-flight
    probe. Fail-CLOSED: if the lock state cannot be read (``ReconcileLockError``) — or the
    advisory module can't be imported — we report in-flight=True so an indeterminate state
    aborts the repair rather than writing under a possibly-live reconciler. A repo that
    has never run the reconciler (ref absent) reads free → False → repair proceeds."""
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


def fsck_cli(argv: list[str], *, repo_root=None, no_mutate: bool = False) -> int:
    # RC2b Option 1: --repair-snapshots opts into rebuilding a stale SNAPSHOT that has
    # a merged-in pre-snapshot orphan (drives the live store to fsck-zero — A3). Strip
    # it before output parsing; it is honored only when mutation is allowed.
    repair_snapshots = "--repair-snapshots" in argv
    do_repair = "--repair" in argv
    dry_run = "--dry-run" in argv
    limit: int | None = None
    for a in argv:
        if a.startswith("--limit="):
            try:
                limit = int(a[len("--limit=") :])
            except ValueError:
                sys.stderr.write(f"Error: invalid --limit value in '{a}'\n")
                return 2
    argv = [
        a
        for a in argv
        if a not in ("--repair-snapshots", "--repair", "--dry-run") and not a.startswith("--limit=")
    ]
    try:
        fmt, _rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        # Dir-mismatch hint: the configured tracker.dir is absent, but a default-named
        # store still exists alongside → tracker.dir was changed without migrating.
        repo_guess = os.path.dirname(os.path.realpath(tracker))
        legacy = os.path.join(repo_guess, ".tickets-tracker")
        mismatch_hint = ""
        if os.path.realpath(legacy) != os.path.realpath(tracker) and os.path.isdir(legacy):
            mismatch_hint = (
                f"\nWARN: configured tracker.dir resolves to {tracker} (absent), but a "
                f"store exists at {legacy} — tracker.dir was changed without migrating."
            )
        if fmt == "json":
            sys.stdout.write(_transform_json(mismatch_hint.strip()) + "\n")
            return 1
        sys.stderr.write(
            f"Error: ticket system not initialized ({tracker} not found).\n"
            f"Run 'ticket init' first.{mismatch_hint}\n"
        )
        return 1

    # ── A3 remediation (--repair): drive the store to fsck-zero ──────────────
    if do_repair:
        if no_mutate and not dry_run:
            sys.stderr.write("Error: --repair requires mutation; use --dry-run for a preview\n")
            return 2
        repair_lines, _unresolved = _repair_run(
            tracker, dry_run=dry_run, limit=limit, repo_root=repo_root
        )
        # Fold the idempotent ensure-sweep into the existing "drive healthy" verb
        # (epic odd-vortex-elbow / WS3): a DISTINCT phase from the A3 data-repair —
        # run_ensures takes + releases the store write lock itself (no nesting), and
        # is log-and-continue (a failed unit never rolls back committed ticket data).
        # Only on a live run; --dry-run stays read-only and does not sweep.
        ensure_lines: list[str] = []
        if not dry_run:
            from rebar._store import ensures as _ensures

            outcomes = _ensures.run_ensures(tracker)
            changed = [o.id for o in outcomes if o.status == "changed"]
            failed = [o.id for o in outcomes if o.status == "failed"]
            ensure_lines.append(
                f"ensures: swept {len(outcomes)} unit(s); "
                f"{len(changed)} changed, {len(failed)} failed"
            )
            ensure_lines += [
                f"  ensure {o.id}: {o.status} ({o.detail})" for o in outcomes if o.status != "ok"
            ]
        # Re-scan for the residual state (read-only in dry-run so it writes nothing).
        scan_lines, issue_count = _scan(tracker, no_mutate or dry_run, repo_root)
        summary = (
            "fsck complete: no issues found"
            if issue_count == 0
            else f"fsck complete: {issue_count} issues found"
        )
        rc = 0 if issue_count == 0 else 1
        full = "\n".join(repair_lines + ensure_lines + scan_lines + [summary])
        sys.stdout.write((_transform_json(full) if fmt == "json" else full) + "\n")
        return rc

    # ``no_mutate`` is passed by the caller (the library's read-only fsck surface),
    # not read from the environment: read paths (list/show via rebar.fsck(report_only=
    # True)) pass no_mutate=True so they never delete the stale lock; the CLI `fsck`
    # always mutates (default False).
    lines, issue_count = _scan(tracker, no_mutate, repo_root, repair_snapshots=repair_snapshots)
    summary = (
        "fsck complete: no issues found"
        if issue_count == 0
        else f"fsck complete: {issue_count} issues found"
    )
    rc = 0 if issue_count == 0 else 1

    if fmt == "json":
        full = "\n".join(lines + [summary])
        sys.stdout.write(_transform_json(full) + "\n")
        return rc
    sys.stdout.write("\n".join(lines + [summary]) + "\n")
    return rc
