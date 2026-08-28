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
    """Compose :func:`cross_session_warning` with the real state, session, and config reads.

    Reads the config toggle and acting session BEFORE the ``show_ticket`` reduce: the pure
    detector is silent whenever the feature is off or the session is unknown, so short-
    circuiting here avoids a wasted reduce (and its cache write) on every instrumented call.
    """
    from rebar._commands.session_id import resolve_session_id
    from rebar.config import load_config

    config = load_config(repo_root)  # read-via: cross-session-warning-toggle
    enabled = config.warnings.cross_session
    acting = resolve_session_id()
    if not enabled or not acting:
        return None

    from rebar._lib_reads import show_ticket

    state = show_ticket(ticket_id, repo_root=repo_root)
    return cross_session_warning(state, acting_session=acting, enabled=enabled)
