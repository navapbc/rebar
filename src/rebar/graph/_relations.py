"""Blocking-relation vocabulary and the graph-traversal primitives built on it.

Holds the pieces every graph query would otherwise re-derive: the set of relations
that count as "blocking", the inversion of those edges into a blocked→blockers map,
and the breadth-first walk skeleton the traversals share.

Kept dependency-free on purpose — nothing here imports another rebar module — so
the deliberately light-weight ``_ready`` can use it without pulling in the heavy
loader/graph modules. (It cannot live in ``_status``: that module imports
``_loader``, which ``_ready`` must not pull in.)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

_BLOCKING_RELATIONS = frozenset({"blocks", "depends_on"})


def bfs(
    starts: Iterable[str],
    neighbors_fn: Callable[[str, set[str]], Iterable[str]],
) -> set[str]:
    """Breadth-first walk that returns the ACCUMULATOR of discovered ids.

    The queue/visited bookkeeping shared by the graph traversals, with every
    filesystem read, reduction, exception-swallowing and filtering policy left to
    the caller's ``neighbors_fn`` — this skeleton has none.

    The returned set is the accumulator of everything ``neighbors_fn`` yielded, NOT
    ``visited``. The two differ in both directions and callers depend on it:

    - a start id is absent from the result unless some edge genuinely yields it,
      even though it is always in ``visited``;
    - a yielded id is present even when nothing backs it on disk and it is
      therefore never expanded.

    ``visited`` is passed to ``neighbors_fn`` (read-only by contract) because the
    callers consult it while deciding what to yield — an already-visited node is
    not re-yielded, which is precisely what keeps a start id out of the result.
    """
    accumulated: set[str] = set()
    visited: set[str] = set()
    queue: list[str] = list(starts)

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        for neighbor in neighbors_fn(current, visited):
            accumulated.add(neighbor)
            if neighbor not in visited:
                queue.append(neighbor)

    return accumulated


def build_blocked_by(ticket_states: dict) -> dict[str, set[str]]:
    """Invert blocking deps into a ``blocked_id → {blocker_ids}`` map.

    The single source of the blocking-edge inversion shared by ``find_ready_tickets``
    and ``detect_newly_unblocked`` (a relation-direction bug must now be fixed in ONE
    place). A LINK in ticket X's dir means "X <relation> target_id":
      - ``depends_on``: X depends on target_id → **target_id blocks X**
      - ``blocks``:     X blocks target_id     → **X blocks target_id**

    Kept here (the dependency-free relations module) so the light-weight ``_ready``
    can use it without pulling in the heavy loader/graph modules.
    """
    blocked_by: dict[str, set[str]] = {}
    for ticket_id, state in ticket_states.items():
        if not isinstance(state, dict):
            continue
        for dep in state.get("deps", []):
            if dep.get("relation") not in _BLOCKING_RELATIONS:
                continue
            target_id = dep.get("target_id")
            if not target_id:
                continue
            if dep.get("relation") == "depends_on":
                blocker_id, blocked_id = target_id, ticket_id
            else:  # "blocks"
                blocker_id, blocked_id = ticket_id, target_id
            blocked_by.setdefault(blocked_id, set()).add(blocker_id)
    return blocked_by
