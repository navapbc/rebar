"""Evidence-scope expansion for the completion close precheck.

The close precheck has two deterministic "work landed" obligations before the
billable completion verifier runs:

* a ticket that records ``file_impact`` must have a reachable implementation
  commit, and
* each referenced commit's changed paths must stay inside the reviewed
  ``file_impact`` envelope.

For a parent ticket, the evidence scope is naturally wider than the parent id:
children (and grandchildren) can own the implementation commits that satisfy the
parent's acceptance criteria.  Bug ``ferric-jet-scorpion`` added that subtree
credit.  This module owns the next evidence edge: a child may close as an
administrative disposition, most often ``duplicate`` or ``superseded``, where the
signed disposition says "the work lives on this replacement ticket."  A parent
close must credit that replacement evidence too; otherwise an honest duplicate
close makes the parent impossible to certify without ``--force``.

The expansion is intentionally narrow.  A random relationship does not broaden
the gate; only a ticket already closed with a close class and naming a live
replacement can add that replacement, and the ordinary no-reference failure still
fires when the expanded scope contains no commit.
"""

from __future__ import annotations

import os
from collections import deque
from typing import Any


def replacement_target_for_closed_disposition(
    ticket_id: str, tracker: str, *, ticket_view: Any | None = None
) -> str | None:
    """Return the live replacement named by a closed disposition ticket, if any."""
    from rebar._commands.close_disposition import DISPOSITION_CLASSES

    if ticket_view is not None:
        try:
            state = ticket_view.show_ticket(ticket_id)
        except Exception:  # noqa: BLE001 — an unreadable pinned source cannot expand scope
            return None
        if state.get("status") != "closed" or state.get("close_class") not in DISPOSITION_CLASSES:
            return None
        return _pinned_replacement_target(ticket_id, ticket_view)

    from rebar._commands.close_disposition import find_replacement
    from rebar.reducer import reduce_ticket

    try:
        state = reduce_ticket(os.path.join(tracker, ticket_id))
    except Exception:  # noqa: BLE001 — unreadable siblings cannot expand the evidence scope
        return None
    if (
        not isinstance(state, dict)
        or state.get("status") != "closed"
        or state.get("close_class") not in DISPOSITION_CLASSES
    ):
        return None
    return find_replacement(ticket_id, str(state["close_class"]), tracker)


def _pinned_replacement_target(ticket_id: str, ticket_view: Any) -> str | None:
    """Pinned-view equivalent of ``replacement_of(..., require_live=True)``."""
    try:
        state = ticket_view.show_ticket(ticket_id, include_inbound=True)
    except Exception:  # noqa: BLE001 — fail closed if the pinned graph cannot be read
        return None
    for dep in state.get("deps") or []:
        if dep.get("relation") == "duplicates":
            target = _live_pinned_ticket(str(dep.get("target_id", "") or ""), ticket_view)
            if target is not None:
                return target
    for link in state.get("inbound_deps") or []:
        if link.get("relation") == "supersedes":
            source = _live_pinned_ticket(str(link.get("from_id", "") or ""), ticket_view)
            if source is not None:
                return source
    return None


def _live_pinned_ticket(ticket_ref: str, ticket_view: Any) -> str | None:
    """Resolve ``ticket_ref`` to a non-archived/non-deleted pinned ticket."""
    if not ticket_ref:
        return None
    resolved = ticket_view.resolve(ticket_ref)
    if resolved is None:
        return None
    try:
        state = ticket_view.show_ticket(resolved)
    except Exception:  # noqa: BLE001 — fail closed if the pinned target cannot be read
        return None
    if state.get("error"):
        return None
    if state.get("archived") or state.get("status") in {"archived", "deleted"}:
        return None
    return resolved


def _replacement_descendant_ids(
    replacement: str, tracker: str, *, ticket_view: Any | None = None
) -> list[str]:
    """Return transitive descendants of a replacement ticket."""
    if ticket_view is not None:
        return list(ticket_view.transitive_descendant_ids(replacement))

    from rebar._engine_support.descendants import list_descendants

    descendants = list_descendants(replacement, tracker)
    return [
        desc_id
        for bucket in ("epics", "stories", "tasks", "bugs")
        for desc_id in descendants.get(bucket, [])
    ]


def _resolve(ticket_id: str, tracker: str, *, ticket_view: Any | None = None) -> str | None:
    """Resolve a ticket id through the same store view as the caller."""
    if ticket_view is not None:
        return ticket_view.resolve(ticket_id)

    from rebar._engine_support.resolver import resolve_ticket_id

    return resolve_ticket_id(ticket_id, tracker)


def expand_with_disposition_replacements(
    accepted_ids: set[str], tracker: str, *, ticket_view: Any | None = None
) -> set[str]:
    """Add closed-disposition replacement tickets to a completion evidence scope."""
    expanded = set(accepted_ids)
    queue = deque(sorted(expanded))
    while queue:
        current = queue.popleft()
        replacement = replacement_target_for_closed_disposition(
            current, tracker, ticket_view=ticket_view
        )
        if replacement is None or replacement in expanded:
            continue
        expanded.add(replacement)
        queue.append(replacement)
        for desc_id in _replacement_descendant_ids(replacement, tracker, ticket_view=ticket_view):
            resolved = _resolve(desc_id, tracker, ticket_view=ticket_view)
            if resolved is not None and resolved not in expanded:
                expanded.add(resolved)
                queue.append(resolved)
    return expanded
