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

import hashlib
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

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
    except Exception:  # noqa: BLE001 — best-effort observability sidecar; broad-but-logged below, never fails the review
        # Observability floor: the sidecar is best-effort observability — a failed emit
        # must never fail the review, but the failure itself is a real signal worth a
        # stderr diagnostic (broad-but-logged; see rebar._logging).
        logger.warning("REVIEW_RESULT sidecar emit failed; continuing", exc_info=True)
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
    except Exception:  # noqa: BLE001 — best-effort retention prune; broad-but-logged below, never fails the review
        # Best-effort retention; a failed prune never fails the review (sidecars are
        # reducer-ignored, so removing old ones is safe). Log the failure (floor).
        logger.warning("REVIEW_RESULT sidecar prune failed; continuing", exc_info=True)
        return 0


def latest_review_result(ticket_id: str, *, repo_root=None) -> dict[str, Any] | None:
    """Return the **most-recent** ``REVIEW_RESULT`` sidecar payload for ``ticket_id``,
    or ``None`` when none is usable.

    Contract (child e344) — this is the reader a remediation re-review uses to hand the
    Pass-2 novelty sub-call its own prior findings. It mirrors the sidecar's
    observability-only, best-effort posture and **never raises**, so a missing/garbled
    prior review degrades gracefully to "no prior findings":

    - Return value: the deserialized ``data`` payload of the latest sidecar event (the
      ``build_payload`` dict — ``schema``, ``findings``, ``coverage``, …), NOT the event
      envelope. Callers read ``result["findings"]`` directly.
    - No sidecar yet / ticket dir absent / empty dir → ``None`` (the common first-review
      case; the caller proceeds with no prior findings).
    - Unreadable or malformed JSON in the newest file → ``None`` + a logged warning.
    - **Schema guard:** a payload whose ``schema`` != ``"plan_review_result_v1"`` is
      rejected (``None``), so a future schema bump can never feed a stale shape to the
      novelty sub-call.
    """
    try:
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
        if not files:
            return None
        # Filenames are timestamp-prefixed, so the last entry is the newest review.
        import json

        with open(os.path.join(ticket_dir, files[-1]), encoding="utf-8") as fh:
            event = json.load(fh)
        payload = event.get("data") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != "plan_review_result_v1":
            return None  # schema guard: never feed a stale/foreign shape downstream
        return payload
    except FileNotFoundError:
        return None  # ticket dir / file absent → no prior review (common first-review case)
    except Exception:  # noqa: BLE001 — best-effort observability reader; broad-but-logged, never raises
        logger.warning(
            "REVIEW_RESULT sidecar read failed; treating as no prior review", exc_info=True
        )
        return None


# ── normalized finding fingerprint (OBSERVABILITY-ONLY — sidecar payload, never the
#    surfaced verdict) ──────────────────────────────────────────────────────────────
# The caller-visible finding ``id`` (orchestrator.mint_finding_id) hashes the EXACT
# finding text, so the Pass-1 finder re-wording the same defect on a re-review mints a
# DIFFERENT id — which makes "did this finding survive a revision?" unmeasurable from the
# exact id alone (it reads as ~100% resolved for every LLM criterion, vs the deterministic
# floor where the text is stable). ``norm_id`` is a coarser, reword-tolerant fingerprint
# (significant-token set + criteria, order-insensitive) so offline calibration can join the
# SAME defect across re-reviews at a granularity finer than criterion-load-delta. It is
# additive to the sidecar event ONLY — the surfaced verdict findings are untouched, so the
# library / MCP / CLI return shape does not change.
_NORM_STOP_TOKENS = 3  # drop tokens this short or shorter as low-signal noise


def norm_id(finding: dict[str, Any]) -> str:
    """A reword-tolerant, criterion-scoped content fingerprint for a finding: the SORTED
    SET of its significant lowercased alphanumeric tokens joined with its sorted criteria.
    Order-insensitive and resilient to minor re-phrasing, so the same underlying defect
    across re-reviews tends to mint the same ``norm_id`` (unlike the exact-text ``id``)."""
    text = str(finding.get("finding", "")).lower()
    tokens = sorted({t for t in re.findall(r"[a-z0-9]+", text) if len(t) > _NORM_STOP_TOKENS})
    basis = " ".join(tokens) + "|" + ",".join(sorted(finding.get("criteria", []) or []))
    return "n" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def build_payload(verdict: dict[str, Any], *, material: str | None = None) -> dict[str, Any]:
    """The sidecar payload: per-finding fingerprints + decisions + verification
    attributes (everything needed to reconstruct per-criterion FP/remediation rates
    offline by joining on ticket_id + finding id), plus the coverage record and the
    full advisory OVERFLOW + DROPPED sets (which are not surfaced to the agent but
    are retained here for analysis)."""

    def _slim(f: dict[str, Any]) -> dict[str, Any]:
        # Field-selection principle (child e344): persist the PROSE a remediation
        # re-review's Pass-2 novelty sub-call needs to re-ground itself against the
        # prior findings (``finding`` / ``suggested_fix`` / ``checklist_item``) plus the
        # fingerprints/decision/verification needed for offline calibration — but
        # deliberately exclude runtime-only carriers (e.g. ``scenarios``, ``evidence``,
        # ``_agentic``) to keep the sidecar lean. As the finding schema grows, add a key
        # here only when an offline consumer (calibration or re-grounding) needs it.
        return {
            "id": f.get("id"),
            # OBSERVABILITY-ONLY enrichment (db7b follow-on): a reword-tolerant fingerprint
            # + the finding's location, so the voluntary-revision signal is cleanly joinable
            # across re-reviews offline. Not surfaced to the agent (sidecar event only).
            "norm_id": norm_id(f),
            "location": f.get("location", ""),
            "criteria": f.get("criteria", []),
            "tier": f.get("tier"),
            "decision": f.get("decision"),
            "severity": f.get("severity"),
            "validity": f.get("validity"),
            "impact": f.get("impact"),
            "priority": f.get("priority"),
            "reason": f.get("reason"),
            "verification": f.get("verification"),
            # Finding PROSE (child e344): re-grounding the Pass-2 novelty sub-call on a
            # remediation re-review needs the prior finding's actual text — not just its
            # fingerprint — to answer the matches-prior sub-answers. Sidecar event ONLY;
            # the surfaced verdict shape is byte-unchanged (asserted in tests).
            "finding": f.get("finding", ""),
            "suggested_fix": f.get("suggested_fix", ""),
            "checklist_item": f.get("checklist_item", ""),
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
