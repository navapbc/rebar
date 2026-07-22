"""The cheap re-sign path for a plan-review attestation (ticket middle-actinium-thrush).

A ``rebar review-plan`` that computes a signable PASS but whose SIGN step fails (recorded
as ``signature.signed=False`` + ``error``) leaves the expensive verdict WITHOUT the durable
product the claim gate consumes — the HMAC attestation. Re-running the full multi-pass LLM
review to recover it is ~10 minutes of billable work for a result already computed and
persisted in the ``REVIEW_RESULT`` sidecar.

:func:`resign_plan_review` is the recovery: it reads the LATEST persisted ``REVIEW_RESULT``
sidecar (NO LLM, NO network), verifies the recorded verdict is a signable PASS AND that the
plan/material has not changed since the review (the sidecar's recorded material fingerprint
still equals the freshly-recomputed one), reconstructs the minimal verdict, and calls
:func:`attest.sign_plan_review` to persist the SAME attestation a normal signing PASS would
have written — so a subsequent ``claim`` passes the gate.

STALENESS GUARD: the recorded fingerprint must equal ``current_material_fingerprint`` NOW.
If the plan drifted the old verdict is stale, so we REFUSE (and tell the user to run a full
``rebar review-plan``) rather than sign a verdict that no longer describes the plan.

Optionality: stdlib + core signing only (the sidecar reader, the attestation machinery, and
``current_material_fingerprint`` are all import-light) — it does NOT need the ``[agents]``
extra or a model key, because it never runs the LLM tiers.
"""

from __future__ import annotations

import logging
from typing import Any

from . import attest, sidecar

logger = logging.getLogger(__name__)


def resign_plan_review(ticket_id: str, *, repo_root=None) -> dict[str, Any]:
    """Cheaply (re)persist the plan-review attestation for an ALREADY-COMPUTED, still-valid
    PASS verdict — WITHOUT re-running the multi-pass LLM review.

    Returns a result dict ``{ok, signed, ticket_id, verdict, reason, signature?}``:

    * ``ok=True`` (``signed=True``) — the latest ``REVIEW_RESULT`` sidecar records a PASS whose
      material fingerprint still matches the current plan; the attestation was re-signed and the
      claim gate now passes.
    * ``ok=False`` (``signed=False``) — REFUSED, with a ``reason``: no sidecar at all, the latest
      sidecar is not a signable PASS (BLOCK / INDETERMINATE / degraded), or the plan changed since
      the review (stale — run a full ``rebar review-plan``). NEVER signs a non-PASS / degraded /
      stale verdict.

    NO LLM and NO network — a sidecar read, a light fingerprint recompute, and a local HMAC sign.
    """
    payload = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
    if payload is None:
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": None,
            "reason": (
                "no REVIEW_RESULT sidecar found for this ticket — run `rebar review-plan` "
                "to produce (and sign) a plan-review verdict"
            ),
        }

    recorded_verdict = str(payload.get("verdict") or "").upper()
    coverage = payload.get("coverage") or {}
    # Never-sign guard (mirrors attest.sign_plan_review): only a clean PASS with no
    # systemic-degrade resolution_class is a certifiable result. A non-PASS / degraded sidecar
    # is refused up-front with a clear message (sign_plan_review would raise on it anyway).
    if recorded_verdict != "PASS" or coverage.get("resolution_class"):
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict or None,
            "reason": (
                f"the latest review was not a signable PASS (verdict={recorded_verdict or 'n/a'}"
                + (
                    f", resolution_class={coverage.get('resolution_class')!r}"
                    if coverage.get("resolution_class")
                    else ""
                )
                + ") — run `rebar review-plan` to produce a fresh verdict"
            ),
        }

    try:
        from . import generation

        initial_generation = generation.collect(ticket_id, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 - recovery remains a structured no-throw API
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": f"plan review generation could not be collected: {exc}",
        }

    # STALENESS GUARD: the plan/material must not have changed since the review. Recompute the
    # current material fingerprint (NO LLM) and require it to equal the sidecar's recorded one.
    recorded_material = payload.get("material_fingerprint")
    current_material = initial_generation.own_material
    if recorded_material is None or current_material is None:
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": (
                "could not compare the plan's material fingerprint against the recorded review "
                "(missing/unreadable) — run `rebar review-plan` to re-review and sign"
            ),
        }
    if recorded_material != current_material:
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": (
                "the plan changed since the review (description/AC/file_impact/children edited), "
                "so the recorded PASS is stale — run `rebar review-plan` to re-review and sign"
            ),
        }

    try:
        phase_metadata = sidecar.parse_review_phase_metadata(payload)
        from .pin_health import review_phase_status

        phase_status = review_phase_status(
            initial_generation.phase,
            phase_metadata["phase"],
            phase_metadata["priority_floor"],
        )
    except sidecar.SidecarReviewPhaseError:
        phase_metadata = {"phase": "planning", "priority_floor": None}
        phase_status = "malformed"
    except Exception:  # noqa: BLE001 - unreadable current state cannot authorize recovery
        phase_metadata = {"phase": "planning", "priority_floor": None}
        phase_status = "malformed"
    if phase_status != "compatible":
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": f"review phase metadata is {phase_status}; run `rebar review-plan`",
            "health": {"phase_status": phase_status},
        }

    enforced = attest._read_enforce_plan_material_pins(repo_root)
    try:
        reviewed_pins = sidecar.parse_reviewed_related_material(payload)
        pin_health = attest.derive_plan_material_pin_health(
            reviewed_pins, repo_root=repo_root, enforced=enforced
        )
    except sidecar.ReviewedRelatedMaterialError:
        pin_health = {"pin_status": "malformed-pin", "enforced": enforced, "targets": []}
    if enforced and pin_health["pin_status"] not in ("current", "legacy-unpinned"):
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": pin_health["pin_status"],
            "reason": (
                "reviewed related-ticket material is no longer valid "
                f"({pin_health['pin_status']}) — run `rebar review-plan` to re-review and sign"
            ),
            "health": pin_health,
        }

    # Reconstruct the minimal verdict the attestation binds. The sidecar slims finding CITATIONS
    # out, so dependency scoping falls to the ticket's current file_impact (dependency_hashes reads
    # it from the store) hashed at the current code — the recovery attestation binds current code,
    # exactly what the claim gate re-checks. counts/model/runner ride from the sidecar.
    verdict: dict[str, Any] = {
        "verdict": "PASS",
        "ticket_id": payload.get("ticket_id") or ticket_id,
        "ticket_type": payload.get("ticket_type"),
        "model": payload.get("model"),
        "runner": payload.get("runner"),
        "coverage": coverage,
    }
    try:
        sig = attest.sign_plan_review(
            verdict,
            material=current_material,
            review_phase=phase_metadata["phase"],
            priority_floor=phase_metadata["priority_floor"],
            initial_generation=initial_generation,
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001 — public recovery path returns structured refusal
        # Relation failures are an unsigned, retry-after-repair gate outcome, not
        # an opaque signing failure.  Keep the public no-throw recovery contract
        # while preserving the stable reason/reference fields used by CLI/MCP.
        from .relation_snapshot import PlanRelationSnapshotError

        if isinstance(exc, PlanRelationSnapshotError):
            record = {
                "event": "plan_relation_snapshot_error",
                "reason": exc.reason,
                "canonical_id": exc.canonical_id,
                "reference": exc.reference,
            }
            logger.error("plan relation snapshot failed: %s", record, extra=record)
            return {
                "ok": False,
                "signed": False,
                "ticket_id": ticket_id,
                "verdict": "INDETERMINATE",
                "reason": (
                    "repair or remove the unreadable plan relationship, then rerun "
                    "`rebar review-plan`; no attestation was signed"
                ),
                "plan_relation_snapshot_error": record,
            }
        logger.warning("cheap re-sign failed to persist the attestation", exc_info=True)
        return {
            "ok": False,
            "signed": False,
            "ticket_id": ticket_id,
            "verdict": recorded_verdict,
            "reason": f"the attestation could not be persisted: {exc}",
        }
    return {
        "ok": True,
        "signed": True,
        "ticket_id": ticket_id,
        "verdict": "PASS",
        "reason": "re-signed the plan-review attestation from the latest REVIEW_RESULT sidecar "
        "(no LLM review re-run)",
        "signature": {
            "signed": True,
            "key_id": sig.get("key_id"),
            "head_sha": sig.get("head_sha"),
        },
    }
