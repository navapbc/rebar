"""Read-time derivation of inbound relationships for a single ticket.

``find_inbound_relationships`` answers "which other tickets point at this one?"
— the half of a ticket's relationship graph that is not stored in its own
directory (incoming links and child tickets).

It is deliberately *read-only*: no reciprocal events are written, so there is
no stored mirror that can drift or be left half-updated. Correctness rests on
two facts about the event log:

  * Links and ``parent_id`` are always stored as the *resolved canonical* ID
    (``ticket_link`` resolves both endpoints before writing), so any genuine
    inbound relationship's event file contains the exact ID string.
  * Therefore a cheap byte-level substring scan over event files yields a
    candidate superset with **no false negatives**. We then reduce only those
    candidates and confirm the relationship against their structured state,
    discarding incidental prose mentions (e.g. a comment naming the ID).

Cost is O(one byte-scan of the corpus) + O(reduce only the mentioning tickets),
rather than reducing every ticket.
"""

from __future__ import annotations

import os

from ticket_reducer._api import reduce_ticket

# Source tickets in these terminal states are not surfaced as live
# relationships — a deleted ticket's links are tombstoned, not active.
_INACTIVE_SOURCE_STATUSES = {"deleted"}

# Relations whose reciprocal is already stored on the subject's own outgoing
# deps (symmetric links). For these we suppress the inbound entry so the same
# logical relationship is not listed twice in ``ticket show``.
_SYMMETRIC_RELATIONS = {"relates_to"}


def _mentioning_dirs(tracker_dir: str, ticket_id: str) -> list[str]:
    """Return ticket directory names whose event files mention ``ticket_id``.

    Byte-level substring scan — no JSON parsing. Excludes the subject ticket
    itself, dot-directories, and the per-ticket ``.cache.json``.
    """
    needle = ticket_id.encode("utf-8")
    candidates: list[str] = []
    try:
        entries = sorted(os.listdir(tracker_dir))
    except OSError:
        return candidates

    for entry in entries:
        if entry.startswith(".") or entry == ticket_id:
            continue
        entry_path = os.path.join(tracker_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        try:
            event_names = os.listdir(entry_path)
        except OSError:
            continue
        for name in event_names:
            if not name.endswith(".json") or name == ".cache.json":
                continue
            try:
                with open(os.path.join(entry_path, name), "rb") as fh:
                    if needle in fh.read():
                        candidates.append(entry)
                        break
            except OSError:
                continue
    return candidates


def find_inbound_relationships(ticket_id: str, tracker_dir: str) -> dict:
    """Derive inbound links and children for ``ticket_id``.

    Returns ``{"ticket_id", "inbound_links", "children"}`` where
    ``inbound_links`` is a sorted list of ``{"from_id", "relation"}`` and
    ``children`` is a sorted list of ticket IDs whose ``parent_id`` is
    ``ticket_id``.
    """
    tracker_dir = os.path.normpath(str(tracker_dir))

    # The subject's own outgoing symmetric links — used to suppress the
    # reciprocal half of a relates_to that the subject already lists.
    own_symmetric: set[tuple[str, str]] = set()
    subject_state = reduce_ticket(os.path.join(tracker_dir, ticket_id))
    if isinstance(subject_state, dict):
        for dep in subject_state.get("deps") or []:
            rel = dep.get("relation", "")
            tid = dep.get("target_id", dep.get("target", ""))
            if rel in _SYMMETRIC_RELATIONS and tid:
                own_symmetric.add((tid, rel))

    inbound_links: list[dict] = []
    children: list[str] = []

    for cand in _mentioning_dirs(tracker_dir, ticket_id):
        state = reduce_ticket(os.path.join(tracker_dir, cand))
        if not isinstance(state, dict):
            continue
        if state.get("status") in _INACTIVE_SOURCE_STATUSES or state.get("archived"):
            continue

        if state.get("parent_id") == ticket_id:
            children.append(cand)

        for dep in state.get("deps") or []:
            tid = dep.get("target_id", dep.get("target", ""))
            if tid != ticket_id:
                continue
            rel = dep.get("relation", "")
            if rel in _SYMMETRIC_RELATIONS and (cand, rel) in own_symmetric:
                continue
            inbound_links.append({"from_id": cand, "relation": rel})

    inbound_links.sort(key=lambda e: (e["from_id"], e["relation"]))
    children.sort()
    return {
        "ticket_id": ticket_id,
        "inbound_links": inbound_links,
        "children": children,
    }
