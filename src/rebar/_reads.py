"""Library/MCP facade over the single-source read implementation (story 23d2-e0f3).

There is now ONE read implementation — ``ticket_reads`` in the engine package
(``src/rebar/_engine/ticket_reads.py``) — shared by the CLI dispatcher (via
``ticket-reads.py``) and by this module (library + MCP). This file is a thin
facade: it resolves the tracker dir, applies the uniform read-freshness policy,
calls the shared ``ticket_reads.*_state`` helpers, and maps their ``ReadError``
onto ``RebarError`` so the library's exit-1 contract is unchanged.

Read-freshness (uniform across CLI / library / MCP): before each read we run a
best-effort, throttled (<=1/min) ``git fetch origin tickets`` + reconverge via
the shared ``ticket-sync.sh`` helper — so MCP (the primary agent surface) is no
longer the stalest interface. Opt out with ``REBAR_NO_SYNC=1`` (the CLI also
accepts ``--no-sync``). Reuses the SAME throttle marker the dispatcher uses, so
CLI and in-process reads never double-fetch.
"""

from __future__ import annotations

import os
from typing import Any

from rebar import config

# Importing rebar._native ensures the bundled engine dir is on sys.path so the
# native packages below resolve (idempotent; mirrors the library's native reads).
import rebar._native  # noqa: F401

import ticket_reads  # noqa: E402  (resolved via rebar._native's sys.path insert)


def _tracker(repo_root: str | os.PathLike[str] | None) -> str:
    """Tracker dir, honoring TICKETS_TRACKER_DIR then repo-root (matches the
    shared resolver's repo_root-based contract)."""
    return str(config.tracker_dir(repo_root))


def _fresh(tracker: str) -> None:
    """Apply the uniform read-freshness policy (best-effort, throttled)."""
    ticket_reads.ensure_fresh(tracker)


def _rebar_error(message: str):
    # Late import avoids a circular import at module load (rebar.__init__ imports
    # this module). Mirrors the subprocess path's RebarError on a nonzero exit.
    from rebar import RebarError

    return RebarError(message, returncode=1, stderr=message)


def show_ticket(ticket_id: str, *, repo_root=None) -> dict:
    """Compiled ticket state (alias/short-id aware). A missing/unresolvable id, a
    ticket that fails to reduce, or one with no CREATE/SNAPSHOT raises
    ``RebarError`` (the subprocess path's exit-1 contract)."""
    tracker = _tracker(repo_root)
    _fresh(tracker)
    try:
        return ticket_reads.show_state(ticket_id, tracker)
    except ticket_reads.ReadError as exc:
        raise _rebar_error(f"rebar show failed (exit 1): {exc.message}") from None


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
    _fresh(tracker)
    return ticket_reads.list_states(
        tracker,
        status=status or "",
        ticket_type=ticket_type or "",
        priority="" if priority is None else str(priority),
        parent=parent or "",
        has_tag=has_tag or "",
        without_tag=without_tag or "",
        include_archived=include_archived,
    )


def deps(ticket_id: str, *, repo_root=None) -> dict:
    """Dependency graph (archived target or unknown id raises ``RebarError``)."""
    tracker = _tracker(repo_root)
    _fresh(tracker)
    try:
        return ticket_reads.deps_state(ticket_id, tracker, include_archived=False)
    except ticket_reads.ReadError as exc:
        raise _rebar_error(f"rebar deps failed (exit 1): {exc.message}") from None


def ready(*, repo_root=None) -> Any:
    """Tickets ready to work (no epic filter from the library entrypoint)."""
    tracker = _tracker(repo_root)
    _fresh(tracker)
    return ticket_reads.ready_states(tracker)


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
    _fresh(tracker)
    return ticket_reads.search_state(
        tracker,
        query,
        status=status,
        ticket_type=ticket_type,
        has_tag=has_tag,
        include_archived=include_archived,
    )
