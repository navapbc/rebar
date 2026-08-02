"""Workflow-TRANSITION resolution and dispatch for the Data Center transport.

Everything in this module answers one question — *"which Jira transition moves this
issue to the state the reconciler asked for, and what happens when none does?"* —
and it is the cluster ``JiraDataCenterTransport.transition_issue_by_name`` and
``JiraDataCenterTransport.update_issue`` both call. It was relocated out of
``transport.py`` along that already-existing call-graph seam (``update_issue`` →
``route_status_to_transition`` → ``resolve_transition``) to buy headroom under the
LOCKED 800-line module-size cap, exactly as the retry/error cluster was relocated to
``retry.py`` by story S1. ``transport.py`` re-exports the public names, so
``transport.<name>`` keeps resolving for existing importers.

**Status is not an editable Jira field.** ``PUT /rest/api/2/issue/{key}`` with
``fields.status`` is rejected by Jira: a workflow state is reachable ONLY by executing
a transition (``POST /rest/api/2/issue/{key}/transitions``). That asymmetry is the
whole reason this module exists — see :func:`route_status_to_transition`.
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
    """The requested workflow state is not reachable from the issue's CURRENT state.

    Deliberately **not** an ``HTTPError`` subclass, and that is load-bearing rather
    than stylistic. ``applier._apply_one`` re-raises ``urllib.error.HTTPError``
    ABOVE its per-mutation backstop (``applier.py:757``) as a fail-fast contract, so
    letting the raw ``BackendHTTPError`` escape a status route would abort the WHOLE
    outbound pass over one issue sitting in an unexpected state — skipping every
    valid mutation queued behind it, the failure shape bug 449f-f9bf-be90-47fe was
    filed for. Raising an ordinary exception instead routes this to
    ``apply_handlers.record_backstop_failure``, which records a ``bridge_alerts``
    entry and lets the batch continue: non-fatal, but OBSERVABLE.

    Subclasses ``ValueError`` so it stays compatible with callers already catching
    the ``ValueError`` :func:`resolve_transition` raises for an unreachable state.
    """


def resolve_transition(client: Any, remote_id: str, target_status: str) -> dict[str, Any]:
    """Return the transition entry that moves ``remote_id`` to ``target_status``.

    **A transition's NAME is not its destination STATUS name, and every production caller
    passes the latter** (bug 7f93). ``LOCAL_STATUS_TO_JIRA`` maps local ``in_progress`` to
    ``"In Progress"`` — a workflow STATE name, which is what that map is documented to hold —
    while Jira's classic workflow offers transitions called ``Start Progress`` and ``Done``.
    Matching only on the transition's own name therefore missed, raised, and got SOFT-FAILED
    into ``bridge_alerts`` by ``apply_handlers.record_backstop_failure``: the pass exited 0
    with no traceback and the issue's status never changed.

    It was a silent PARTIAL failure, which is worse than a loud one. ``Done`` happens to be
    both a transition name and a status name on that workflow, so ``closed`` synced by
    coincidence while ``in_progress`` did not. It never fired on Cloud because the DIG
    workflow names its transitions after their destinations, so the two spellings coincide.

    Resolution order, and the order matters:
      1. an EXACT transition-name match wins, so a caller that genuinely passes a transition
         name (the existing live round-trip test does) is unaffected — this is additive;
      2. otherwise a UNIQUE destination-status match on ``to.name``, which the transitions
         payload declares;
      3. an AMBIGUOUS destination raises rather than guessing. Two transitions can lead to one
         status by different routes (different screens or conditions), and silently picking
         one would be a coin flip the caller cannot see.

    Jira lists ONLY the transitions that are legal from the issue's current state, so "no
    match" is also how an illegal-from-here transition presents — see
    :func:`route_status_to_transition`.
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
    """Move ``remote_id`` to workflow state ``status``, non-fatally when it cannot.

    THE reason this function exists: ``status`` arrives in ``update_issue``'s kwargs
    (``dispatch_apply_phases._OUTBOUND_BATCH_ALLOWLIST`` contains it) and the Data
    Center transport used to pass it straight into ``issue.update(fields=…)`` — a
    REST field EDIT of a field Jira does not let you edit. Jira rejected it, the
    error was soft-failed, and the outbound status silently never changed. Cloud
    already routed status to a transition inside its own transport
    (``adapters/jira/acli.py:170,182-183``); this is the missing DC half.

    An unreachable destination — whether it surfaces as "no such transition is
    offered" (Jira only lists transitions legal from the current state) or as a 400
    illegal-transition response from the execute call — becomes
    :class:`IllegalTransitionError`, which is NOT fatal to the pass but IS recorded
    as a bridge alert. Any other HTTP error propagates untouched: a 401 or a 502 is
    an outage, and hiding it here is how the original defect survived.
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
