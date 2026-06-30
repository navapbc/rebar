"""Code-review verdict sidecar (epic b744 / WS4).

Plan-review's ``sidecar.emit`` anchors on ``verdict['ticket_id']`` — but code review reviews a
DIFF, which has no ticket. So this emit takes an EXPLICIT ``target_ticket`` and writes a
``REVIEW_RESULT`` event (the same event TYPE plan-review uses) on it, with a code-review payload.
It is called by ``produce_code_review_verdict`` ONLY when a ``target_ticket`` is supplied (e.g. a
ticket-scoped review, or WS6's Gerrit path); the diff-only path emits no event (the verdict dict
is the artifact). Best-effort: a failure never breaks the gate.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

EVENT_TYPE = "REVIEW_RESULT"
SCHEMA = "code_review_result_v1"


def build_payload(verdict: dict[str, Any], *, target_ticket: str) -> dict[str, Any]:
    """A slim sidecar payload from a code_review_verdict (verdict + counts + coverage + the
    findings/coaching), tagged with its schema + the anchor ticket."""
    return {
        "schema": SCHEMA,
        "verdict": verdict.get("verdict"),
        "ticket_id": target_ticket,
        "runner": verdict.get("runner"),
        "model": verdict.get("model"),
        "coverage": verdict.get("coverage", {}),
        "blocking": verdict.get("blocking", []),
        "advisory": verdict.get("advisory", []),
        "coaching": verdict.get("coaching", []),
    }


def emit(verdict: dict[str, Any], *, target_ticket: str, repo_root=None) -> bool:
    """Append a ``REVIEW_RESULT`` sidecar event for ``verdict`` on ``target_ticket``. Idempotent
    per (verdict-identity) is not attempted here (a code review is diff-scoped, not run-keyed);
    callers emit once per produced verdict. Returns True on success, False on any failure
    (best-effort — never raises into the gate)."""
    if not target_ticket:
        return False
    try:
        from rebar import config as _config
        from rebar._commands._seam import append_event

        tracker = _config.tracker_dir(repo_root)
        payload = build_payload(verdict, target_ticket=target_ticket)
        append_event(target_ticket, EVENT_TYPE, payload, tracker, repo_root=repo_root)
        return True
    except Exception:  # noqa: BLE001 — sidecar is best-effort; a failure must not fail the gate
        logger.warning("code-review REVIEW_RESULT sidecar emit failed; continuing", exc_info=True)
        return False
