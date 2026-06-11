"""In-process access to the engine's native Python read API.

The reducer is now a real subpackage (``rebar.reducer``), so the library imports
it directly — no ``sys.path`` insertion of the engine dir, and therefore no
generic top-level names (``ticket_reducer`` etc.) leaking onto the library import
path (ticket ``fare-rant-clasp``). This module just re-exports the stable
read-path entry points.
"""

from __future__ import annotations

from rebar._engine import engine_dir
from rebar.reducer import (
    apply_ticket_filters,
    find_inbound_relationships,
    reduce_all_tickets,
    reduce_ticket,
    to_llm,
)

__all__ = [
    "reduce_all_tickets",
    "reduce_ticket",
    "to_llm",
    "find_inbound_relationships",
    "apply_ticket_filters",
    "engine_dir",
]
