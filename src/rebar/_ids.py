"""Shared ticket-ID resolution primitives (stdlib-only leaf).

The single resolution seam Python CLIs and the library use, so every surface
accepts the same ID forms: full 16-hex, 8-hex short, alias, Jira issue key
(e.g. ``REB-310``), and unique prefix >= 4 chars.

This is a **top-of-tree leaf**: it imports only stdlib + ``rebar._alias`` (itself
a stdlib-only leaf) and NOTHING from ``rebar.reducer`` / ``rebar._engine_support``
/ ``rebar._commands`` / ``rebar.llm``.  It therefore sits BELOW both the pure
event-replay layer (``rebar.reducer``) and the higher read layer
(``rebar._engine_support``), so both can depend on it downward without a package
cycle — the same pattern ``rebar._alias`` uses.  Historically this lived in
``rebar._engine_support.resolver``, which forced the reducer to reach UP into
``_engine_support`` via a function-local import (a layering inversion + import
cycle); moving the primitive here removes that back-edge.  ``rebar._engine_support
.resolver`` now re-exports these names, so its public surface is unchanged.

Alias lookup is done IN-PROCESS (Tier E E6.5a — replacing the
``ticket-alias-resolve.py`` subprocess): the alias scan reads each ticket's
CREATE event (and the latest SNAPSHOT, for compacted tickets) and matches a
stored ``data.alias`` or a backfilled ``compute_alias`` — the same single-source
alias helper (``rebar._alias``) the create path uses, so stored-at-create and
backfilled-at-resolve aliases stay in lock-step.

Jira-key lookup consults the reconciler's **binding store** reverse index
(``.tickets-tracker/.bridge_state/bindings.json`` → ``reverse: {jira_key →
local_id}``), which is the authoritative Jira↔rebar mapping. (Historically the
resolver scanned ``data.jira_key`` on CREATE/SNAPSHOT events, but that field is
never written — the live mapping is the binding store — so that path was dead and
has been replaced.)
"""

from __future__ import annotations

import json
import os
import re
import sys

from rebar._alias import compute_alias

_FULL_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$")
_SHORT_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")
# Jira issue key shape: project key (>=2 alnum, leading letter) + "-" + number,
# e.g. ``REB-310``. Disjoint from lowercase-hex full/short IDs and lowercase-word
# aliases, so a Jira-key match never collides with the other forms.
_JIRA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]+-[0-9]+$")


def _resolve_via_binding_store(target: str, tracker_dir: str) -> str | None:
    """Resolve a Jira issue key to its bound local ticket-dir name, or None.

    Consults the reconciler's binding store reverse index
    (``<tracker_dir>/.bridge_state/bindings.json`` → ``reverse: {jira_key →
    local_id}``) — the authoritative Jira↔rebar mapping. Best-effort: a missing or
    corrupt store, a non-dict ``reverse``, an unbound key, or a binding that points
    at a ticket dir that no longer exists all yield None (Jira resolution simply
    unavailable — this never raises). The lookup tries the key verbatim then
    upper-cased, since Jira project keys are canonically upper-case.
    """
    bindings_path = os.path.join(tracker_dir, ".bridge_state", "bindings.json")
    try:
        with open(bindings_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    reverse = data.get("reverse")
    if not isinstance(reverse, dict):
        return None
    local_id = reverse.get(target) or reverse.get(target.upper())
    if not isinstance(local_id, str) or not local_id:
        return None
    if os.path.isdir(os.path.join(tracker_dir, local_id)):
        return local_id
    return None


def _scan_alias(target: str, tracker_dir: str) -> list[str] | None:
    """Scan tickets for an alias matching ``target`` (in-process alias resolution).

    Returns the list of matching ticket-dir names, or ``None`` on a hard failure
    listing the tracker (a hard failure must not masquerade as "no match").
    Per-ticket I/O errors skip that ticket. Each ticket's effective alias is its
    stored ``data.alias`` (CREATE event, or the latest non-PRECONDITIONS SNAPSHOT
    ``compiled_state`` for compacted tickets) or a backfilled ``compute_alias`` —
    keeping stored-at-create and backfilled-at-resolve aliases in lock-step.
    """
    try:
        entries = sorted(os.listdir(tracker_dir))
    except OSError as exc:
        print(f"Error: cannot list {tracker_dir!r}: {exc}", file=sys.stderr)
        return None

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
        if create_path:
            try:
                with open(create_path, encoding="utf-8") as f:
                    data = json.load(f).get("data", {}) or {}
                stored_alias = data.get("alias") or ""
            except (OSError, json.JSONDecodeError):
                pass
        # SNAPSHOT compiled_state is authoritative for compacted tickets; fill the
        # missing alias BEFORE the compute_alias backfill (wordlist drift).
        if snapshot_path and not stored_alias:
            try:
                with open(snapshot_path, encoding="utf-8") as f:
                    snap_state = (json.load(f).get("data", {}) or {}).get(
                        "compiled_state", {}
                    ) or {}
                stored_alias = snap_state.get("alias") or ""
            except (OSError, json.JSONDecodeError):
                pass
        effective_alias = stored_alias or compute_alias(name) or ""
        if effective_alias and effective_alias == target:
            alias_matches.append(name)
    return alias_matches


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

    # Jira issue key (e.g. REB-310) → bound local ticket via the binding store.
    # Checked before the alias scan; the shapes are disjoint, so this only matches
    # genuine Jira keys.
    if _JIRA_KEY_RE.match(ticket_id):
        bound = _resolve_via_binding_store(ticket_id, tracker_dir)
        if bound is not None:
            return bound

    alias_matches = _scan_alias(ticket_id, tracker_dir)
    if alias_matches is not None:
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
