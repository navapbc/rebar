"""Ticket state helpers: initial state factory and error-state builder."""

from __future__ import annotations


def make_initial_state() -> dict:
    """Return a fresh empty ticket state dict with all standard schema fields."""
    return {
        "ticket_id": None,
        "ticket_type": None,
        "title": None,
        "status": "open",
        "author": None,
        "created_at": None,
        "env_id": None,
        "parent_id": None,
        "priority": None,
        "assignee": None,
        "alias": None,
        "description": "",
        "tags": [],
        "comments": [],
        "deps": [],
        "bridge_alerts": [],
        "reverts": [],
        "file_impact": [],
        "verify_commands": [],
        "signature": None,
        "preconditions_summary": {"status": "pre-manifest"},
        "parent_status_uuid": "",
    }


def make_error_dict(ticket_id: str, status: str, error: str) -> dict:
    """Build an error-state dict with all standard schema fields (d145-e1a9).

    Ensures consumers iterating ticket_type/title never crash on missing keys,
    regardless of which error path produced the dict. Built from
    :func:`make_initial_state` so the two share ONE field list (previously two
    near-identical literals — the error dict had drifted to OMIT
    ``preconditions_summary``; basing it on the canonical schema fixes that and
    guarantees the key sets stay in lock-step).
    """
    state = make_initial_state()
    state.update(
        {
            "ticket_id": ticket_id,
            "title": f"[{status}] {error} for {ticket_id}",
            "status": status,
            "error": error,
        }
    )
    return state
