#!/usr/bin/env python3
"""Transition replay fallback for invalid direct Jira transitions (story S6).

A Jira workflow may forbid the DIRECT end-state transition rebar wants (e.g.
``open -> done`` when only ``open -> in_progress -> done`` is allowed). The outbound
scalar update then fails and ``dispatch_one._update_one_scalar_update`` turns the
failure into comment-fallback + drift. This module adds a *grounded* replay: mirror
rebar's own recorded status hops — the append-only ``*-STATUS.json`` events
``rebar._commands.txn`` writes — to reach the end state via the allowed intermediate
hops, aborting to the existing comment-fallback the moment any hop is rejected.
Self-healing (inventing hops rebar never took) is OUT of scope.

The whole comparison lives in **Jira-name space**. ``LOCAL_STATUS_TO_JIRA`` is
NON-INJECTIVE (``In Progress`` <- {in_progress, blocked}; ``Done`` <- {closed,
cancelled, deleted}), so it has no inverse and is NEVER inverted here: recorded local
hops are forward-mapped to Jira names, the issue's current ``fields.status.name`` (a
Jira NAME) is matched directly against that forward-mapped sequence, and replay walks
the remaining Jira-name hops through the existing ``transition_issue_by_name`` seam.

Self-resolves the store via ``config.tracker_dir(config.reconciler_repo_root())`` — no
``repo_root``/``tracker`` parameter is threaded through the call chain.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from pathlib import Path

from rebar.config import reconciler_repo_root, tracker_dir

from ._backend import BackendHTTPError, TicketTransport
from ._errors import JiraAPIError

# The local-status -> Jira-workflow-name map, taken from the reconciler's OWN
# parity-checked copy (``rebar_reconciler.config.local_to_jira_status``) rather than the
# ``adapters/jira_family`` vendor layer: the import-graph contract
# (``test_jira_family_boundary``) forbids a core engine module from consuming the
# Jira-family layer, and this in-layer literal is kept in lock-step with it by test.
from .config import local_to_jira_status as LOCAL_STATUS_TO_JIRA

logger = logging.getLogger(__name__)

# The union both named backend implementors raise when a hop is rejected: acli Cloud
# raises ``RuntimeError`` (no transition reaches the target); Data Center raises
# ``ValueError`` (no such transition) and its ``IllegalTransitionError`` subclass
# (illegal from the current state); a 400 on the POST surfaces as
# ``urllib.error.HTTPError`` (covers ``BackendHTTPError``) or ``JiraAPIError``.
_HOP_REJECTIONS: tuple[type[BaseException], ...] = (
    RuntimeError,
    ValueError,
    urllib.error.HTTPError,
    BackendHTTPError,
    JiraAPIError,
)


def recorded_status_hops(local_id: str, tracker: str | Path) -> list[str]:
    """Ordered Jira-name status hops rebar recorded for *local_id*.

    Globs ``<tracker>/<local_id>/*-STATUS.json``, sorts by filename (chronological — the
    order ``reduce_ticket`` replays in), and returns each event's ``data["status"]``
    (a LOCAL status) FORWARD-mapped to its Jira workflow name via
    ``LOCAL_STATUS_TO_JIRA``. An unmappable local status is skipped. e.g.
    ``["To Do", "In Progress", "Done"]``.
    """
    ticket_dir = Path(tracker) / local_id
    hops: list[str] = []
    for path in sorted(ticket_dir.glob("*-STATUS.json"), key=lambda p: p.name):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data = event.get("data") if isinstance(event, dict) else None
        local_status = data.get("status") if isinstance(data, dict) else None
        jira_name = LOCAL_STATUS_TO_JIRA.get(local_status) if local_status else None
        if jira_name is not None:
            hops.append(jira_name)
    return hops


def replay_should_drift(local_id: str | None) -> bool:
    """Decide, for a DIRECT transition that failed and could not be replayed to the target,
    whether it should DRIFT (comment-fallback, ``result`` None) rather than PROPAGATE the
    original error to the pre-S6 per-mutation soft-fail backstop.

    * Falsy ``local_id`` -> ``True``: with no ticket reference there is nothing to replay
      against, so the failure drifts exactly as a pre-S6 illegal-transition ``JiraAPIError``
      did (it is never re-raised).
    * ``local_id`` present WITH a recorded ``*-STATUS.json`` trail -> ``True``: replay was
      applicable (it either aborted at a rejected hop or the current status was not in the
      recorded sequence), so the mutation drifts.
    * ``local_id`` present WITHOUT any recorded trail -> ``False``: replay was never
      applicable, so the original transition error propagates unchanged — preserving the
      bare-``RuntimeError`` soft-fail contract (``acli`` "no transition reaches …").
    """
    if not local_id:
        return True
    tracker = tracker_dir(reconciler_repo_root())
    return bool(recorded_status_hops(local_id, tracker))


def replay_transition(
    client: TicketTransport, remote_id: str, local_id: str | None, target_status: str
) -> bool:
    """Replay rebar's recorded intermediate hops to reach *target_status*.

    Returns ``True`` only if every remaining hop after the resume point succeeds;
    ``False`` (skip / abort -> caller's comment-fallback + drift) when:

    * ``local_id`` is falsy, or no ``*-STATUS.json`` hops are recorded (nothing to
      replay against);
    * the issue's current ``fields.status.name`` does not positionally match the
      forward-mapped hop list (on multiple matches, anchors on the LAST occurrence —
      Jira is at its most-recently-synced state);
    * any hop raises one of the ``_HOP_REJECTIONS`` (no partial hop is retried).
    """
    if not local_id:
        return False

    tracker = tracker_dir(reconciler_repo_root())
    hops = recorded_status_hops(local_id, tracker)
    if not hops:
        return False

    current = client.get_issue(remote_id)["fields"]["status"]["name"]

    resume_index = None
    for i, name in enumerate(hops):
        if name == current:
            resume_index = i
    if resume_index is None:
        logger.info(
            "replay_transition: current status %r for %s not in recorded hops %r; "
            "aborting to drift",
            current,
            remote_id,
            hops,
        )
        return False

    remaining = hops[resume_index + 1 :]
    logger.info(
        "replay_transition: replaying %d hop(s) for %s from %r toward %r: %r",
        len(remaining),
        remote_id,
        current,
        target_status,
        remaining,
    )
    for jira_name in remaining:
        try:
            client.transition_issue_by_name(remote_id, jira_name)
        except _HOP_REJECTIONS as exc:
            logger.info(
                "replay_transition: hop %r for %s rejected (%s); aborting to drift",
                jira_name,
                remote_id,
                type(exc).__name__,
            )
            return False
        logger.debug("replay_transition: hop %r for %s succeeded", jira_name, remote_id)
    return True
