"""Code-review verdict sidecar (epic b744 / WS4).

Plan-review's ``sidecar.emit`` anchors on ``verdict['ticket_id']`` — but code review reviews a
DIFF, which has no ticket. So this emit takes an EXPLICIT ``target_ticket`` and writes a
``REVIEW_RESULT`` event (the same event TYPE plan-review uses) on it, with a code-review payload.
It is called by ``produce_code_review_verdict`` ONLY when a ``target_ticket`` is supplied (e.g. a
ticket-scoped review, or WS6's Gerrit path); the diff-only path emits no event (the verdict dict
is the artifact). Best-effort: a failure never breaks the gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

EVENT_TYPE = "REVIEW_RESULT"
SCHEMA = "code_review_result_v1"
# The impact-model formula version that produced this sidecar's scores (story
# raptorial-galloping-dragon). Stamped top-level so the calibration replay SEGMENTS old-formula vs
# new-formula findings and never pools across versions. Bump on any impact_code shape change.
IMPACT_MODEL_VERSION = "code-v2"


def change_fingerprint(
    change_id: str, revision: str, changed_files: list[str], diff_text: str
) -> str:
    """A stable diff-scoped join key for a code-review artifact (story limestone).

    The plan-review ``material_fingerprint(ctx: PlanContext)`` is ticket-scoped (ticket_id /
    description / file_impact / children) and a diff has no PlanContext, so this is a small NEW
    analogue with the SAME construction — a sha256 over a sorted-key JSON basis, 16-hex prefix —
    keyed by the CHANGE (gerrit change-id + revision + the sorted changed-file set + a hash of the
    diff text) instead of a ticket. Per-finding identity still reuses
    ``plan_review.sidecar.norm_id`` verbatim (a code-review finding shares the finding/criteria
    shape norm_id reads)."""
    basis = {
        "change_id": change_id or "",
        "revision": revision or "",
        "changed_files": sorted(changed_files or []),
        "diff_sha": hashlib.sha256((diff_text or "").encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _with_norm_ids(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp each finding with its reword-tolerant ``norm_id`` (reused from plan-review)."""
    from rebar.llm.plan_review.sidecar import norm_id

    out = []
    for f in findings or []:
        if isinstance(f, dict):
            out.append({**f, "norm_id": norm_id(f)})
        else:
            out.append(f)
    return out


def build_payload(
    verdict: dict[str, Any],
    *,
    target_ticket: str,
    change_id: str = "",
    revision: str = "",
    change_fp: str = "",
) -> dict[str, Any]:
    """A slim sidecar payload from a code_review_verdict (verdict + counts + coverage + the
    findings/coaching), tagged with its schema + the anchor ticket. When change metadata is supplied
    (the reviewbot artifact path) the payload also carries the ``(change_id, revision)`` key, the
    diff-scoped ``change_fingerprint``, and per-finding ``norm_id``s — the join keys a calibration
    corpus needs."""
    return {
        "schema": SCHEMA,
        "impact_model_version": IMPACT_MODEL_VERSION,
        "verdict": verdict.get("verdict"),
        "ticket_id": target_ticket,
        "change_id": change_id,
        "revision": revision,
        "change_fingerprint": change_fp,
        "runner": verdict.get("runner"),
        "model": verdict.get("model"),
        "coverage": verdict.get("coverage", {}),
        "blocking": _with_norm_ids(verdict.get("blocking", [])),
        "advisory": _with_norm_ids(verdict.get("advisory", [])),
        "coaching": verdict.get("coaching", []),
    }


def emit(
    verdict: dict[str, Any],
    *,
    target_ticket: str,
    repo_root=None,
    change_id: str = "",
    revision: str = "",
    change_fp: str = "",
) -> bool:
    """Append a ``REVIEW_RESULT`` sidecar event for ``verdict`` on ``target_ticket``. Idempotency
    per (verdict-identity) is not attempted here (a code review is diff-scoped, not run-keyed);
    callers emit once per produced verdict. When change metadata is supplied (the reviewbot
    artifact path) the payload carries the ``(change_id, revision)`` key + ``change_fingerprint``.
    Returns True on success, False on any failure (best-effort — never raises into the gate)."""
    if not target_ticket:
        return False
    try:
        from rebar import config as _config
        from rebar._commands._seam import append_event

        tracker = _config.tracker_dir(repo_root)
        payload = build_payload(
            verdict,
            target_ticket=target_ticket,
            change_id=change_id,
            revision=revision,
            change_fp=change_fp,
        )
        append_event(target_ticket, EVENT_TYPE, payload, tracker, repo_root=repo_root)
        return True
    except Exception:  # noqa: BLE001 — sidecar is best-effort; a failure must not fail the gate
        logger.warning("code-review REVIEW_RESULT sidecar emit failed; continuing", exc_info=True)
        return False
