"""Provide the parent-first lifecycle cascade shared by claim and transition.

A child cannot advance beyond an eligible ancestor. The shared walk resolves and advances
parents recursively, guards malformed cycles, and treats a parent moved by a concurrent writer
as a benign race. Callers supply their write primitive while retaining identical traversal and
error behavior.

This module imports neither claim nor transition at module scope because both depend on it.
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
    action: str,
    parent_action: str,
    cascade_seen: frozenset[str] | None = None,
) -> None:
    """Advance an eligible parent before its child.

    ``resolve_parent`` returns a parent currently in ``eligible_status`` or ``None``.
    ``advance`` supplies the recursive write. ``cascade_seen`` guards cycles. ``action`` and
    ``parent_action`` provide active and passive error phrases.

    Parent failure aborts the child while preserving exit and concurrency identity. The walk
    is sequential and fail-fast, so an advanced ancestor is not rolled back after a later
    failure.
    """
    seen = cascade_seen or frozenset()
    parent_id = resolve_parent(ticket_id, eligible_status)
    if parent_id is None or parent_id == ticket_id or parent_id in seen:
        return
    try:
        advance(parent_id, seen | {ticket_id})
    except CommandError as exc:
        # A peer may move the parent after the unlocked eligibility read. If the recheck finds it
        # outside ``eligible_status``, the ancestor invariant is satisfied and the child may
        # proceed. A parent still eligible indicates a failed write and aborts the child.
        if resolve_parent(ticket_id, eligible_status) is None:
            return  # parent moved concurrently; the caller proceeds with the child
        msg = (
            f"Error: cannot {action}: its parent {parent_id} could not be "
            f"{parent_action} first, so the child was left unchanged.\n"
            f"  Parent error: {exc.message}"
        )
        # Preserve the concurrency identity: ConcurrencyMismatch hardcodes returncode=10,
        # so it must be RE-RAISED as itself rather than flattened to a CommandError.
        if isinstance(exc, ConcurrencyMismatch):
            raise ConcurrencyMismatch(msg) from None
        raise CommandError(msg, returncode=exc.returncode) from None
