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

from rebar import config

# ``--repair`` cluster extracted to the ``fsck_repair`` leaf module (module-size
# split, epic 716f). The two shared filesystem helpers ``_ticket_dirs`` and
# ``_resolve_tracker_git_dir`` live there and are re-imported here (one-way:
# diagnostic → repair, never back); the rest are re-exported so ``fsck.<symbol>``
# attribute access (gitutil/fsck_recover + the snapshot-check tests) keeps resolving.
from rebar._commands.fsck_repair import (  # noqa: F401
    _AUTO_RECOVER_ORPHAN_TYPES,
    _HUMAN_TRIAGE_ORPHAN_TYPES,
    _repair_plan,
    _repair_run,
    _repair_ticket,
    _resolve_tracker_git_dir,
    _ticket_dirs,
)
from rebar._engine_support.output import OutputFormatError, parse_output
from rebar._store import compat
from rebar._store.gitutil import run_git
from rebar.reducer import KNOWN_EVENT_TYPES, reduce_ticket
from rebar.reducer._cache import RETIRED_SUFFIX, is_active_event

_STRUCTURED_KINDS = {
    "corrupt",
    "corrupt_create",
    "missing_create",
    "snapshot_inconsistent",
    "snapshot_stale_channel",
    "orphan_event",
    "status_fork_resolved",
}


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

    # Store-wide authorship PRESENCE tally (3183): summed from each ticket's reduced
    # ``authorship`` summary — presence only, never a crypto check (see verify-authorship).
    signed_total = 0
    unsigned_total = 0
    for ticket_id in _ticket_dirs(tracker):
        ticket_dir = os.path.join(tracker, ticket_id)
        with contextlib.redirect_stderr(io.StringIO()):
            state = reduce_ticket(ticket_dir)
        _authorship = state.get("authorship") if isinstance(state, dict) else None
        if isinstance(_authorship, dict):
            signed_total += int(_authorship.get("signed") or 0)
            unsigned_total += int(_authorship.get("unsigned") or 0)
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
        # re-check (folds the orphan back in) — the remediation A3 runs against the live store.
        # SNAPSHOT_STALE_CHANNEL (story 568c) rebuilds the same way: replaying the retained
        # CREATE under include_retired re-projects the missing creation_channel.
        rebuildable = any(
            "SNAPSHOT_INCONSISTENT" in f or "ORPHAN_EVENT" in f or "SNAPSHOT_STALE_CHANNEL" in f
            for f in findings
        )
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
    # Informational WARN (no issue_count, like push-pending): an unknown event_type is
    # preserved-and-ignored by replay, so the store is NOT corrupt — but its effect is
    # INVISIBLE until this binary is upgraded (e.g. a reconcile host on an old binary would
    # reduce without it and push stale state). The event_type is read from the canonical
    # filename suffix (``{ts}-{uuid}-{TYPE}``), matching reducer/_sort.event_sort_key.
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

    # Informational ensure-registry status (epic odd-vortex-elbow / WS3), derived WITHOUT
    # running the sweep: N applied (in the git-ignored .ensure-applied marker, intersected with
    # the registry) / M registered. Lowercase tag ⇒ text-only — never in --output json.
    from rebar._store import ensures as _ensures

    registry = _ensures.registry_ids()
    applied_n = len(_ensures.applied_ids(tracker) & registry)
    ensures_line = f"ensures: {applied_n}/{len(registry)} applied"
    if applied_n < len(registry):
        ensures_line += " — run `rebar fsck --repair` to converge"
    lines.append(ensures_line)

    # Advisory authorship line (3183): store-wide count of events WITHOUT an author_sig
    # (presence only, like the ensures line — text-only, never in --output json / issue_count).
    authorship_line = f"authorship: {signed_total} signed, {unsigned_total} unsigned event(s)"
    if unsigned_total:
        authorship_line += " — run `rebar verify-authorship`"
    lines.append(authorship_line)

    return lines, issue_count


def _has_retired_create(ticket_dir: str) -> bool:
    """True when the ticket retains its genesis CREATE as a folded ``*.retired`` source
    (``{ts}-{uuid}-CREATE.json.retired``). Compaction renames folded events rather than
    deleting them (invariant I1), so a compacted ticket normally still carries its CREATE
    here — which is what lets ``rebuild_snapshot_from_full_log`` (``include_retired=True``)
    replay the CREATE and re-project ``creation_channel`` into a refreshed SNAPSHOT."""
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return False
    return any(n.endswith("-CREATE.json" + RETIRED_SUFFIX) for n in names)


def _check_snapshot(ticket_dir: str, ticket_id: str, snapshot_filename: str) -> list[str]:
    out: list[str] = []
    try:
        with open(os.path.join(ticket_dir, snapshot_filename), encoding="utf-8") as f:
            snapshot = json.load(f)
    except (json.JSONDecodeError, OSError):
        return out
    _data = snapshot.get("data", {})
    # Creation-channel provenance drift (story 568c): a PRE-feature SNAPSHOT — one whose
    # compiled_state was compacted before `creation_channel` existed — carries no channel, and
    # on SNAPSHOT-only replay there is no CREATE to re-infer from. Read-time re-inference
    # (process_snapshot) already keeps reads correct, but the DURABLE snapshot stays stale.
    # When the ticket still retains its CREATE as a folded `.retired` source, the snapshot is
    # rebuildable: `--repair-snapshots` re-projects the channel via
    # rebuild_snapshot_from_full_log (which replays the retained CREATE). Gate strictly on a
    # real compiled_state dict that lacks the key AND a retained CREATE, so a post-feature
    # snapshot (channel present) never trips this.
    _compiled = _data.get("compiled_state")
    if (
        isinstance(_compiled, dict)
        and "creation_channel" not in _compiled
        and _has_retired_create(ticket_dir)
    ):
        out.append(
            f"SNAPSHOT_STALE_CHANNEL: {ticket_id}/{snapshot_filename} — compiled_state "
            "predates creation_channel; rebuild from the retained CREATE to persist it"
        )
    source_uuids = _data.get("source_event_uuids", [])
    if not source_uuids:
        return out

    # Map uuid -> (filename, event_type). The type is parsed from the filename suffix
    # (``{ts}-{uuid}-{TYPE}.json``), so it agrees with the event body without a second read.
    event_files: dict[str, tuple[str, str]] = {}
    for name in sorted(os.listdir(ticket_dir)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        # I1: a folded source renamed to ``*.retired`` is NOT a live event — it must never
        # read as "source UUID still exists" (SNAPSHOT_INCONSISTENT). Explicit guard on top
        # of the ``.json`` filter above (which already excludes ``*.json.retired``).
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
        # Compaction folds ONLY KNOWN_EVENT_TYPES into source_event_uuids (compact.py
        # excludes any other type from deletion + the source list), so a pre-snapshot event
        # of a non-KNOWN type (reducer-ignored REVIEW_RESULT, or a forward-compat type from a
        # newer clone) is *correctly* absent — flagging it ORPHAN_EVENT is a false positive.
        # Stay symmetric with compaction: only a genuinely orphaned KNOWN-type event is loss.
        if etype not in KNOWN_EVENT_TYPES:
            continue
        if name < snapshot_filename and "-SNAPSHOT.json" not in name:
            if file_uuid not in source_uuid_set:
                out.append(
                    f"ORPHAN_EVENT: {ticket_id}/{name} — pre-snapshot event not "
                    "captured in source_event_uuids"
                )
    return out


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


def _transform_json(text: str, compat_error: dict | None = None) -> str:
    """Port of the dispatcher's text→json transform (ticket-fsck.sh lines 37-69). Story
    21dd: attach a ``{"kind","detail"}`` ``compat_error`` (incompatible/corrupt store) so
    ``jq -e '.compat_error.kind'`` detects it WITHOUT the read being blocked."""
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
    payload: dict = {"issues": issues, "fixed": fixed, "issue_count": len(issues)}
    if compat_error is not None:
        payload["compat_error"] = compat_error
    return json.dumps(payload)


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

    # Story 21dd: the read-only diagnostic surfaces an incompatible/corrupt store as a
    # structured `compat_error` (JSON) + WARNING, WITHOUT blocking (repair is gated via
    # lock.acquire() instead); the exit code is unchanged.
    compat_error = compat.describe_store_compat(tracker)
    if compat_error is not None:
        sys.stderr.write(f"WARNING: {compat_error['detail']}\n")

    if fmt == "json":
        full = "\n".join(lines + [summary])
        sys.stdout.write(_transform_json(full, compat_error) + "\n")
        return rc
    sys.stdout.write("\n".join(lines + [summary]) + "\n")
    return rc
