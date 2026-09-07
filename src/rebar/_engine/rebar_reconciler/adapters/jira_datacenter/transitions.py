"""Route Jira Data Center status changes through workflow transitions.

Status is not an editable issue field. Direct transition requests and
``update_issue`` use this resolver and dispatcher. ``transport`` re-exports the
public names.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter.retry import _with_connection_retry

__all__ = [
    "IllegalTransitionError",
    "resolve_transition",
    "route_status_to_transition",
    "transition_to_status",
]


class IllegalTransitionError(ValueError):
    """Represent a workflow state unreachable from the issue's current state.

    This non-HTTP ``ValueError`` becomes an observable per-mutation failure
    without aborting the remaining batch.
    """


def resolve_transition(client: Any, remote_id: str, target_status: str) -> dict[str, Any]:
    """Return the transition that reaches ``target_status``.

    Prefer an exact transition-name match. Otherwise require a unique destination
    ``to.name`` match. Refuse missing or ambiguous destinations because Jira
    exposes only transitions legal from the issue's current state.
    """
    transitions = _with_connection_retry(lambda: client.transitions(remote_id))
    entries = [t for t in transitions if isinstance(t, dict)]

    match = next((t for t in entries if t.get("name") == target_status), None)

    if match is None:
        by_destination = [
            t
            for t in entries
            if isinstance(t.get("to"), dict) and t["to"].get("name") == target_status
        ]
        if len(by_destination) > 1:
            routes = sorted(str(t.get("name", "")) for t in by_destination)
            raise ValueError(
                f"transition to status {target_status!r} is AMBIGUOUS for {remote_id}: "
                f"{len(by_destination)} transitions declare it as their destination "
                f"({routes}). Name the transition explicitly rather than the status."
            )
        if by_destination:
            match = by_destination[0]

    if match is None:
        available = sorted(
            f"{t.get('name', '')!r} -> {(t.get('to') or {}).get('name', '?')!r}" for t in entries
        )
        raise ValueError(
            f"no transition named {target_status!r} is available for {remote_id}, and none "
            f"declares it as a destination status (available, as "
            f"'transition' -> 'destination status': {available})"
        )
    return match


def transition_to_status(client: Any, remote_id: str, target_status: str) -> None:
    """Resolve ``target_status`` to a transition and execute it."""
    match = resolve_transition(client, remote_id, target_status)
    _with_connection_retry(lambda: client.transition_issue(remote_id, match["id"]))


def _is_illegal_transition(exc: BaseException) -> bool:
    """True when ``exc`` is Jira refusing a transition from the CURRENT workflow state.

    Mirrors ``dispatch_one._is_illegal_transition_400`` — a 400 whose body mentions
    ``illegal`` or ``transition`` — so the two layers draw the "state error, not an
    outage" line in the same place. Deliberately narrow: a 401/403/5xx is a real
    failure and must keep the fail-fast contract rather than be softened here.
    """
    return getattr(exc, "code", None) == 400 and (
        "illegal" in str(exc).lower() or "transition" in str(exc).lower()
    )


def route_status_to_transition(client: Any, remote_id: str, status: str) -> None:
    """Move an issue to ``status`` through a workflow transition.

    Convert missing transitions and illegal-transition HTTP responses to
    ``IllegalTransitionError`` so the batch records a nonfatal failure. Propagate
    unrelated HTTP failures unchanged.
    """
    try:
        transition_to_status(client, remote_id, status)
    except IllegalTransitionError:
        raise
    except BackendHTTPError as exc:
        if not _is_illegal_transition(exc):
            raise
        raise IllegalTransitionError(
            f"outbound status {status!r} could not be applied to {remote_id}: Jira rejected "
            f"the transition as illegal from the issue's current workflow state ({exc})"
        ) from exc
    except ValueError as exc:
        raise IllegalTransitionError(
            f"outbound status {status!r} could not be applied to {remote_id}: no transition "
            f"reaches it from the issue's current workflow state ({exc})"
        ) from exc
