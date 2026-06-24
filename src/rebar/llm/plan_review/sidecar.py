"""The ``REVIEW_RESULT`` observability sidecar (child db7b).

Every plan review emits a ``REVIEW_RESULT`` event capturing the per-criterion
verdicts + finding fingerprints + metadata, so per-criterion FP / remediation
analysis can be reconstructed OFFLINE without taxing rebar's hot paths.

It is a **reducer-ignored** sidecar: ``REVIEW_RESULT`` is NOT in
``KNOWN_EVENT_TYPES``, so the reducer skips it (it never enters compiled state,
deps, validate, or the close/claim hot paths) and compaction PRESERVES it
(forward-compat payload, never absorbed into a SNAPSHOT). It IS in the write-path
allow-list (so it can be emitted) and in ``_NON_REPLAY_KNOWN_TYPES`` (so ``fsck``
recognises it and does not warn "newer than me"). This mirrors the SYNC /
PRECONDITIONS precedent. Like every event it follows the
preserved-and-ignored-by-older-clones rollout (upgrade reconcile hosts first).
"""

from __future__ import annotations

from typing import Any

EVENT_TYPE = "REVIEW_RESULT"


def emit(verdict: dict[str, Any], *, material: str | None = None, repo_root=None) -> bool:
    """Append a ``REVIEW_RESULT`` sidecar event from a plan-review verdict. Returns
    True on success, False on any failure (the sidecar is observability — a failed
    emit must NEVER fail the review itself). Best-effort by design."""
    from rebar import config as _config
    from rebar._commands._seam import append_event

    try:
        tracker = _config.tracker_dir(repo_root)
        payload = build_payload(verdict, material=material)
        append_event(verdict["ticket_id"], EVENT_TYPE, payload, tracker, repo_root=repo_root)
        return True
    except Exception:
        return False


def build_payload(verdict: dict[str, Any], *, material: str | None = None) -> dict[str, Any]:
    """The sidecar payload: per-finding fingerprints + decisions + verification
    attributes (everything needed to reconstruct per-criterion FP/remediation rates
    offline by joining on ticket_id + finding id), plus the coverage record and the
    full advisory OVERFLOW + DROPPED sets (which are not surfaced to the agent but
    are retained here for analysis)."""

    def _slim(f: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f.get("id"),
            "criteria": f.get("criteria", []),
            "tier": f.get("tier"),
            "decision": f.get("decision"),
            "severity": f.get("severity"),
            "validity": f.get("validity"),
            "impact": f.get("impact"),
            "priority": f.get("priority"),
            "reason": f.get("reason"),
            "verification": f.get("verification"),
        }

    all_findings = (
        verdict.get("blocking", [])
        + verdict.get("advisory", [])
        + verdict.get("overflow", [])
        + verdict.get("indeterminate", [])
        + verdict.get("dropped", [])
    )
    return {
        "schema": "plan_review_result_v1",
        "verdict": verdict.get("verdict"),
        "ticket_id": verdict.get("ticket_id"),
        "ticket_type": verdict.get("ticket_type"),
        "material_fingerprint": material,
        "model": verdict.get("model"),
        "runner": verdict.get("runner"),
        "coverage": verdict.get("coverage", {}),
        "findings": [_slim(f) for f in all_findings],
        "coaching": [
            {"move_id": c.get("move_id"), "finding_refs": c.get("finding_refs", [])}
            for c in verdict.get("coaching", [])
        ],
    }
