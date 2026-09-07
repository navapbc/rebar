"""Walk the store and run per-ticket ``fsck`` validators.

The scan checks event JSON, CREATE presence, snapshot source consistency, orphan events, and
unsupported event types. Stale index-lock cleanup is the ordinary scan's only mutation and is
disabled by ``no_mutate=True``; ``repair_snapshots=True`` may also restore sources and rebuild
snapshots. Tracker-wide health and per-environment authorship remain delegated to their leaf
modules. Authorship totals come from the store walk, while the ensure advisory separately reads
the registry and local applied marker.

Dependencies point from the command driver through this module into health, repair, and
authorship leaves without back imports.
"""

from __future__ import annotations

import json
import os
import time

from rebar._commands.fsck_authorship import EnvAuthorshipTally
from rebar._commands.fsck_repair import (
    _is_stale_channel_snapshot,
)
from rebar._commands.fsck_repair import (
    is_snapshot_orphan as _is_snapshot_orphan,
)
from rebar._commands.fsck_repair import (
    missing_sources_finding as _missing_sources_finding,
)
from rebar._commands.fsck_repair import (
    repair_or_plan as _repair_or_plan,
)
from rebar._commands.fsck_tracker_health import _tracker_health
from rebar._store.gitutil import (
    _dir_is_archived,
    _reclaim_if_stale_index_lock,
    _resolve_tracker_git_dir,
    _ticket_dirs,
)
from rebar.reducer import reduce_ticket
from rebar.reducer._cache import is_active_event


def _check_json_validity(
    tracker: str, env_authorship: EnvAuthorshipTally, *, include_archived: bool = False
) -> tuple[list[str], int]:
    """Check event JSON while feeding the per-environment authorship tally.

    Every ticket contributes to the store-wide tally. ``include_archived`` controls only which
    parse findings are reported.
    """
    lines: list[str] = []
    issues = 0
    for ticket_id in _ticket_dirs(tracker, include_archived=True):
        ticket_dir = os.path.join(tracker, ticket_id)
        reported = include_archived or not _dir_is_archived(ticket_dir)
        for filename in sorted(os.listdir(ticket_dir)):
            if not filename.endswith(".json") or filename.startswith("."):
                continue
            try:
                with open(os.path.join(ticket_dir, filename), encoding="utf-8") as f:
                    env_authorship.observe(filename, json.load(f))
            except (json.JSONDecodeError, ValueError, OSError):
                if reported:
                    lines.append(f"CORRUPT: {ticket_id}/{filename} — invalid JSON")
                    issues += 1
    return lines, issues


def _check_create_events(
    tracker: str, *, include_archived: bool = False
) -> tuple[list[str], int, int, int]:
    """Check for a usable CREATE and return report lines, issues, and authorship totals.

    Totals count signature presence without cryptographic verification. Every ticket
    contributes, while ``include_archived`` controls only reported findings.
    """
    # The reducer warns to stderr on corrupt events; those warnings are noise here
    # (not part of the fsck output contract), so silence its stderr.
    import contextlib
    import io

    lines: list[str] = []
    issues = 0
    signed_total = 0
    unsigned_total = 0
    for ticket_id in _ticket_dirs(tracker, include_archived=True):
        ticket_dir = os.path.join(tracker, ticket_id)
        reported = include_archived or not _dir_is_archived(ticket_dir)
        with contextlib.redirect_stderr(io.StringIO()):
            state = reduce_ticket(ticket_dir)
        _authorship = state.get("authorship") if isinstance(state, dict) else None
        if isinstance(_authorship, dict):
            signed_total += int(_authorship.get("signed") or 0)
            unsigned_total += int(_authorship.get("unsigned") or 0)
        if not reported:
            continue
        # reduce_ticket returns None (no CREATE) or a state dict; an error/ghost
        # ticket reduces to status 'fsck_needed' or 'error'.
        if state is None:
            lines.append(f"MISSING_CREATE: {ticket_id} — no CREATE event found")
            issues += 1
        elif state.get("status") == "fsck_needed":
            lines.append(
                f"CORRUPT_CREATE: {ticket_id} — CREATE event present but missing "
                "required fields (ticket_type or title)"
            )
            issues += 1
        elif state.get("status") == "error":
            lines.append(f"MISSING_CREATE: {ticket_id} — no CREATE event found")
            issues += 1
        # Surface a resolved cross-clone STATUS/claim race (audit reliability #1, story
        # 3003). The reducer records these in derived state; report the most recent one.
        if state and state.get("status_fork_resolutions"):
            last = state["status_fork_resolutions"][-1]
            lines.append(
                f"STATUS_FORK_RESOLVED: {ticket_id} — concurrent claim/status race resolved "
                f"(dropped uuid={last.get('dropped_uuid')})"
            )
            # Resolved status forks are report-only derived state. The reducer has already
            # settled them, and ``fsck._NEVER_COUNTED_KINDS`` excludes them from JSON totals.
    return lines, issues, signed_total, unsigned_total


def _check_index_lock(tracker: str, no_mutate: bool) -> list[str]:
    """Check 3 — stale ``.git/index.lock`` cleanup, the ONLY mutation fsck performs.

    Never counted as an issue; suppressed entirely by ``no_mutate`` (the read-only surfaces).
    """
    lines: list[str] = []
    git_dir = _resolve_tracker_git_dir(tracker)
    if not git_dir:
        return lines
    lock_file = os.path.join(git_dir, "index.lock")
    if not os.path.isfile(lock_file):
        return lines
    try:
        stale = (time.time() - os.path.getmtime(lock_file)) > 300
    except OSError:
        stale = False
    if not stale:
        lines.append("WARN: .git/index.lock exists (younger than 5 minutes) — not removed")
    elif no_mutate:
        lines.append(
            "WARN: stale .git/index.lock present (older than 5 minutes) — not removed (read-only)"
        )
    else:
        # The write-path helper rechecks device, inode, and age before unlinking. This prevents
        # removal of a replacement lock created during the race window.
        _reclaim_if_stale_index_lock(tracker)
        lines.append("FIXED: removed stale .git/index.lock (older than 5 minutes)")
    return lines


def _check_ticket_snapshots(
    tracker: str,
    *,
    no_mutate: bool,
    repair_snapshots: bool,
    dry_run: bool,
    include_archived: bool = False,
) -> tuple[list[str], int]:
    """Check 4 — SNAPSHOT ``source_event_uuids`` consistency, per ticket.

    With ``repair_snapshots`` this also drives RC2b Option 1: rebuild a stale snapshot that
    dropped a merged-in orphan, then re-check (folding the orphan back in) — the remediation A3
    runs against the live store. SNAPSHOT_STALE_CHANNEL (story 568c) rebuilds the same way:
    replaying the retained CREATE under include_retired re-projects the missing creation_channel.
    """
    lines: list[str] = []
    issues = 0
    for ticket_id in _ticket_dirs(tracker, include_archived=include_archived):
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
        if repair_snapshots:
            emitted, findings = _repair_or_plan(
                tracker,
                ticket_id,
                ticket_dir,
                findings,
                _snap_findings,
                no_mutate=no_mutate,
                dry_run=dry_run,
            )
            lines.extend(emitted)

        lines.extend(findings)
        issues += len(findings)
    return lines, issues


def _check_forward_compat(tracker: str, *, include_archived: bool = False) -> list[str]:
    """Check 5 — event types newer than this binary (P2.3).

    Informational WARN (never counted, like push-pending): an unknown event_type is
    preserved-and-ignored by replay, so the store is NOT corrupt — but its effect is INVISIBLE
    until this binary is upgraded (e.g. a reconcile host on an old binary would reduce without it
    and push stale state). The event_type is read from the canonical filename suffix
    (``{ts}-{uuid}-{TYPE}``), matching reducer/_sort.event_sort_key.
    """
    from rebar.reducer._version import is_unknown_newer_type

    unknown_types: set[str] = set()
    for ticket_id in _ticket_dirs(tracker, include_archived=include_archived):
        ticket_dir = os.path.join(tracker, ticket_id)
        for filename in os.listdir(ticket_dir):
            if not filename.endswith(".json") or filename.startswith("."):
                continue
            etype = filename[: -len(".json")].rsplit("-", 1)[-1]
            if is_unknown_newer_type(etype):
                unknown_types.add(etype)
    if not unknown_types:
        return []
    return [
        "WARN: store contains event types newer than this rebar understands: "
        f"{', '.join(sorted(unknown_types))} — upgrade rebar. These events are "
        "preserved on disk but their effect is invisible until you upgrade (a "
        "reconcile host on an old binary may push stale state)."
    ]


def _advisory_lines(tracker: str, signed_total: int, unsigned_total: int) -> list[str]:
    """Return uncounted text advisories for ensure status and unsigned event presence.

    Ensure status reads the local applied marker without running the sweep. These lines do not
    appear in JSON output.
    """
    from rebar._store import ensures as _ensures

    registry = _ensures.registry_ids()
    applied_n = len(_ensures.applied_ids(tracker) & registry)
    ensures_line = f"ensures: {applied_n}/{len(registry)} applied"
    if applied_n < len(registry):
        ensures_line += " — run `rebar fsck --repair` to converge"

    authorship_line = f"authorship: {signed_total} signed, {unsigned_total} unsigned event(s)"
    if unsigned_total:
        authorship_line += " — run `rebar verify-identity`"
    return [ensures_line, authorship_line]


def _scan(
    tracker: str,
    no_mutate: bool,
    repo_root=None,
    *,
    repair_snapshots: bool = False,
    dry_run: bool = False,
    include_archived: bool = False,
) -> tuple[list[str], int]:
    """Walk the store and run every check, in report order.

    Each check is a function above; this orchestrates them and accumulates
    ``(lines, issue_count)``. Order is part of the output contract. Check 1 must run first for a
    second reason: it is the pass that populates the per-env authorship tally that check 4.9
    (inside ``_tracker_health``) reads.
    """
    lines: list[str] = []
    issue_count = 0

    # Per-env authorship tally (bug ed5c): fed from the payloads check 1 already parses, so the
    # per-env signed-rate check costs no extra pass over the store. Reported at the end, next to
    # the store-wide authorship line it complements.
    env_authorship = EnvAuthorshipTally()

    json_lines, json_issues = _check_json_validity(
        tracker, env_authorship, include_archived=include_archived
    )
    create_lines, create_issues, signed_total, unsigned_total = _check_create_events(
        tracker, include_archived=include_archived
    )
    snapshot_lines, snapshot_issues = _check_ticket_snapshots(
        tracker,
        no_mutate=no_mutate,
        repair_snapshots=repair_snapshots,
        dry_run=dry_run,
        include_archived=include_archived,
    )
    # Checks 4.5–4.7 + 4.9 are tracker-level, not per-ticket: they live in fsck_tracker_health.
    tracker_lines, tracker_issues = _tracker_health(tracker, repo_root, env_authorship)

    lines += json_lines
    lines += create_lines
    lines += _check_index_lock(tracker, no_mutate)
    lines += snapshot_lines
    lines += tracker_lines
    lines += _check_forward_compat(tracker, include_archived=include_archived)
    lines += _advisory_lines(tracker, signed_total, unsigned_total)
    issue_count += json_issues + create_issues + snapshot_issues + tracker_issues

    # Per-environment authorship is counted because it isolates an active writer that signs no
    # events, which the advisory store-wide total cannot identify.
    env_findings = env_authorship.findings()
    lines += env_findings
    issue_count += len(env_findings)

    return lines, issue_count


def _active_event_map(ticket_dir: str, snapshot_filename: str) -> dict[str, tuple[str, str]]:
    """Map each active event UUID to its filename and type without reading event bodies.

    Types come from canonical filename suffixes. ``is_active_event`` excludes folded retired
    sources, and the inspected snapshot is excluded from comparison with itself.
    """
    event_files: dict[str, tuple[str, str]] = {}
    for name in sorted(os.listdir(ticket_dir)):
        if not name.endswith(".json") or name.startswith("."):
            continue
        if not is_active_event(name) or name == snapshot_filename:
            continue
        parts = name.split("-", 1)
        if len(parts) < 2:
            continue
        rest_no_ext = parts[1].rsplit(".json", 1)[0]
        type_split = rest_no_ext.rsplit("-", 1)
        if len(type_split) < 2:
            continue
        event_files[type_split[0]] = (name, type_split[1])
    return event_files


def _check_snapshot(ticket_dir: str, ticket_id: str, snapshot_filename: str) -> list[str]:
    out: list[str] = []
    try:
        with open(os.path.join(ticket_dir, snapshot_filename), encoding="utf-8") as f:
            snapshot = json.load(f)
    except (json.JSONDecodeError, OSError):
        return out
    _data = snapshot.get("data", {})
    # A snapshot without ``creation_channel`` is stale only when compiled state exists and a
    # retired CREATE can supply the missing value. ``--repair-snapshots`` can then rebuild it.
    if _is_stale_channel_snapshot(ticket_dir, snapshot_filename):
        out.append(
            f"SNAPSHOT_STALE_CHANNEL: {ticket_id}/{snapshot_filename} — compiled_state "
            "predates creation_channel; rebuild from the retained CREATE to persist it"
        )
    source_uuids = _data.get("source_event_uuids", [])
    out.extend(_missing_sources_finding(ticket_dir, ticket_id, snapshot_filename, source_uuids))
    if not source_uuids:
        return out

    event_files = _active_event_map(ticket_dir, snapshot_filename)

    source_uuid_set = set(source_uuids)
    for u in source_uuids:
        if u in event_files:
            out.append(
                f"SNAPSHOT_INCONSISTENT: {ticket_id}/{snapshot_filename} — source UUID "
                f"{u} still exists as {event_files[u][0]}"
            )
    for file_uuid, (name, etype) in event_files.items():
        # Scanner and compaction share ``is_snapshot_orphan``. Unknown types and snapshots are
        # excluded because compaction does not fold them as ordinary sources.
        if _is_snapshot_orphan(name, etype, file_uuid, snapshot_filename, source_uuid_set):
            out.append(
                f"ORPHAN_EVENT: {ticket_id}/{name} — pre-snapshot event not "
                "captured in source_event_uuids"
            )
    return out
