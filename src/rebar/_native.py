"""In-process access to the engine's native Python packages.

Importing this module ensures the bundled engine directory is on ``sys.path``
so the stdlib-only ``ticket_reducer`` / ``ticket_graph`` / ``dso_reconciler``
packages can be imported directly (no subprocess). It then re-exports the
stable read-path entry points.
"""

from __future__ import annotations

import sys

from rebar._engine import engine_dir

_eng = str(engine_dir())
if _eng not in sys.path:
    sys.path.insert(0, _eng)

# Native re-exports (read path — no subprocess).
from ticket_reducer import (  # noqa: E402
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
