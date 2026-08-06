"""Tier B link/revert composer commands (docs/bash-migration.md §4).

Extracted from ``composer.py`` along its existing call-graph seam (the module-size
policy's 800-LOC cap): ``link_core``/``link_cli`` (LINK writes through the shared
graph) and ``revert_core``/``revert_cli`` (compensating REVERT events). Each keeps
the ``*_core`` (validation + event compose, shared by the library facade) /
``*_cli`` (arg + output-format parsing) split described in composer's module
docstring. ``composer`` re-exports these names, so callers and monkeypatch sites
that resolve them via ``rebar._commands.composer`` keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import sys

from rebar._commands._seam import (
    CommandError,
    append_event,
    require_id,
    require_not_ghost,
    tracker_dir,
)
from rebar._engine_support.resolver import resolve_ticket_id

logger = logging.getLogger(__name__)


def link_core(
    src_raw: str, tgt_raw: str, relation: str, *, repo_root=None, quiet: bool = False
) -> dict | None:
    """Resolve endpoints and add a LINK via the shared graph (mirrors ticket_link's
    non-dry-run path → ticket-graph.py --link → add_dependency).

    add_dependency owns relation validation, hierarchy promotion (+ the REDIRECT
    note), the redundant-link guard, cycle detection, and the LINK event write —
    the SAME function the bash path calls, so parity is structural. ``quiet``
    suppresses add_dependency's stdout/stderr (the library facade discards it, as
    the subprocess path did); the CLI lets it through. Raises :class:`CommandError`.
    """
    import contextlib
    import io

    from rebar.graph._links import CyclicDependencyError, add_dependency

    tracker = tracker_dir(repo_root)
    src_id = resolve_ticket_id(src_raw, str(tracker))
    if src_id is None:
        raise CommandError(f"Error: ticket '{src_raw}' does not exist")
    tgt_id = resolve_ticket_id(tgt_raw, str(tracker))
    if tgt_id is None:
        raise CommandError(f"Error: ticket '{tgt_raw}' does not exist")
    sink = io.StringIO()
    try:
        if quiet:
            # The sink stays: rebar-mcp speaks MCP-over-stdio, so a stray print inside
            # a tool call would corrupt the JSON-RPC stream. The record travels back as
            # a RETURN VALUE instead.
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                return add_dependency(src_id, tgt_id, str(tracker), relation)
        return add_dependency(src_id, tgt_id, str(tracker), relation)
    except (CyclicDependencyError, ValueError) as exc:
        raise CommandError(f"Error: {exc}") from None


def _link_dry_run(src_raw: str, tgt_raw: str, relation: str, *, repo_root=None) -> int:
    """In-process ``link --dry-run`` preview (Tier E E6.5a — replaces the
    ticket-link.sh subprocess). Resolves endpoints, asks the shared hierarchy
    resolver what WOULD happen, and prints the byte-identical ``[DRY RUN]`` line
    without writing any event. Missing tickets error like the bash _check_ticket_
    exists; a resolver failure falls back to the plain "Would create" preview."""
    from rebar.graph._hierarchy import resolve_hierarchy_link

    tracker = str(tracker_dir(repo_root))
    src_id = resolve_ticket_id(src_raw, tracker)
    if src_id is None:
        print(f"Error: ticket '{src_raw}' does not exist", file=sys.stderr)
        return 1
    tgt_id = resolve_ticket_id(tgt_raw, tracker)
    if tgt_id is None:
        print(f"Error: ticket '{tgt_raw}' does not exist", file=sys.stderr)
        return 1
    try:
        res = resolve_hierarchy_link(src_id, tgt_id, tracker, relation)
    except Exception:  # noqa: BLE001 — resolver unavailable → plain preview (bash parity)
        print(f"[DRY RUN] Would create: {src_id} {relation} {tgt_id} (no event written)")
        return 0
    if res.get("is_redundant"):
        print(
            f"[DRY RUN] Would reject: {src_id} {relation} {tgt_id} — "
            "redundant link (ancestor-descendant) (no event written)"
        )
    elif res.get("was_redirected"):
        rs = res.get("resolved_source", src_id)
        rt = res.get("resolved_target", tgt_id)
        print(f"[DRY RUN] Would promote: {rs} {relation} {rt} (no event written)")
    else:
        print(f"[DRY RUN] Would create: {src_id} {relation} {tgt_id} (no event written)")
    return 0


def link_cli(argv: list[str], *, repo_root=None) -> int:
    """Dispatcher Python route for ``link``: parse --dry-run, resolve, delegate."""
    dry_run = "--dry-run" in argv
    rest = [a for a in argv if a != "--dry-run"]
    if len(rest) < 3:
        print("Usage: ticket link <id1> <id2> <relation>", file=sys.stderr)
        return 1
    src_raw, tgt_raw, relation = rest[0], rest[1], rest[2]

    if dry_run:
        return _link_dry_run(src_raw, tgt_raw, relation, repo_root=repo_root)

    try:
        link_core(src_raw, tgt_raw, relation, repo_root=repo_root)
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    return 0


_REVERT_USAGE = (
    "Usage: ticket revert <ticket_id> <target_uuid> [--reason=<text>]\n"
    "  ticket_id:   ticket directory name\n"
    "  target_uuid: UUID of the event to revert\n"
    "  --reason=    optional reason text"
)


def revert_core(ticket_id: str, target_uuid: str, reason: str = "", *, repo_root=None) -> str:
    """Append a REVERT event targeting an existing event (mirrors ticket-revert.sh).

    Resolves the id, ghost-checks, finds the target event by UUID, rejects
    REVERT-of-REVERT, then appends the REVERT event through the seam. Reverting an
    ARCHIVED event also clears the ``.archived`` marker (the reducer un-archives).
    Returns the resolved ticket id. Raises :class:`CommandError`.
    """
    from rebar.reducer.marker import remove_marker

    tracker = tracker_dir(repo_root)
    if not (tracker / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")
    resolved = require_id(ticket_id, tracker)
    ticket_dir = tracker / resolved
    require_not_ghost(resolved, tracker)

    target_type = None
    for entry in sorted(os.listdir(ticket_dir)):
        if entry.startswith(".") or not entry.endswith(".json"):
            continue
        try:
            with open(ticket_dir / entry, encoding="utf-8") as fh:
                ev = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if ev.get("uuid") == target_uuid:
            target_type = ev.get("event_type", "")
            break
    if target_type is None:
        raise CommandError(
            f"Error: event not found: no event with UUID '{target_uuid}' in ticket '{resolved}'"
        )
    if target_type == "REVERT":
        raise CommandError(
            f"Error: cannot revert a REVERT event (target UUID '{target_uuid}' is a REVERT)"
        )

    append_event(
        resolved,
        "REVERT",
        {"target_event_uuid": target_uuid, "target_event_type": target_type, "reason": reason},
        tracker,
        repo_root=repo_root,
    )
    if target_type == "ARCHIVED":
        try:
            remove_marker(str(ticket_dir))
        except Exception:
            logger.warning(
                "could not clear .archived marker for %s after REVERT; continuing",
                resolved,
                exc_info=True,
            )
    return resolved


def revert_cli(argv: list[str], *, repo_root=None) -> int:
    """Dispatcher Python route for ``revert``: parse args, print the confirmation."""
    if len(argv) < 2:
        print(_REVERT_USAGE, file=sys.stderr)
        return 1
    ticket_id, target_uuid = argv[0], argv[1]
    reason = ""
    for arg in argv[2:]:
        if arg.startswith("--reason="):
            reason = arg[len("--reason=") :]
        else:
            print(f"Error: unknown argument '{arg}'", file=sys.stderr)
            print(_REVERT_USAGE, file=sys.stderr)
            return 1
    try:
        resolved = revert_core(ticket_id, target_uuid, reason, repo_root=repo_root)
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    print(f"Reverted event '{target_uuid}' on ticket '{resolved}'")
    return 0
