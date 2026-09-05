"""rebar.graph — dependency-graph build, cycle detection, hierarchy promotion,
and link writes.

Re-exports the flat public API (build/cycle/hierarchy/link helpers) so the
library, CLI, and tests share ONE surface. Submodules are
imported eagerly so ``rebar.graph._graph`` / ``._links`` / ``._cache`` resolve as
attributes. Reducer patch points live on the canonical loader module:
``rebar.graph._loader.reduce_ticket`` and ``rebar.graph._loader.reducer``.
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
from rebar.graph._links import (
    CyclicDependencyError,
    _is_active_link,
    add_dependency,
    remove_dependency,
)
from rebar.graph._loader import reduce_ticket

__all__ = [
    "CyclicDependencyError",
    # Submodules imported eagerly so ``rebar.graph._graph`` / ``._links`` / etc.
    # resolve as attributes for callers and test patches.
    "_blockers",
    "_cache",
    "_compute_dep_graph",
    "_find_direct_blockers",
    "_graph",
    "_hierarchy",
    "_is_active_link",
    "_links",
    "_loader",
    "add_dependency",
    "build_dep_graph",
    "check_cycle_at_level",
    "check_would_create_cycle",
    "compute_archive_eligible",
    "reduce_ticket",
    "remove_dependency",
    "resolve_hierarchy_link",
]
