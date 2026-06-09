"""In-process implementations of the library read functions (#2 Step 2).

These mirror the bash read shims — ``ticket-show.sh``, ``ticket-list.sh``,
``ticket-graph.py`` (deps), ``ticket-ready.py``, ``ticket-search.py`` — but run
in-process via the native ``ticket_reducer`` / ``ticket_graph`` packages, with no
``bash`` + ``python3`` subprocess per call. ``rebar.__init__``'s public read
functions delegate straight here (there is no subprocess fallback).

Each function returns the SAME Python object the bash dispatcher yields after its
JSON round-trip, so callers (and the MCP server, which delegates to these) see no
change. Parity against the bash engine is pinned by
``tests/interfaces/test_native_read_parity.py``.

ONE deliberate behavior difference vs. the subprocess path: in-process reads do
NOT run the dispatcher's ``_ensure_initialized`` step, so they perform NO implicit
``git fetch origin tickets`` — they are pure local replay. A read therefore
reflects the local ``tickets`` branch only; pull/fetch explicitly (or use a write,
which still syncs) to observe collaborators' pushes. See docs/architecture.md.
"""

from __future__ import annotations

import os
from typing import Any

from rebar import config

# Importing rebar._native ensures the bundled engine dir is on sys.path so the
# native packages below resolve (idempotent; mirrors the library's native reads).
import rebar._native  # noqa: F401

from ticket_reducer import (  # noqa: E402
    apply_ticket_filters,
    reduce_all_tickets,
    reduce_ticket,
    search_states,
)
from ticket_reducer._present import public_state  # noqa: E402
from ticket_graph._graph import build_dep_graph  # noqa: E402
from ticket_graph._ready import find_ready_tickets  # noqa: E402
from ticket_resolver import resolve_ticket_id  # noqa: E402


def _tracker(repo_root: str | os.PathLike[str] | None) -> str:
    """Tracker dir, honoring TICKETS_TRACKER_DIR then repo-root (matches the
    shims' resolution for the library's repo_root-based contract)."""
    return str(config.tracker_dir(repo_root))


def _rebar_error(message: str):
    # Late import avoids a circular import at module load (rebar.__init__ imports
    # this module). Mirrors the subprocess path's RebarError on a nonzero exit.
    from rebar import RebarError

    return RebarError(message, returncode=1, stderr=message)


def show_ticket(ticket_id: str, *, repo_root=None) -> dict:
    """Compiled ticket state (alias/short-id aware) — mirrors the library's
    ``ticket_show``: ``public_state(reduce_ticket(...))``. A missing/unresolvable
    id, a ticket that fails to reduce, or one with no CREATE/SNAPSHOT (empty
    ``ticket_type``) raises ``RebarError`` (the subprocess path's exit-1
    contract)."""
    tracker = _tracker(repo_root)
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise _rebar_error(f"rebar show failed (exit 1): Ticket '{ticket_id}' not found")
    state = reduce_ticket(os.path.join(tracker, resolved))
    if not state:
        raise _rebar_error(f'rebar show failed (exit 1): failed to reduce ticket "{resolved}"')
    state = public_state(state)
    if not state.get("ticket_type"):
        raise _rebar_error(
            f'rebar show failed (exit 1): ticket "{resolved}" has no CREATE or SNAPSHOT event'
        )
    return state


def list_tickets(
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    priority: int | str | None = None,
    parent: str | None = None,
    has_tag: str | None = None,
    without_tag: str | None = None,
    include_archived: bool = False,
    repo_root=None,
) -> list[dict]:
    tracker = _tracker(repo_root)
    parent_filter = parent or ""
    if parent_filter:
        parent_filter = resolve_ticket_id(parent_filter, tracker) or parent_filter
    results = reduce_all_tickets(
        tracker, exclude_archived=not include_archived, exclude_deleted=False
    )
    results = apply_ticket_filters(
        results,
        type_filter=ticket_type or "",
        status_filter=status or "",
        parent_filter=parent_filter,
        tag_filter=has_tag or "",
        priority_filter="" if priority is None else str(priority),
        without_tag_filter=without_tag or "",
    )
    return [public_state(t) for t in results]


def deps(ticket_id: str, *, repo_root=None) -> dict:
    """Dependency graph (mirrors ``ticket-graph.py`` deps mode, include_archived
    off — the library's ``deps`` passes no --include-archived). An archived target
    or unknown id raises ``RebarError``."""
    tracker = _tracker(repo_root)
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise _rebar_error(f"rebar deps failed (exit 1): ticket '{ticket_id}' does not exist")
    ticket_dir = os.path.join(tracker, resolved)
    try:
        target_state = reduce_ticket(ticket_dir)
    except Exception:
        target_state = None
    if isinstance(target_state, dict) and target_state.get("archived") is True:
        raise _rebar_error(
            f"rebar deps failed (exit 1): ticket '{resolved}' is archived. "
            "Use --include-archived to include archived tickets."
        )
    return build_dep_graph(resolved, tracker, exclude_archived=True)


def ready(*, repo_root=None) -> Any:
    """Tickets ready to work (mirrors ``ticket-ready.py --json``: no epic filter
    from the library entrypoint)."""
    tracker = _tracker(repo_root)
    return [public_state(s) for s in find_ready_tickets(tracker)]


def search(
    query: str,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    has_tag: str | None = None,
    include_archived: bool = False,
    repo_root=None,
) -> list:
    tracker = _tracker(repo_root)
    states = reduce_all_tickets(
        tracker, exclude_archived=not include_archived, exclude_deleted=True
    )
    results = search_states(
        states, query, status=status, ticket_type=ticket_type, has_tag=has_tag
    )
    return [public_state(t) for t in results]
