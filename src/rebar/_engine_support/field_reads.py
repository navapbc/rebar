"""Library field reads: a ticket's ``file_impact`` and ``verify_commands`` arrays.

Both resolve an id and read one array off the reduced ticket, reusing the single reducer
(bug f026) and the shared resolver. The CLI reads them through ``rebar show <id>
--field=<name>``; the ``get-file-impact`` / ``get-verify-commands`` verbs were removed.
"""

from __future__ import annotations

import os

from rebar._engine_support.reads import show_state
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.reducer import reduce_ticket


# ── library-facing pure helpers ───────────────────────────────────────────────
def file_impact(ticket_id: str, tracker: str) -> list:
    """File-impact array for ``ticket_id``; ``[]`` on a resolve/dir miss."""
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        return []
    path = os.path.join(tracker, resolved)
    if not os.path.isdir(path):
        return []
    state = reduce_ticket(path)
    return (state or {}).get("file_impact") or []


def verify_commands(ticket_id: str, tracker: str) -> list:
    """Verify-commands array for ``ticket_id``; raises :class:`ReadError` on a miss."""
    state = show_state(ticket_id, tracker)
    return state.get("verify_commands") or []
