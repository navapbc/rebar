"""The dedicated gate ERROR sidecar (ticket 8bc5).

When an LLM gate hits an INFRASTRUCTURE exception (:class:`LLMUnavailableError`), we
additively persist a dedicated ``gate_error_v1`` sidecar record so an
env/integration-diagnosis interval is captured BY GATE CODE — the failure is otherwise
ephemeral (surfaced once on stderr and lost). This is strictly ADDITIVE: it NEVER changes
any gate's existing outcome (plan/code review still soft-degrade to INDETERMINATE;
completion still fail-closes by re-raising).

The record rides the SAME event stream as the gate's normal sidecar — ``REVIEW_RESULT``
for plan-review/code-review, ``COMPLETION_VERDICT`` for completion — tagged with a
distinct ``schema`` (``"gate_error_v1"``). Because this is a SEPARATE builder it NEVER
routes through :func:`rebar.llm.completion.reconcile_verdict` (which coerces any non-PASS
verdict to FAIL), so the ``"ERROR"`` verdict survives verbatim. Every existing verdict
reader is schema-guarded and SKIPS a foreign schema, so this new record is invisible to
them — no new event type, back-compatible and reducer-ignored like its host stream.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

GATE_ERROR_SCHEMA = "gate_error_v1"

# A gate name -> the event stream its sidecar records already ride on. Reusing the host
# stream (rather than minting a new event type) keeps this back-compatible: the type is
# already reducer-ignored, non-replay, and in the write allow-list.
_GATE_EVENT_TYPE = {
    "plan_review": "REVIEW_RESULT",
    "code_review": "REVIEW_RESULT",
    "completion": "COMPLETION_VERDICT",
}


def build_gate_error_payload(
    gate: str, *, cause: str, evidence_ref: str | None = None
) -> dict[str, Any]:
    """The dedicated ERROR payload: ``verdict == "ERROR"`` + an ``error{cause, evidence_ref}``
    object, tagged ``schema == "gate_error_v1"``. Deliberately a SEPARATE builder from the
    verdict sidecars so it never touches ``reconcile_verdict`` (which would coerce ERROR→FAIL)."""
    return {
        "schema": GATE_ERROR_SCHEMA,
        "verdict": "ERROR",
        "gate": gate,
        "error": {"cause": cause, "evidence_ref": evidence_ref},
    }


def emit_gate_error(
    ticket_id: str,
    gate: str,
    *,
    cause: str,
    evidence_ref: str | None = None,
    repo_root=None,
) -> bool:
    """Append a ``gate_error_v1`` sidecar record for ``ticket_id`` onto ``gate``'s host event
    stream. Returns True on success, False on any failure — best-effort observability, so a
    failed emit must NEVER change the gate's outcome (the caller degrades / re-raises
    regardless). Broad-but-logged, mirroring the verdict sidecars' emit posture."""
    from rebar import config as _config
    from rebar._commands._seam import append_event

    try:
        event_type = _GATE_EVENT_TYPE[gate]
        tracker = _config.tracker_dir(repo_root)
        payload = build_gate_error_payload(gate, cause=cause, evidence_ref=evidence_ref)
        append_event(ticket_id, event_type, payload, tracker, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — best-effort observability sidecar; broad-but-logged, never changes the gate outcome
        logger.warning("gate_error sidecar emit failed; continuing", exc_info=True)
        return False
    return True
