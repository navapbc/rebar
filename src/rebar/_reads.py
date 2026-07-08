"""Library/MCP facade over the single-source read implementation (story 23d2-e0f3).

There is now ONE read implementation — the ``rebar._engine_support.reads``
subpackage (imported below as ``ticket_reads``) — shared by the CLI dispatcher
and by this module (library + MCP). This file is a thin facade: it resolves the
tracker dir, applies the uniform read-freshness policy, calls the shared
``reads.*_state`` helpers, and maps their ``ReadError`` onto ``RebarError`` so the
library's exit-1 contract is unchanged.

Read-freshness (uniform across CLI / library / MCP): before each read we run a
best-effort, throttled (<=1/min) ``git fetch origin tickets`` + reconverge via
the shared in-process freshness helper (``reads.ensure_fresh``) — so MCP (the
primary agent surface) is no longer the stalest interface. Opt out with
``REBAR_SYNC_PULL=off`` (permanent alias ``REBAR_NO_SYNC=1``; the CLI also accepts
``--no-pull``). Reuses the SAME throttle marker the
dispatcher uses, so CLI and in-process reads never double-fetch.
"""

from __future__ import annotations

import os
from typing import Any

from rebar import config

# The single-source read implementation is a real subpackage now
# (``rebar._engine_support.reads``); no sys.path manipulation needed.
from rebar._engine_support import reads as ticket_reads


def _tracker(repo_root: str | os.PathLike[str] | None) -> str:
    """Tracker dir, honoring REBAR_TRACKER_DIR
    then repo-root (matches the shared resolver's repo_root-based contract)."""
    return str(config.tracker_dir(repo_root))


def _fresh(tracker: str) -> None:
    """Apply the uniform read-freshness policy (best-effort, throttled)."""
    ticket_reads.ensure_fresh(tracker)


def _rebar_error(message: str):
    # Source RebarError from the stdlib-only leaf (item 9.3): keeps this read
    # facade OUT of the import SCC (importing it from ``rebar`` was the last
    # ``_reads -> rebar`` back-edge). Mirrors the subprocess path's RebarError on
    # a nonzero exit. Now safe at module scope, but kept local to match the file's
    # lazy-import style and avoid churn.
    from rebar._errors import RebarError

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


def list_by_query(query: ticket_reads.TicketQuery, *, repo_root=None) -> list[dict]:
    """Single query-accepting read entry: resolve the tracker, apply the uniform
    read-freshness policy, and run the filter core (``list_states``).

    Every read boundary that already speaks :class:`TicketQuery` funnels through
    here (the public library facade ``rebar.list_tickets`` and the scalar
    :func:`list_tickets` shim below both build a query via
    :meth:`TicketQuery.from_library` and hand it to this one entry), so the
    tracker-resolution + freshness + ``list_states`` plumbing lives ONCE."""
    tracker = _tracker(repo_root)
    _fresh(tracker)
    return ticket_reads.list_states(tracker, query)


def list_tickets(
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    priority: int | str | None = None,
    parent: str | None = None,
    has_tag: str | None = None,
    without_tag: str | None = None,
    include_archived: bool = False,
    exclude_deleted: bool = False,
    min_children: int | None = None,
    blocking_state: str = "",
    with_children_count: bool = False,
    sort: str | None = None,
    include_body: bool = True,
    repo_root=None,
) -> list[dict]:
    # Scalar shim kept for internal callers that pass a small kwarg subset (e.g.
    # plan_review attest/orchestrator call ``list_tickets(parent=…)``). Build the
    # TicketQuery via the single normalizer (``from_library`` owns the None→"" /
    # priority-cast), then funnel through the one query-accepting read entry — the
    # 14-field shape is never respelled per read layer.
    query = ticket_reads.TicketQuery.from_library(
        status=status,
        ticket_type=ticket_type,
        priority=priority,
        parent=parent,
        has_tag=has_tag,
        without_tag=without_tag,
        include_archived=include_archived,
        exclude_deleted=exclude_deleted,
        min_children=min_children,
        blocking_state=blocking_state,
        with_children_count=with_children_count,
        sort=sort,
        include_body=include_body,
    )
    return list_by_query(query, repo_root=repo_root)


def deps(ticket_id: str, *, repo_root=None) -> dict:
    """Dependency graph (archived target or unknown id raises ``RebarError``)."""
    tracker = _tracker(repo_root)
    _fresh(tracker)
    try:
        return ticket_reads.deps_state(ticket_id, tracker, include_archived=False)
    except ticket_reads.ReadError as exc:
        raise _rebar_error(f"rebar deps failed (exit 1): {exc.message}") from None


def ready(*, sort: str | None = None, repo_root=None) -> Any:
    """Tickets ready to work (no epic filter from the library entrypoint)."""
    tracker = _tracker(repo_root)
    _fresh(tracker)
    return ticket_reads.ready_states(tracker, sort=sort or "")


def next_batch(epic_id: str, *, repo_root=None, limit: int = 0) -> dict:
    """Conflict-aware parallel batch under an epic (Tier C, in-process). A missing
    epic raises ``RebarError`` (the subprocess path's exit-1 contract)."""
    from rebar._engine_support import next_batch as _nb

    tracker = _tracker(repo_root)
    _fresh(tracker)
    try:
        return _nb.next_batch_state(tracker, epic_id, limit=limit)
    except ticket_reads.ReadError as exc:
        raise _rebar_error(f"next-batch failed (exit 1): {exc.message}") from None


def search(
    query: str,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    has_tag: str | None = None,
    include_archived: bool = False,
    sort: str | None = None,
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
        sort=sort or "",
    )


def recent_session_logs(*, limit: int = 5, repo_root=None) -> list[dict]:
    """The ``limit`` newest ``session_log`` tickets, newest first (by created_at)."""
    tracker = _tracker(repo_root)
    _fresh(tracker)
    return ticket_reads.recent_session_logs_state(tracker, limit=limit)
