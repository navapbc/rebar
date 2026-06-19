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
from rebar._engine_support.output import OutputFormatError, parse_output
from rebar.reducer import reduce_ticket

_STRUCTURED_KINDS = {
    "corrupt",
    "corrupt_create",
    "missing_create",
    "snapshot_inconsistent",
    "orphan_event",
}


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


def _scan(tracker: str, no_mutate: bool) -> tuple[list[str], int]:
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
        for snap_name in sorted(
            n
            for n in os.listdir(ticket_dir)
            if n.endswith("-SNAPSHOT.json") and not n.startswith(".")
        ):
            lines.extend(c4 := _check_snapshot(ticket_dir, ticket_id, snap_name))
            issue_count += len(c4)

    # ── Check 4.5: push-pending (informational; no issue_count) ──────────────
    pp = _push_pending(tracker)
    if pp:
        lines.append(pp)

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

    event_files: dict[str, str] = {}
    for name in sorted(os.listdir(ticket_dir)):
        if not name.endswith(".json") or name.startswith("."):
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
        event_files[type_split[0]] = name

    source_uuid_set = set(source_uuids)
    for u in source_uuids:
        if u in event_files:
            out.append(
                f"SNAPSHOT_INCONSISTENT: {ticket_id}/{snapshot_filename} — source UUID "
                f"{u} still exists as {event_files[u]}"
            )
    for file_uuid, name in event_files.items():
        if name < snapshot_filename and "-SNAPSHOT.json" not in name:
            if file_uuid not in source_uuid_set:
                out.append(
                    f"ORPHAN_EVENT: {ticket_id}/{name} — pre-snapshot event not "
                    "captured in source_event_uuids"
                )
    return out


def _push_pending(tracker: str) -> str | None:
    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", tracker, *args], capture_output=True, text=True)

    if _git("remote", "get-url", "origin").returncode != 0:
        return None
    if _git("rev-parse", "--verify", "origin/tickets").returncode != 0:
        return None
    cp = _git("rev-list", "origin/tickets..HEAD", "--count")
    try:
        ahead = int((cp.stdout or "0").strip() or "0")
    except ValueError:
        ahead = 0
    if ahead > 0:
        return (
            f"PUSH_PENDING: local tickets branch is ahead of origin/tickets by {ahead} "
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


def fsck_cli(argv: list[str], *, repo_root=None, no_mutate: bool = False) -> int:
    try:
        fmt, _rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        if fmt == "json":
            sys.stdout.write(_transform_json("") + "\n")
            return 1
        sys.stderr.write(
            "Error: ticket system not initialized (.tickets-tracker/ not found).\n"
            "Run 'ticket init' first.\n"
        )
        return 1

    # ``no_mutate`` is passed by the caller (the library's read-only fsck surface),
    # not read from the environment: read paths (list/show via rebar.fsck(report_only=
    # True)) pass no_mutate=True so they never delete the stale lock; the CLI `fsck`
    # always mutates (default False).
    lines, issue_count = _scan(tracker, no_mutate)
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
