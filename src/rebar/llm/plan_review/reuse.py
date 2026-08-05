"""Review-reuse short-circuits for the plan-review gate.

Two sibling paths that answer "is a re-review of this UNCHANGED ticket pure waste?"
before the billable multi-pass LLM review runs (extracted from ``__init__`` along the
existing call seam — ``_run_plan_review`` calls both, in order):

* :func:`idempotent_reuse` (feature b3e5) — the PASS path: reuse a still-valid
  **certified attestation** (the exact validity the claim gate consumes).
* :func:`verdict_reuse` (bug 7e77) — the BLOCK path: a BLOCK never signs, so the
  attestation path can never fire for it; instead reuse the stored BLOCK verdict from
  the latest ``REVIEW_RESULT`` sidecar when its ``material_fingerprint`` AND
  ``verified_at_sha`` both still match the current plan/code (the same
  sidecar-baseline comparison as ``remediation_mode._sidecar_branch_decision``).

Both are read-only (no LLM, no new sidecar, no re-sign) and both are bypassed by
``--force``. Alternative rejected (7e77): signing BLOCK attestations would complicate
the claim gate's PASS-only trust model for no claim-side benefit.
"""

from __future__ import annotations

import logging
from typing import Any

from . import attest, sidecar
from .attest import claim_gate_check

logger = logging.getLogger(__name__)

__all__ = ["idempotent_reuse", "verdict_reuse"]


def _type_changed(stored_type: Any, ctx) -> bool:
    """True when the ticket's type has changed since the stored verdict was computed.

    Type is NOT cosmetic to a review: it selects which criteria apply at all
    (``orchestrator.py:364`` and ``:642``, ``workflow_ops.py:167`` and ``:183``), so a
    PASS earned as a ``bug`` — exempt from several criteria — must not be replayed for
    a ``task``, which is not exempt.

    FAILS OPEN by design: both sides must be truthy to report a change. A sidecar
    written before this check existed carries no usable type, and invalidating every
    such attestation would force a fresh review of every already-certified ticket. Those
    re-earn their type binding the next time the plan changes, which is the deliberate
    trade — see the "Rejected" note on ticket 6e4f-cad1-aa39-446a for why the material
    fingerprint was left alone instead.
    """
    current_type = getattr(ctx, "ticket_type", "")
    return bool(stored_type) and bool(current_type) and stored_type != current_type


def idempotent_reuse(ticket_id: str, ctx, *, repo_root) -> dict[str, Any] | None:
    """Idempotence short-circuit (feature b3e5): reuse an existing, still-valid plan-review
    attestation INSTEAD of re-running the billable multi-pass LLM review, when the ticket is
    UNCHANGED. Returns a well-formed PASS ``plan_review_verdict`` (``coverage.llm_ran=False``,
    ``coverage.idempotent_skip=True``, ``signature`` mirroring the current attestation) when
    reuse is safe, else ``None`` (→ a full review runs).

    Validity is decided by :func:`claim_gate_check` — the EXACT check the ``claim`` gate
    consumes (a certified plan-review attestation that binds the current material fingerprint,
    whose reviewed code has not drifted, whose criteria-registry stamp still matches, and which
    post-dates any reopen). So the skip fires precisely when a claim would already pass, and it
    cannot weaken the gate. No LLM / no network beyond the same local reads the claim gate does;
    it never re-signs and never re-emits a sidecar (the attestation is already current)."""
    from rebar import signing
    from rebar.llm import findings

    gate = claim_gate_check(ticket_id, repo_root=repo_root)
    if not gate.get("ok"):
        return None
    try:
        sig = signing.verify_signature(ticket_id, kind=attest._MANIFEST_PREFIX, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — cannot read the attestation record → run a full review
        return None
    # claim_gate_check owns every other validity question; it binds the material
    # fingerprint, the reviewed code SHA and the registry stamp, none of which move
    # when only the ticket's type changes. The stored type lives on the sidecar.
    try:
        prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root) or {}
    except Exception:  # noqa: BLE001 — cannot read the sidecar → fail open, keep reusing
        prior = {}
    if _type_changed(prior.get("ticket_type"), ctx):
        logger.info(
            "plan review NOT reused for %s: ticket type changed %r -> %r since the "
            "stored verdict; re-running under the criteria now in force",
            ticket_id,
            prior.get("ticket_type"),
            getattr(ctx, "ticket_type", ""),
        )
        return None

    material = attest.current_material_fingerprint(ticket_id, repo_root=repo_root)
    verdict: dict[str, Any] = {
        "verdict": "PASS",
        "ticket_id": getattr(ctx, "ticket_id", ticket_id),
        "ticket_type": getattr(ctx, "ticket_type", ""),
        "blocking": [],
        "advisory": [],
        "coaching": [],
        "indeterminate": [],
        "material_fingerprint": material,
        "coverage": {"llm_ran": False, "idempotent_skip": True},
        "signature": {
            "signed": True,
            "key_id": sig.get("key_id"),
            "head_sha": sig.get("head_sha"),
        },
        "runner": "reused",
        "model": None,
        "sidecar_emitted": False,
    }
    logger.info(
        "plan review reused (ticket unchanged; attestation current) for %s "
        "— pass --force to re-run",
        ticket_id,
    )
    return findings.validate_structured(verdict, "plan_review_verdict")


def verdict_reuse(ticket_id: str, ctx, *, repo_root) -> dict[str, Any] | None:
    """BLOCK verdict-reuse short-circuit (bug 7e77): reuse the STORED BLOCK verdict from the
    latest ``REVIEW_RESULT`` sidecar instead of re-running the billable multi-pass LLM review,
    when NOTHING the review would read has changed. Returns a well-formed BLOCK
    ``plan_review_verdict`` (``coverage.llm_ran=False``, ``coverage.verdict_reuse=True``,
    the stored blocking/advisory findings + coaching rendered back, unsigned — a BLOCK never
    signs) when reuse is safe, else ``None`` (→ a full review runs).

    Preconditions (all must hold; the sidecar stamps both baselines on EVERY verdict for
    exactly this purpose — "a BLOCK loop leaves a usable baseline even though a BLOCK never
    signs"):

    * the latest usable sidecar verdict is ``BLOCK`` with at least one stored blocking finding;
    * its ``material_fingerprint`` equals the ticket's CURRENT material fingerprint
      (the plan is unrevised); and
    * its ``verified_at_sha`` equals the current review code SHA (the code is undrifted) —
      both sides of that comparison come from ONE rule (:func:`sidecar.review_code_sha`),
      mirroring ``remediation_mode._sidecar_branch_decision``.

    Emits NO new sidecar (reuse is a read, not a review) and is bypassed by ``--force``
    (the caller gates on it). Fail-safe: any read error → ``None`` → full review."""
    from rebar.llm import findings

    try:
        prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
        if not prior or prior.get("verdict") != "BLOCK":
            return None
        base_material = prior.get("material_fingerprint")
        base_sha = prior.get("verified_at_sha")
        if not base_material or not base_sha:
            return None
        if _type_changed(prior.get("ticket_type"), ctx):
            return None  # the ticket's type changed → different criteria → re-review
        if base_material != attest.current_material_fingerprint(ticket_id, repo_root=repo_root):
            return None  # the plan was revised → re-review
        if base_sha != sidecar.review_code_sha(repo_root):
            return None  # the reviewed code drifted → re-review
        stored = prior.get("findings") or []
        blocking = [dict(f) for f in stored if f.get("decision") == "block"]
        if not blocking:
            return None  # a BLOCK payload without blocking findings is unusable
        advisory = [dict(f) for f in stored if f.get("decision") == "advisory"]
        coaching = [dict(c) for c in prior.get("coaching") or []]
    except Exception:  # noqa: BLE001 — fail-safe: any read error → full review, never crash
        logger.warning("BLOCK verdict-reuse check failed; running a full review", exc_info=True)
        return None
    coverage = dict(prior.get("coverage") or {})
    coverage["llm_ran"] = False
    coverage["verdict_reuse"] = True
    verdict: dict[str, Any] = {
        "verdict": "BLOCK",
        "ticket_id": getattr(ctx, "ticket_id", ticket_id),
        "ticket_type": getattr(ctx, "ticket_type", ""),
        "blocking": blocking,
        "advisory": advisory,
        "coaching": coaching,
        "indeterminate": [],
        "material_fingerprint": base_material,
        "coverage": coverage,
        # A BLOCK never signs — same machine-readable reason a fresh BLOCK carries.
        "signature": {"signed": False, "reason": "BLOCK"},
        "runner": "reused",
        "model": None,
        "sidecar_emitted": False,
    }
    logger.info(
        "plan review BLOCK reused (plan and code unchanged since the stored verdict) for %s "
        "— pass --force to re-run",
        ticket_id,
    )
    return findings.validate_structured(verdict, "plan_review_verdict")
