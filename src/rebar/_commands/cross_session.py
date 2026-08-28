"""Cross-session warning detector (story 0804).

A PURE detector plus a thin convenience that composes it with the real reads. The
detector warns when a mutation targets a ticket whose live claim is held by a
DIFFERENT session; it performs no I/O so it stays trivially testable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def cross_session_warning(
    state: Mapping[str, Any], *, acting_session: str | None, enabled: bool
) -> str | None:
    """Return a one-line holder-naming warning, or ``None`` when silent.

    Silent when the toggle is off, the acting session is unknown, the ticket is
    unheld, or the holder IS the acting session. Pure function of its arguments.
    """
    if not enabled or not acting_session:
        return None
    claimed = state.get("claimed_session")
    if not claimed or acting_session == claimed:
        return None
    harness = state.get("claim_harness")
    if harness:
        return f"ticket held by another session {claimed} (harness {harness})"
    return f"ticket held by another session {claimed}"


def cross_session_warning_for(ticket_id: str, *, repo_root: str | None = None) -> str | None:
    """Compose :func:`cross_session_warning` with the real state, session, and config reads."""
    from rebar._commands.session_id import resolve_session_id
    from rebar._lib_reads import show_ticket
    from rebar.config import load_config

    state = show_ticket(ticket_id, repo_root=repo_root)
    acting = resolve_session_id()
    config = load_config(repo_root)  # read-via: cross-session-warning-toggle
    enabled = config.warnings.cross_session
    return cross_session_warning(state, acting_session=acting, enabled=enabled)
