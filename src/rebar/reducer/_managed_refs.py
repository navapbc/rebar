"""Compaction-stable provenance for cross-system reference removal.

``managed_refs`` records every local ``(kind, target)`` ever managed. When a peer retains a
managed reference that is absent locally, outbound sync deletes it. Unknown peer references
are adopted to preserve human changes. Storing the monotonic projection in ``compiled_state``
preserves removal intent across compaction.

Kinds represent ``parent`` or a synced link relation. Targets use local ticket IDs for provider
translation. Snapshots sort ``[kind, target]`` pairs for stable bytes.

The reducer never prunes provenance because safe reclamation requires confirmed absence both
locally and on the peer.
"""

from __future__ import annotations

from typing import Any, TypeGuard

# Managed kinds have peer mappings. ``parent`` represents containment. Duplicate, supersede,
# and discovery relations remain local and do not participate in removal propagation.
MANAGED_REF_KINDS: tuple[str, ...] = ("parent", "blocks", "depends_on", "relates_to")

# A managed reference, normalized.
Ref = tuple[str, str]


def _is_kind(kind: Any) -> TypeGuard[str]:
    """True iff ``kind`` is one of the managed-ref kinds. A ``TypeGuard`` so callers narrow
    the value to ``str`` — e.g. ``(kind, target)`` types as ``tuple[str, str]`` (the ``Ref``)."""
    return isinstance(kind, str) and kind in MANAGED_REF_KINDS


def parse_managed_refs(raw: Any) -> set[Ref]:
    """Parse valid ``(kind, target)`` pairs from persisted state.

    Malformed containers and entries are ignored. An empty result proves no managed ownership,
    so synchronization adopts peer refs instead of deleting them.
    """
    out: set[Ref] = set()
    if not isinstance(raw, (list, tuple)):
        return out
    for entry in raw:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            kind, target = entry[0], entry[1]
            if _is_kind(kind) and isinstance(target, str) and target:
                out.add((kind, target))
    return out


def serialize_managed_refs(refs: set[Ref]) -> list[list[str]]:
    """Serialize a set of refs to a deterministically-sorted list of ``[kind, target]``.

    Sorting makes the SNAPSHOT ``compiled_state`` byte-stable regardless of the
    order refs were folded in.
    """
    return [[kind, target] for kind, target in sorted(refs)]


def add_managed_ref(state: dict, kind: str, target: Any) -> None:
    """Add a valid ref idempotently without removing prior refs.

    Invalid kinds and empty or non-string targets do nothing. Replaying a duplicate event leaves
    the serialized set unchanged.
    """
    if not _is_kind(kind) or not target or not isinstance(target, str):
        return
    refs = parse_managed_refs(state.get("managed_refs"))
    refs.add((kind, target))
    state["managed_refs"] = serialize_managed_refs(refs)


def seed_managed_refs_from_current(state: dict) -> list[list[str]]:
    """Seed missing snapshot provenance from the current parent and dependencies.

    Current local and adopted refs become managed. Peer-only refs remain eligible for adoption.
    References removed before compaction cannot be recovered from current state.
    """
    refs: set[Ref] = set()
    parent_id = state.get("parent_id") or None
    if isinstance(parent_id, str) and parent_id:
        refs.add(("parent", parent_id))
    for dep in state.get("deps") or []:
        if not isinstance(dep, dict):
            continue
        relation = dep.get("relation")
        target = dep.get("target_id")
        if _is_kind(relation) and isinstance(target, str) and target:
            refs.add((relation, target))
    return serialize_managed_refs(refs)


def managed_ref_set(local_ticket: dict) -> set[Ref]:
    """Return the set of managed ``(kind, target)`` refs for a reduced ticket dict.

    Missing ``managed_refs`` → empty set (fail-open). This is the single read path
    both the parent and link outbound gates share."""
    return parse_managed_refs(local_ticket.get("managed_refs"))


def should_propagate_removal(kind: str, target: str, local_ticket: dict) -> bool:
    """Return whether a locally absent reference should be deleted from its peer.

    Deletion requires a valid ref in the managed set. Missing provenance preserves the peer ref
    for inbound adoption.
    """
    if not _is_kind(kind) or not isinstance(target, str) or not target:
        return False
    return (kind, target) in managed_ref_set(local_ticket)
