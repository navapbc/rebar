"""Managed-reference provenance: the compaction-surviving removal-sync primitive.

Cross-system sync (Jira today; Linear / GitHub Issues planned) must propagate a
LOCAL removal of a reference — detach a parent, unlink a dependency — WITHOUT it
being resurrected by the next inbound pass. To decide REMOVE-vs-ADOPT for a peer
reference that is present on the peer but absent locally, the outbound differ asks
one question: *did our side ever manage this reference?* If yes, the local absence
is a deliberate removal and we propagate a delete; if no, the peer added it and we
ADOPT it inbound (never clobber a human-created reference).

That question is answered by ``managed_refs``: a **strictly-monotonic** projection,
maintained by the reducer in ``compiled_state``, of every logical reference this
ticket has *ever* managed. Because it lives in ``compiled_state`` it is restored by
``process_snapshot`` and therefore **survives ``compact_ticket``** — closing the
durability hole that a raw-event projection (cf. ``local_label_intent`` for labels)
fails closed across (a removal performed at/after a compaction boundary would
otherwise be re-resurrected because the compacted log no longer proves we'd managed
the ref).

A "logical reference" is normalized, **provider-agnostic**, as ``(kind, target)``:

  - ``kind``    one of :data:`MANAGED_REF_KINDS` — ``parent`` or a link relation
                (``blocks`` / ``depends_on`` / ``relates_to``).
  - ``target``  the LOCAL ticket id the reference points at (the parent id, or the
                dependency target). Never a Jira key — each provider maps the local
                ref to its own entity at sync time, so this primitive is reused
                unchanged by future peers.

Serialized as a deterministically-sorted list of ``[kind, target]`` pairs so the
SNAPSHOT ``compiled_state`` stays byte-stable across rebuilds.

Reclamation: ``managed_refs`` is strictly monotonic here (never pruned by
UNLINK/detach — pruning in the reducer would re-open the resurrection window). A
*safe* prune needs the PEER snapshot (a ref is reclaimable only once it is absent
both locally AND on the peer) and so must be a reconcile-time step emitting an
explicit prune event — a documented FUTURE hook, deliberately out of scope here.
Per-ticket ref cardinality is small (tens), so the unbounded-growth concern is not
a practical one at this scale.
"""

from __future__ import annotations

from typing import Any, TypeGuard

# The closed, provider-agnostic kind vocabulary. ``parent`` is the single-valued
# containment reference; the rest are the link relations that map to a peer
# issue-link. Relations with no reliable peer link type (duplicates / supersedes /
# discovered_from) are intentionally absent — they are never synced, so they are
# never "managed" for removal-propagation purposes.
MANAGED_REF_KINDS: tuple[str, ...] = ("parent", "blocks", "depends_on", "relates_to")

# A managed reference, normalized.
Ref = tuple[str, str]


def _is_kind(kind: Any) -> TypeGuard[str]:
    """True iff ``kind`` is one of the managed-ref kinds. A ``TypeGuard`` so callers narrow
    the value to ``str`` — e.g. ``(kind, target)`` types as ``tuple[str, str]`` (the ``Ref``)."""
    return isinstance(kind, str) and kind in MANAGED_REF_KINDS


def parse_managed_refs(raw: Any) -> set[Ref]:
    """Parse a stored ``managed_refs`` value into a set of ``(kind, target)`` tuples.

    Tolerant by design (this reads persisted, possibly-legacy state): a missing /
    malformed value yields the empty set, and individual malformed entries are
    skipped rather than raising. An empty set means "nothing managed" — under which
    :func:`should_propagate_removal` returns False for every ref (fail-open: no
    delete is propagated, so a transient/absent projection can only delay
    convergence, never fire an irreversible wrong removal or clobber a human ref).
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
    """Fold one logical reference into ``state['managed_refs']`` (idempotent, monotonic).

    A no-op when ``kind`` is not a managed kind or ``target`` is falsy. Idempotent:
    re-folding the same ref (e.g. a duplicate-delivered event) does not change the
    set, so the projection is safe under at-least-once replay. Never removes.
    """
    if not _is_kind(kind) or not target or not isinstance(target, str):
        return
    refs = parse_managed_refs(state.get("managed_refs"))
    refs.add((kind, target))
    state["managed_refs"] = serialize_managed_refs(refs)


def seed_managed_refs_from_current(state: dict) -> list[list[str]]:
    """Build a managed-refs list from a ticket's CURRENT ``parent_id`` + ``deps``.

    The migration path for pre-feature / old-SNAPSHOT tickets whose persisted
    ``compiled_state`` predates this field: their current references are treated as
    managed (a ref already in ``deps`` was created locally or inbound-ADOPTED — rebar
    owns it, so a later local removal should propagate; a Jira-only ref never in
    ``deps`` is simply not present here and is still ADOPTED inbound, never clobbered).

    Known limitation (documented): a removal performed BEFORE this feature shipped is
    already gone from current state and from a compacted log, so it cannot be
    recovered here — only post-feature removals self-heal.
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
    """Decide whether a peer reference absent locally should be DELETED on the peer.

    The shared, provider-agnostic removal gate consumed by both the parent and the
    link outbound paths. Returns True (propagate the delete) iff ``(kind, target)`` is
    in the ticket's managed-ref set — i.e. we managed the reference and its local
    absence is a deliberate removal. Returns False otherwise, including when
    ``managed_refs`` is absent/empty — degrading to additive-only (the peer ref is
    left for inbound ADOPT, never clobbered, and the removal is simply not yet
    propagated rather than wrongly fired).
    """
    if not _is_kind(kind) or not isinstance(target, str) or not target:
        return False
    return (kind, target) in managed_ref_set(local_ticket)
