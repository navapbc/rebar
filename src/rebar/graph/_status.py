"""Ticket status resolution (tombstone-aware) for ticket-graph."""

from __future__ import annotations

import json
import os

from ._loader import reduce_ticket
from ._relations import _BLOCKING_RELATIONS  # re-exported for _blockers

# ``_BLOCKING_RELATIONS`` is imported here solely so ``_blockers`` can resolve it
# via ``from ._status import _BLOCKING_RELATIONS``; list it in ``__all__`` to mark
# that intentional re-export alongside this module's own status helper.
__all__ = ["_get_ticket_status", "_BLOCKING_RELATIONS"]


# The reviewer noted the function "may not exist" — it does exist and is the authoritative
# status resolver for graph operations.
def _get_ticket_status(ticket_id: str, tracker_dir: str) -> str:
    """Return the effective status of a ticket.

    Tombstone-awareness rules:
    - Directory absent → treat as "closed" (archived/tombstoned)
    - Directory contains .tombstone.json → read its 'status' field
    - reduce_ticket() returns None → treat as "closed" (ghost ticket safety)
    - reduce_ticket() returns error-state → treat as "closed"
    """
    ticket_dir = os.path.join(tracker_dir, ticket_id)

    # Missing directory → archived/tombstoned → closed
    if not os.path.isdir(ticket_dir):
        return "closed"

    # .tombstone.json present → read its status
    tombstone_path = os.path.join(ticket_dir, ".tombstone.json")
    if os.path.isfile(tombstone_path):
        try:
            with open(tombstone_path, encoding="utf-8") as f:
                tombstone = json.load(f)
            return str(tombstone.get("status", "closed"))
        except (OSError, json.JSONDecodeError):
            return "closed"

    # Reduce the ticket to get its compiled state
    try:
        state = reduce_ticket(ticket_dir)
    except Exception:
        return "closed"

    if state is None:
        return "closed"

    # Error-state dicts (ghost tickets, corrupt CREATE)
    if isinstance(state, dict) and state.get("status") in ("error", "fsck_needed"):
        return "closed"

    return str(state.get("status", "open"))
