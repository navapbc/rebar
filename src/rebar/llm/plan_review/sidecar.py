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

import os
from typing import Any

EVENT_TYPE = "REVIEW_RESULT"

# Retention bound (child db7b AC4). REVIEW_RESULT is reducer-IGNORED, so rebar's event
# COMPACTION intentionally PRESERVES it (never snapshots/absorbs a non-KNOWN type) —
# compaction therefore cannot bound its growth. A dedicated prune keeps the most-recent
# RETAIN sidecars per ticket (recent history for offline analysis; each review
# supersedes the prior, and prior runs were already captured at emit time) and removes
# older ones, bounding growth without touching the reducer/compaction hot paths.
RETAIN_PER_TICKET = 10


def emit(verdict: dict[str, Any], *, material: str | None = None, repo_root=None) -> bool:
    """Append a ``REVIEW_RESULT`` sidecar event from a plan-review verdict, then prune
    to the retention bound. Returns True on success, False on any failure (the sidecar
    is observability — a failed emit must NEVER fail the review itself). Best-effort."""
    from rebar import config as _config
    from rebar._commands._seam import append_event

    try:
        tracker = _config.tracker_dir(repo_root)
        payload = build_payload(verdict, material=material)
        append_event(verdict["ticket_id"], EVENT_TYPE, payload, tracker, repo_root=repo_root)
    except Exception:
        return False
    prune(verdict.get("ticket_id", ""), repo_root=repo_root)  # best-effort retention
    return True


def prune(ticket_id: str, *, keep: int = RETAIN_PER_TICKET, repo_root=None) -> int:
    """Bound REVIEW_RESULT growth: keep the most-recent ``keep`` sidecar events for a
    ticket (filename timestamp order) and remove older ones. Returns the count removed.
    Best-effort and exception-swallowing — a failed prune never fails the review; the
    sidecars are reducer-ignored, so removing old ones is safe (not state-bearing)."""
    try:
        import subprocess

        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_id

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_id(ticket_id, tracker) or ticket_id
        ticket_dir = os.path.join(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        old = files[: max(0, len(files) - keep)]
        if not old:
            return 0
        rels = [f"{rid}/{f}" for f in old]
        subprocess.run(["git", "-C", tracker, "rm", "-q", *rels], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                tracker,
                "commit",
                "-q",
                "--no-verify",
                "-m",
                f"prune: REVIEW_RESULT sidecar for {rid} (retain {keep})",
            ],
            check=True,
            capture_output=True,
        )
        return len(old)
    except Exception:
        return 0


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
        # Per-pass latency + cost-proxy metrics (db7b AC5), lifted from coverage for
        # easy offline join (det_ms / llm_ms / total_ms / llm_calls / claim_path).
        "metrics": (verdict.get("coverage", {}) or {}).get("metrics", {}),
        "coverage": verdict.get("coverage", {}),
        "findings": [_slim(f) for f in all_findings],
        "coaching": [
            {"move_id": c.get("move_id"), "finding_refs": c.get("finding_refs", [])}
            for c in verdict.get("coaching", [])
        ],
    }
