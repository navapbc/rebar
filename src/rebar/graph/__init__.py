"""rebar.graph — dependency-graph build, cycle detection, hierarchy promotion,
and link writes.

Re-exports the flat public API (build/cycle/hierarchy/link helpers) that the
engine's ``ticket-graph.py`` CLI wrapper used to expose, so the library, CLI, and
tests share ONE surface (Tier E E7d — the wrapper is deleted). Submodules are
imported eagerly so ``rebar.graph._graph`` / ``._links`` / ``._cache`` resolve as
attributes, and ``_reducer`` is the single loader instance ``_graph``/``_blockers``
use (so a test patch on ``rebar.graph._reducer.reduce_all_tickets`` intercepts real
calls).
"""

from rebar.graph import _blockers, _cache, _graph, _hierarchy, _links, _loader
from rebar.graph._blockers import _find_direct_blockers
from rebar.graph._graph import (
    _compute_dep_graph,
    build_dep_graph,
    check_cycle_at_level,
    check_would_create_cycle,
)
from rebar.graph._hierarchy import compute_archive_eligible, resolve_hierarchy_link
from rebar.graph._links import CyclicDependencyError, _is_active_link, add_dependency
from rebar.graph._loader import reduce_ticket

# Backward-compat aliases (tests access these directly).
_reduce_ticket = reduce_ticket
_reducer = _loader.reducer

__all__ = [
    "build_dep_graph",
    "check_cycle_at_level",
    "check_would_create_cycle",
    "resolve_hierarchy_link",
    "compute_archive_eligible",
    "CyclicDependencyError",
    "add_dependency",
    "reduce_ticket",
    "_is_active_link",
    "_find_direct_blockers",
    "_compute_dep_graph",
]
