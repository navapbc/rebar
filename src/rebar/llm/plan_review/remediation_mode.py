"""Remediation-mode eligibility for a plan-review re-review (epic 7d43, child ec89).

Extracted from :mod:`rebar.llm.plan_review.attest` (task c6c9) as a pure move along a seam that
already existed in the call graph: ``remediation_mode_candidate`` was a leaf *within* ``attest``
(nothing else in that module called it) and ``_sidecar_branch_decision`` had exactly one caller
anywhere in the tree — ``remediation_mode_candidate``, directly above it. No body is rewritten.

``attest`` re-exports :func:`remediation_mode_candidate` (see the foot of ``attest.py``), so every
existing call site keeps reaching it as ``attest.remediation_mode_candidate`` — including
``plan_review/__init__.py`` and the ``monkeypatch.setattr(attest, "remediation_mode_candidate", …)``
seam in ``tests/unit/test_plan_review_remediation_gate.py``.

WHY THE HELPERS ARE IMPORTED INSIDE THE FUNCTION BODIES, NOT AT MODULE LEVEL
    Before the move, every free name here resolved through ``attest``'s module globals, and the
    test suite patches two of them *on the attest module object*
    (``monkeypatch.setattr(attest, "current_material_fingerprint", …)`` and
    ``monkeypatch.setattr(attest, "registry_version", …)``). A module-level
    ``from .attest import …`` would bind the pre-patch objects once at import and silently ignore
    those patches. A **function-local** ``from .attest import …`` re-reads the attribute from the
    module on every call, so the patch seam — and the resolution semantics generally — are
    preserved exactly. It also keeps this module import-cycle safe against ``attest``'s re-export.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def remediation_mode_candidate(
    ticket_id: str, *, window_minutes: int, now_ns: int | None = None, repo_root=None
) -> dict[str, Any]:
    """Return fail-safe remediation-floor eligibility and per-precondition reasons.

    Signature baselines are preferred; unsigned BLOCK loops fall back to the latest sidecar.
    Eligibility requires changed plan material, unchanged code/registry, prior finding text,
    and a review inside the configured freshness window. Read failures simply deny the mode.
    """
    from rebar import signing
    from rebar.llm.gate_context import current_code_sha

    from . import sidecar
    from .attest import (
        current_material_fingerprint,
        is_plan_review_manifest,
        manifest_material,
        manifest_regver,
        registry_version,
    )

    reasons: dict[str, bool] = {
        "signed": False,
        "plan_changed": False,
        "code_unchanged": False,
        "registry_unchanged": False,
        "prior_sidecar": False,
        "within_window": False,
    }
    # A broken precondition signal can only deny remediation mode.
    try:
        # Baseline resolution (story a850): the SIGNATURE branch is authoritative when a valid
        # certified plan-review manifest exists. BOTH no-usable-signature paths (verification
        # error; non-plan-review manifest) fall through to the SIDECAR branch — a BLOCK never
        # signs, so without the fallback the floor was inert in exactly the BLOCK-loop regime.
        manifest = None
        try:
            result = signing.verify_signature(ticket_id, repo_root=repo_root)
            manifest = result.get("manifest") if result.get("verified") else None
        except Exception:  # noqa: BLE001 — a broken signature read falls through to the sidecar branch
            manifest = None
        if not is_plan_review_manifest(manifest):
            return _sidecar_branch_decision(
                ticket_id,
                window_minutes=window_minutes,
                now_ns=now_ns,
                repo_root=repo_root,
            )
        reasons["signed"] = True

        # plan CHANGED: the current material fingerprint differs from the prior signed one.
        signed_material = manifest_material(manifest)
        current_material = current_material_fingerprint(ticket_id, repo_root=repo_root)
        reasons["plan_changed"] = (
            signed_material is not None
            and current_material is not None
            and current_material != signed_material
        )

        # code UNCHANGED: current verified_at_sha equals the prior signed one (deterministic,
        # reusing the signed snapshot ref). Both must be present and equal — a local-mode (None)
        # review on either side is not a reliable signal, so it is treated as changed.
        signed_sha = signing.verified_at_sha_from_manifest(manifest)
        current_sha = current_code_sha()
        reasons["code_unchanged"] = bool(signed_sha) and signed_sha == current_sha

        # registry UNCHANGED: the criteria-routing version equals the prior signed one
        # (overlay-aware — an activated/edited/disabled criterion is a registry change).
        reasons["registry_unchanged"] = manifest_regver(manifest) == registry_version(repo_root)

        # prior REVIEW_RESULT sidecar WITH finding text available (child e344). NOTE: this reads
        # the newest USABLE v1 payload (walk-back over malformed/foreign-schema files), whereas
        # the window below reads the newest FILE's timestamp; they can differ if the newest file
        # is unusable — benign here (both only gate eligibility, conservatively).
        # AUDIT (bug old-frilly-plankton): this is an EXISTENCE gate ("did a substantive prior
        # review run?"), NOT a novelty prior set — it never feeds findings into novelty scoring, so
        # it deliberately reads ALL findings (a review that floored everything still ran and is a
        # valid convergence anchor). Do NOT narrow this to ``surfaced_findings`` — that would change
        # eligibility semantics. The surfaced-only filter belongs only where prior findings become a
        # novelty SIGNAL (``_maybe_apply_rising_floor`` / ``prior_concerns``).
        prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
        reasons["prior_sidecar"] = bool(
            prior and any((f.get("finding") or "").strip() for f in prior.get("findings", []) or [])
        )

        # within the freshness window, measured from the last review of ANY kind (newest sidecar);
        # each review emits a sidecar, so the window RESETS on every review.
        last_ts = sidecar.latest_review_timestamp(ticket_id, repo_root=repo_root)
        if last_ts is not None:
            current_ns = now_ns if now_ns is not None else time.time_ns()
            reasons["within_window"] = (
                0 <= (current_ns - last_ts) <= window_minutes * 60 * 1_000_000_000
            )
    except Exception:
        logger.warning(
            "remediation-mode candidate check failed; treating as not eligible", exc_info=True
        )
        return {"eligible": False, "reasons": reasons, "baseline": "signature"}

    return {"eligible": all(reasons.values()), "reasons": reasons, "baseline": "signature"}


def _sidecar_branch_decision(
    ticket_id: str, *, window_minutes: int, now_ns: int | None, repo_root=None
) -> dict[str, Any]:
    """The SIDECAR-baseline eligibility branch (story a850), used only when no valid certified
    plan-review manifest exists (a BLOCK loop — a BLOCK never signs). Baselines come from the
    most recent ``REVIEW_RESULT`` payload (stamped since a850). The reasons dict has EXACTLY
    the five keys below — ``sidecar_baseline`` subsumes prior-sidecar existence, no ``signed``
    key — so ``eligible = all(reasons.values())`` cannot be structurally inert. Fail-safe:
    any read error → that precondition stays False → full review."""
    from . import sidecar
    from .attest import current_material_fingerprint, registry_version

    reasons: dict[str, bool] = {
        "sidecar_baseline": False,
        "plan_changed": False,
        "code_unchanged": False,
        "registry_unchanged": False,
        "within_window": False,
    }
    try:
        prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
        base_material = (prior or {}).get("material_fingerprint")
        base_sha = (prior or {}).get("verified_at_sha")
        base_regver = (prior or {}).get("regver")
        reasons["sidecar_baseline"] = bool(base_material and base_sha and base_regver)
        current_material = current_material_fingerprint(ticket_id, repo_root=repo_root)
        reasons["plan_changed"] = (
            base_material is not None
            and current_material is not None
            and current_material != base_material
        )
        # Both sides come from ONE rule (review_code_sha: snapshot SHA else git HEAD).
        reasons["code_unchanged"] = bool(base_sha) and base_sha == sidecar.review_code_sha(
            repo_root
        )
        reasons["registry_unchanged"] = base_regver is not None and base_regver == registry_version(
            repo_root
        )
        last_ts = sidecar.latest_review_timestamp(ticket_id, repo_root=repo_root)
        if last_ts is not None:
            current_ns = now_ns if now_ns is not None else time.time_ns()
            reasons["within_window"] = (
                0 <= (current_ns - last_ts) <= window_minutes * 60 * 1_000_000_000
            )
    except Exception:
        logger.warning("remediation sidecar-branch check failed; not eligible", exc_info=True)
        return {"eligible": False, "reasons": reasons, "baseline": "sidecar"}
    return {"eligible": all(reasons.values()), "reasons": reasons, "baseline": "sidecar"}
