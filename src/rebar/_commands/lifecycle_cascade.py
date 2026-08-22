"""The ONE parent-first cascade walk, shared by ``claim`` and ``transition`` (story 4329).

rebar upholds an invariant (docs/concurrency.md §I4a) that a ticket never runs ahead of
its own ancestors in the lifecycle: a child is not ``in_progress`` under a merely ``open``
parent, and a reopened / reactivated child is not left under a still-``closed`` parent
(bug ``cranial-sulfur-peafowl``). Every verb that takes a child along such an edge must
therefore first pull an eligible parent along the SAME edge, recursively up the chain.

That invariant grew THREE copies of the same walk — ``transition._cascade_parent_first``
(the reference implementation, driven by ``transition._CASCADING_EDGES`` and recursing
into ``transition_compute``), ``claim.claim_compute``'s own inline walk hardcoded to the
``open`` edge, and the reconciler's ``_cascade_inbound_status_parents`` (ticket
``bb73-97de-eeea-4899``), which already single-sourced the DECISION but re-implemented the
walk because it writes through the reconciler's ``_write_event_file``.

What genuinely differs between those sites is the **write primitive**, not the walk. What
was duplicated — and therefore able to DRIFT — is the ancestor lookup, the cycle guard,
the benign-race TOCTOU re-check, and the error attribution. This module holds exactly that
shared part, parameterised by ``resolve_parent`` (the lookup) and ``advance`` (the write
primitive), so the interactive verbs cannot disagree about what a raced parent means.

Deliberately imports NEITHER ``transition`` NOR ``claim`` at module level: both import
back into this layer, and the existing lazy, function-body imports at ``claim.py`` and
``apply_inbound_events.py`` exist precisely to break that cycle.
"""

from __future__ import annotations

from collections.abc import Callable

from rebar._commands._seam import CommandError
from rebar._commands.txn import ConcurrencyMismatch

__all__ = ["cascade_parent_first"]


def cascade_parent_first(
    ticket_id: str,
    *,
    eligible_status: str,
    resolve_parent: Callable[[str, str], str | None],
    advance: Callable[[str, frozenset[str]], object],
    verb: str,
    cascade_seen: frozenset[str] | None = None,
) -> None:
    """Move ``ticket_id``'s parent along this edge FIRST, so the child never runs ahead.

    ``resolve_parent(ticket_id, eligible_status)`` returns the parent's resolved id IFF
    the parent exists and currently sits in ``eligible_status`` — else ``None`` (no
    cascade, the child op proceeds alone). ``advance(parent_id, cascade_seen)`` is the
    caller's WRITE primitive; it is expected to recurse back through this helper, which
    is how the walk climbs the whole ancestor chain. ``verb`` names the child's operation
    for the error message (``"claim"``, ``"move to in_progress"``, …). ``cascade_seen``
    is the recursion guard that breaks a malformed parent cycle; callers leave it ``None``.

    A parent failure ABORTS the child, and the raised error names the parent as the cause
    while preserving the parent's exit code / concurrency identity — a raced parent
    surfaces as exit-10 / ``ConcurrencyError`` at the leaf too, so the "pick another
    ticket" retry path still fires there.

    Like the cascades it replaces, the walk is sequential and fail-fast rather than
    transactional: a parent already advanced is NOT rolled back if the child then fails.
    """
    seen = cascade_seen or frozenset()
    parent_id = resolve_parent(ticket_id, eligible_status)
    if parent_id is None or parent_id == ticket_id or parent_id in seen:
        return
    try:
        advance(parent_id, seen | {ticket_id})
    except CommandError as exc:
        # TOCTOU: the cascade DECISION above read the parent's status WITHOUT the write
        # lock. A peer may have moved the parent off `eligible_status` between that read
        # and the locked parent write we just attempted, which is why it was rejected.
        # Re-check the parent's live status: if it has LEFT `eligible_status`, the
        # cascade's whole purpose (never leave the child ahead of its parent) is already
        # satisfied, so this is BENIGN — return normally and let the caller move the
        # child, matching the single-agent contract "parent already moved -> only the
        # requested ticket moves". Only a parent still genuinely in `eligible_status`
        # (e.g. its own gate blocked the write) is a real failure that must abort the
        # child. Sharing this re-check is the point of the extraction: with a copy per
        # verb the two cascades could drift on what a raced parent means.
        if resolve_parent(ticket_id, eligible_status) is None:
            return  # parent moved concurrently; the caller proceeds with the child
        msg = (
            f"Error: cannot {verb} {ticket_id}: its parent {parent_id} could not "
            f"{verb} first, so the child was left unchanged.\n"
            f"  Parent error: {exc.message}"
        )
        # Preserve the concurrency identity: ConcurrencyMismatch hardcodes returncode=10,
        # so it must be RE-RAISED as itself rather than flattened to a CommandError.
        if isinstance(exc, ConcurrencyMismatch):
            raise ConcurrencyMismatch(msg) from None
        raise CommandError(msg, returncode=exc.returncode) from None
