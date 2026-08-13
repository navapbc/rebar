"""Access point for the reducer from the graph package.

Historically this loaded ``ticket-reducer.py`` via ``spec_from_file_location``
to cope with the hyphenated engine filename. Now that the reducer is a real
subpackage (``rebar.reducer``) it imports directly. ``reducer`` stays a module
object exposing ``reduce_ticket`` / ``reduce_all_tickets`` so existing patch
points (``_loader_module.reducer.reduce_all_tickets``) are unchanged.
"""

from __future__ import annotations

from typing import Any

import rebar.reducer as reducer

reduce_ticket = reducer.reduce_ticket
reduce_all_tickets = reducer.reduce_all_tickets

# Statuses whose compiled state is not a usable graph node: a ghost/corrupt ticket
# carries no trustworthy deps, so it is dropped rather than reasoned about.
_UNUSABLE_STATUSES = ("error", "fsck_needed")


def load_indexed_states(tracker_dir: str) -> dict[str, Any]:
    """Load every graph-visible ticket state, indexed by ``ticket_id``.

    The canonical form of the load policy shared by ``_find_direct_blockers`` and
    ``_compute_dep_graph``, which each hand-rolled it verbatim: reduce the whole
    tracker (archived tickets INCLUDED — callers apply their own ``exclude_archived``
    filter downstream; session logs EXCLUDED — they are never graph nodes of another
    ticket), drop ``error``/``fsck_needed`` states, and index by id.

    ``reduce_all_tickets`` is reached through the module-level ``reducer`` attribute
    so the established ``_loader_module.reducer.reduce_all_tickets`` test patch point
    keeps working.

    NOT a universal loader: traversals needing raw per-ticket reduction with no
    status filtering (``_get_all_blocked_by``, ``check_cycle_at_level``) and the
    tombstone-aware loader in ``_unblock`` deliberately do NOT route through it,
    because their node-sets differ.
    """
    states: dict[str, Any] = {}
    for state in reducer.reduce_all_tickets(
        tracker_dir, exclude_archived=False, exclude_session_logs=True
    ):
        ticket_id = state.get("ticket_id", "")
        if ticket_id and state.get("status") not in _UNUSABLE_STATUSES:
            states[ticket_id] = state
    return states
