"""Shared Python ticket-ID resolver.

Mirrors ``ticket-lib.sh::resolve_ticket_id`` so Python
CLIs accept the same ID forms (full 16-hex, 8-hex short, alias, jira_key,
unique prefix >= 4 chars) as the bash dispatcher does.

Alias/jira_key lookup is done IN-PROCESS (Tier E E6.5a — replacing the
``ticket-alias-resolve.py`` subprocess): the alias-mode scan reads each ticket's
CREATE event (and the latest SNAPSHOT, for compacted tickets) and matches a
stored ``data.alias``/``data.jira_key`` or a backfilled ``compute_alias`` — the
same single-source alias helper (``rebar._alias``) the create path uses, so
stored-at-create and backfilled-at-resolve aliases stay in lock-step.
"""

from __future__ import annotations

import json
import os
import re
import sys

from rebar._alias import compute_alias

_FULL_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$")
_SHORT_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")


def _scan_alias_jira(target: str, tracker_dir: str) -> tuple[list[str], list[str]] | None:
    """In-process port of ``ticket-alias-resolve.py`` (alias mode).

    Returns ``(jira_matches, alias_matches)`` (ticket-dir names), or ``None`` on a
    hard failure listing the tracker (mirrors the helper's exit 1 — a hard failure
    must not masquerade as "no match"). Per-ticket I/O errors skip that ticket.
    """
    try:
        entries = sorted(os.listdir(tracker_dir))
    except OSError as exc:
        print(f"Error: cannot list {tracker_dir!r}: {exc}", file=sys.stderr)
        return None

    jira_matches: list[str] = []
    alias_matches: list[str] = []
    for name in entries:
        if name.startswith("."):
            continue
        ticket_dir = os.path.join(tracker_dir, name)
        if not os.path.isdir(ticket_dir):
            continue
        # First CREATE (lexically earliest) + latest non-PRECONDITIONS SNAPSHOT
        # (compacted tickets fold the CREATE into a SNAPSHOT compiled_state).
        create_path = None
        snapshot_path = None
        try:
            for fname in sorted(os.listdir(ticket_dir)):
                if fname.endswith("-CREATE.json") and create_path is None:
                    create_path = os.path.join(ticket_dir, fname)
                elif fname.endswith("-SNAPSHOT.json") and not fname.endswith(
                    "-PRECONDITIONS-SNAPSHOT.json"
                ):
                    snapshot_path = os.path.join(ticket_dir, fname)
        except OSError:
            continue
        stored_alias = ""
        jira_key = ""
        if create_path:
            try:
                with open(create_path, encoding="utf-8") as f:
                    data = json.load(f).get("data", {}) or {}
                stored_alias = data.get("alias") or ""
                jira_key = data.get("jira_key") or ""
            except (OSError, json.JSONDecodeError):
                pass
        # SNAPSHOT compiled_state is authoritative for compacted tickets; fill only
        # the missing values, BEFORE the compute_alias backfill (wordlist drift).
        if snapshot_path and (not stored_alias or not jira_key):
            try:
                with open(snapshot_path, encoding="utf-8") as f:
                    snap_state = (json.load(f).get("data", {}) or {}).get(
                        "compiled_state", {}
                    ) or {}
                if not stored_alias:
                    stored_alias = snap_state.get("alias") or ""
                if not jira_key:
                    jira_key = snap_state.get("jira_key") or ""
            except (OSError, json.JSONDecodeError):
                pass
        if jira_key and jira_key == target:
            jira_matches.append(name)
            continue
        effective_alias = stored_alias or compute_alias(name) or ""
        if effective_alias and effective_alias == target:
            alias_matches.append(name)
    return jira_matches, alias_matches


def resolve_ticket_id(ticket_id: str, tracker_dir: str) -> str | None:
    """Return the canonical ticket directory name for ``ticket_id``, or None.

    Ambiguous matches and tracker-listing failures are surfaced via stderr to
    match the bash side's diagnostics; the function still returns None so callers
    can pick their own error vs graceful path.
    """
    # Fast path: if the input is already an exact, canonical ticket directory
    # name, use it directly. This avoids a per-call alias-resolver subprocess
    # for inputs that are already resolved (e.g. dependency-graph BFS over known
    # directory names), and is unambiguous — a directory matching the input name
    # exactly is that ticket.
    if os.path.isdir(os.path.join(tracker_dir, ticket_id)):
        return ticket_id

    if _FULL_ID_RE.match(ticket_id):
        return ticket_id if os.path.isdir(os.path.join(tracker_dir, ticket_id)) else None

    if _SHORT_ID_RE.match(ticket_id):
        if os.path.isdir(os.path.join(tracker_dir, ticket_id)):
            return ticket_id
        try:
            matches = [
                n
                for n in os.listdir(tracker_dir)
                if not n.startswith(".")
                and n[:9] == ticket_id
                and os.path.isdir(os.path.join(tracker_dir, n))
            ]
        except OSError:
            return None
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(
                f"Error: Ambiguous 8-hex ID '{ticket_id}' matches: {' '.join(sorted(matches))}",
                file=sys.stderr,
            )
        return None

    scanned = _scan_alias_jira(ticket_id, tracker_dir)
    if scanned is not None:
        jira_matches, alias_matches = scanned
        if len(jira_matches) == 1:
            return jira_matches[0]
        if len(jira_matches) > 1:
            print(
                f"Error: Ambiguous jira_key '{ticket_id}' matches multiple tickets: "
                f"{' '.join(sorted(jira_matches))}",
                file=sys.stderr,
            )
            return None
        if len(alias_matches) == 1:
            return alias_matches[0]
        if len(alias_matches) > 1:
            print(
                f"Error: Ambiguous alias '{ticket_id}' matches multiple tickets: "
                f"{' '.join(sorted(alias_matches))}",
                file=sys.stderr,
            )
            return None

    if len(ticket_id) >= 4:
        try:
            matches = [
                n
                for n in os.listdir(tracker_dir)
                if not n.startswith(".")
                and n.startswith(ticket_id)
                and os.path.isdir(os.path.join(tracker_dir, n))
            ]
        except OSError:
            return None
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(
                f"Error: Ambiguous prefix '{ticket_id}' matches multiple tickets: "
                f"{' '.join(sorted(matches))}",
                file=sys.stderr,
            )

    return None
