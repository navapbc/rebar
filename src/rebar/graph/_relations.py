"""Single source of truth for the blocking-relation vocabulary.

Kept dependency-free on purpose: every module that needs to know which relations
are "blocking" — including the deliberately light-weight ``_ready`` — imports it
from here, so the constant is defined ONCE and no module forks its own copy. (It
cannot live in ``_status``: that module imports the heavy ``_loader``, which
``_ready`` must not pull in.)
"""

from __future__ import annotations

_BLOCKING_RELATIONS = frozenset({"blocks", "depends_on"})


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
