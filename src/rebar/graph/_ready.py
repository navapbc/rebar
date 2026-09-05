"""Ready-to-work computation (single source of truth).

Extracted verbatim from ``ticket-ready.py`` (recommendation-#2 Step 1) so the CLI
script and the in-process library share ONE implementation. A ticket is "ready"
when:
  1. its status is "open" or "in_progress", and
  2. every direct blocker (a dep with relation "depends_on" or "blocks") is in a
     TERMINAL state -- closed, archived or deleted (or missing) -- per the shared
     ``rebar.reducer.is_terminal_status`` predicate.

Kept dependency-light on purpose: it reaches the reducer through the graph loader
and does not pull in the heavier graph modules.
"""

from __future__ import annotations

from pathlib import Path

from rebar.reducer import is_terminal_status

from . import _loader
from ._relations import build_blocked_by

# The "open-ish" statuses that make a ticket eligible for readiness/dispatch.
# `idea` is deliberately EXCLUDED (by omission): an undesigned idea must never
# surface in `ready`/`next-batch`, so it is never counted as ready work here.
_OPEN_STATUSES = {"open", "in_progress"}

# Fields the readiness computation never reads. Dropped during PASS 1's reduce so the
# whole store's bodies + signature material are never simultaneously live; the ready
# subset is re-reduced in full afterwards (see ``_rehydrate``). Spelled here rather
# than imported from ``rebar._engine_support.reads`` to keep this module
# dependency-light (see the module docstring).
_READY_OMITTED_FIELDS: tuple[str, ...] = (
    "description",
    "comments",
    "authorship_ledger",
    "attestations",
    "signature",
    "keyring",
)


def _is_closed(status: str) -> bool:
    return is_terminal_status(status)


def _is_open(status: str) -> bool:
    return status in _OPEN_STATUSES


def find_ready_tickets(
    tracker_dir: str,
    epic_filter: str | None = None,
) -> list[dict]:
    """Return list of ticket state dicts that are ready to work on.

    A ticket is ready when:
    - status is "open" or "in_progress"
    - every direct blocker is in a TERMINAL state -- closed, archived or deleted
      (or missing) -- per the shared ``rebar.reducer.is_terminal_status`` predicate
    - if epic_filter is set, ticket's parent_id must equal epic_filter

    Args:
        tracker_dir: Path to the .tickets-tracker directory.
        epic_filter: Optional epic ID to scope results to direct children.

    Returns:
        List of ticket state dicts (ready tickets only).
    """
    tracker_path = Path(tracker_dir)
    if not tracker_path.is_dir():
        return []

    # session_log tickets never participate in the dependency graph — exclude them
    # so ready-computation timings are unaffected by verbose log bodies.
    #
    # PASS 1 (readiness): reduce LEAN. The computation below reads only
    # ticket_id/status/parent_id/deps, so the bulky bodies + signature material are
    # dropped inside the reducer loop and the whole store's copies are never
    # simultaneously live. Callers of this function still receive FULL states — the
    # ready subset is re-reduced in pass 2 below.
    all_states_list = _loader.reducer.reduce_all_tickets(
        str(tracker_dir),
        exclude_session_logs=True,
        omit_fields=_READY_OMITTED_FIELDS,
    )

    # Build a lookup dict, skipping error states.
    ticket_states: dict[str, dict] = {}
    for state in all_states_list:
        tid = state.get("ticket_id")
        if not tid:
            continue
        if state.get("status") in ("error", "fsck_needed"):
            continue
        ticket_states[tid] = state

    # blocked_id → set of blocker_ids (shared blocking-edge inversion).
    blocked_by = build_blocked_by(ticket_states)

    def all_blockers_closed(ticket_id: str) -> bool:
        blockers = blocked_by.get(ticket_id, set())
        for blocker_id in blockers:
            blocker_state = ticket_states.get(blocker_id)
            if blocker_state is None:
                # Tombstoned / missing → treat as closed
                continue
            if not _is_closed(blocker_state.get("status", "open")):
                return False
        return True

    ready: list[dict] = []
    for ticket_id, state in ticket_states.items():
        status = state.get("status", "open")
        if not _is_open(status):
            continue
        if epic_filter is not None and state.get("parent_id") != epic_filter:
            continue
        if all_blockers_closed(ticket_id):
            ready.append(state)

    return _rehydrate(tracker_path, ready)


def _rehydrate(tracker_path: Path, ready: list[dict]) -> list[dict]:
    """PASS 2: re-reduce each ready ticket so the returned dicts are FULL states.

    ``find_ready_tickets`` computes readiness over lean states (see
    ``_READY_OMITTED_FIELDS``) but its contract is the full compiled state —
    ``rebar.ready()`` and the MCP ``ready_tickets(full=True)`` path both depend on
    it. The ready subset is a tiny fraction of the store, and each ticket's reduce
    is served from the reducer cache the lean pass just warmed, so the second pass
    is cheap. A ticket that fails to re-reduce (raced away mid-scan) keeps its lean
    state rather than vanishing from the result.
    """
    out: list[dict] = []
    for state in ready:
        ticket_id = state.get("ticket_id") or ""
        full = _loader.reducer.reduce_ticket(str(tracker_path / ticket_id))
        out.append(full if isinstance(full, dict) else state)
    return out
