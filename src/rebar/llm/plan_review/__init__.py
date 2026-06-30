"""The plan-review gate (epic 5fd2 — ``messy-moose-jig``).

A plan-review verification gate that fires at open→in_progress — the inverse of the
completion-verifier close gate. It COACHES agents toward better plans (advisory by
default in v1) and emits a signed **attestation** that a review process was followed
(a composable "rigorous agentic development vs vibe-coding" CI signal).

Two surfaces:

* :func:`review_plan` — the heavy, out-of-band capability: run the four-pass
  review against a ticket's whole plan, emit the ``REVIEW_RESULT`` sidecar, and (on
  a non-blocking PASS) sign a plan-review attestation. CLI: ``rebar review-plan``;
  write-gated MCP: ``review_plan``.
* :func:`claim_gate_check` — the FAST, local check the ``claim`` path uses when the
  gate is enabled (``verify.require_plan_review_for_claim``): a pure HMAC verify +
  freshness/material binding, NO LLM and NO network. ``--force`` bypasses it.

Optionality: stdlib-only at import (the registry/DET tier are pure Python; the LLM
passes lazy-import the runner stack). ``review_plan`` needs the ``[agents]`` extra +
a model key only to run the LLM tiers; the DET floor + attestation work without it.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar.llm.config import DEFAULT_MODEL, VERIFIER_DEFAULT_MODEL, LLMConfig
from rebar.llm.runner import Runner

from . import attest, orchestrator, sidecar
from .attest import claim_gate_check

logger = logging.getLogger(__name__)

__all__ = ["review_plan", "claim_gate_check", "registry_coverage"]


def _verifier_cfg(cfg: LLMConfig) -> LLMConfig:
    """The cfg the Pass-2 verify (and Pass-4 coach) steps run under: the decisive non-frontier
    verifier model (``VERIFIER_DEFAULT_MODEL``) UNLESS the operator EXPLICITLY chose a model
    (``cfg.model != DEFAULT_MODEL`` — i.e. ``REBAR_LLM_MODEL`` / ``[tool.rebar.llm].model`` was
    set to a non-default; any other value is an explicit choice and wins). Mirrors
    :func:`rebar.llm.completion.verify_completion`'s tuning.

    This downgrade lives here (on cfg) rather than as a static per-step ``model:`` in
    ``gates/plan-review.yaml`` because ``resolve_model`` precedence is ``step > workflow >
    cfg`` — a literal step model would ALWAYS beat the operator's cfg/env model and so could
    not honor an override. The Pass-1 finder is unaffected: it runs the YAML ``model_ladder``
    via the ProductionBatchRunner, not ``cfg.model``.

    The non-frontier-default RULE itself is the shared review kernel's
    (:func:`rebar.llm.review_kernel.resolve_verifier_model`); this wrapper just applies it to
    cfg (the kernel stays free of the LLMConfig plumbing)."""
    from dataclasses import replace

    from rebar.llm.review_kernel import resolve_verifier_model

    return replace(
        cfg,
        model=resolve_verifier_model(
            cfg.model, default_model=DEFAULT_MODEL, verifier_default=VERIFIER_DEFAULT_MODEL
        ),
    )


def _progressive_enabled(repo_root) -> bool:
    """Whether the progressive drift-refresh path is opted in
    (``verify.progressive_drift_refresh``, default off)."""
    from rebar import config as _config

    try:
        return bool(_config.load_config(repo_root).verify.progressive_drift_refresh)
    except Exception:  # noqa: BLE001 — config unreadable → conservative full review
        return False


def _remediation_decision(ticket_id: str, repo_root) -> dict[str, Any] | None:
    """The remediation-mode eligibility DECISION for ``ticket_id`` (epic 7d43, child ec89),
    or ``None`` when the ``verify.remediation_mode`` key is off/absent (default-OFF v1 rollout) or
    config is unreadable — in which case the gate runs a byte-identical full review. When enabled,
    returns :func:`attest.remediation_mode_candidate`'s decision dict (the Pass-3 drop math that
    consumes ``eligible`` is child cc5b; this only decides eligibility)."""
    from rebar import config as _config

    try:
        verify_cfg = _config.load_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → conservative full review (no remediation)
        return None
    if not verify_cfg.remediation_mode:
        return None
    return attest.remediation_mode_candidate(
        ticket_id, window_minutes=verify_cfg.remediation_window_minutes, repo_root=repo_root
    )


def review_plan(
    ticket_id: str,
    *,
    ref: str | None = None,
    source: str | None = None,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
    sign: bool = True,
    emit_sidecar: bool = True,
    advisory_cap: int | None = None,
) -> dict[str, Any]:
    """Run the plan-review gate on ``ticket_id`` and return a ``plan_review_verdict``.

    Assembles the whole-ticket context, runs the DET floor + the find → verify →
    decide LLM passes, mints finding ids, applies the advisory cap, runs the Pass-4
    coach, emits
    the ``REVIEW_RESULT`` sidecar (best-effort), and — on a non-blocking ``PASS`` —
    signs a plan-review attestation (so a subsequent ``claim`` passes the gate).

    Returns ``{verdict: PASS|BLOCK|INDETERMINATE, ticket_id, ticket_type, blocking[],
    advisory[], coaching[], indeterminate[], coverage, signature?, ...}``. Bugs and
    session_logs are exempt (PASS, runner=exempt). Raises only on a hard
    context-assembly failure; an unavailable LLM degrades to a DET-only review.

    ``ref``/``source`` select the code read-root (attested snapshot at the pinned SHA by
    default; ``local`` reads the in-place checkout). Verdict production runs through the v3
    engine workflow (``gates/plan-review.yaml``) and is SIGNED by this unchanged wrapper.
    An explicit non-default ``config`` is resolved ONCE at this boundary and honored uniformly
    — both for the LLM calls AND the verdict's ``model``/``runner`` fields (epic
    veiny-trout-brink; the gate ops read it via ``resolve_gate_config``).
    """
    from rebar.llm import gate_source

    handle = gate_source.resolve_gate_handle(ref, source, repo_root)
    with gate_source.gate_read_root(handle):
        cfg = gate_source.apply_handle(config or LLMConfig.from_env(repo_root=repo_root), handle)
        verdict = _run_plan_review(
            ticket_id,
            cfg=cfg,
            runner=runner,
            sign=sign,
            emit_sidecar=emit_sidecar,
            advisory_cap=advisory_cap,
            repo_root=repo_root,
        )
    return gate_source.annotate_result(verdict, handle)


def _run_plan_review(
    ticket_id: str,
    *,
    cfg: LLMConfig,
    runner: Runner | None,
    sign: bool,
    emit_sidecar: bool,
    advisory_cap: int | None,
    repo_root,
) -> dict[str, Any]:
    ctx = orchestrator.assemble_context(ticket_id, repo_root=repo_root, cfg=cfg)
    # Progressive drift-refresh (Story 2): when the attestation is stale ONLY because
    # reviewed code drifted (material + registry unchanged) and a cheap probe confirms the
    # plan still matches the code, refresh the attestation instead of a full re-review.
    # OPT-IN (verify.progressive_drift_refresh, default off) until the saving is measured.
    if sign and _progressive_enabled(repo_root):
        refreshed = orchestrator.drift_refresh(ctx, cfg, runner=runner, repo_root=repo_root)
        if refreshed is not None:
            from rebar.llm import findings

            return findings.validate_structured(refreshed, "plan_review_verdict")
    # Remediation-mode eligibility (epic 7d43, child ec89) — decided here, PARALLEL to the
    # drift-refresh check above and on the same code/material/registry signals, but it does NOT
    # early-return: the full criteria set still runs, and the DECISION is recorded on the verdict
    # so the Pass-3 rising floor (child cc5b) can consume it. Off/absent key ⇒ None ⇒ a
    # byte-identical full review (the back-out).
    remediation = _remediation_decision(ticket_id, repo_root) if sign else None
    cap = advisory_cap if advisory_cap is not None else orchestrator.DEFAULT_ADVISORY_CAP
    # Verdict PRODUCTION runs through the v3 engine workflow (gates/plan-review.yaml); the
    # signing/sidecar wrapper below is unchanged, so the signed attestation is stable. The
    # verify/coach steps run under the verifier cfg (non-frontier model unless overridden).
    from rebar.llm.workflow import gate_dispatch

    verdict = gate_dispatch.produce_plan_review_verdict(
        ctx, _verifier_cfg(cfg), runner=runner, advisory_cap=cap, repo_root=repo_root
    )

    material = orchestrator.material_fingerprint(ctx)
    verdict["material_fingerprint"] = material

    # Record the remediation-mode decision on the verdict coverage (observability + the seam the
    # Pass-3 rising floor reads in child cc5b). Only when remediation mode is enabled AND a real
    # decision was produced — a normal full review (key off/absent) leaves coverage untouched, so
    # the verdict shape is byte-identical to today's.
    if remediation is not None:
        verdict.setdefault("coverage", {})["remediation"] = remediation

    # Sidecar (best-effort; never fails the review). Skippable for a pure-read run.
    verdict["sidecar_emitted"] = (
        sidecar.emit(verdict, material=material, repo_root=repo_root) if emit_sidecar else False
    )

    # Sign on a non-blocking PASS (not for exempt/blocking/indeterminate). The
    # attestation = "process followed, no blocking red flags + coverage", NOT
    # "perfect"; advisory findings are coaching, not blocks.
    # Sign only a genuine PASS where the LLM tier actually ran. The verdict is already
    # INDETERMINATE when the tier was unavailable; the explicit llm_ran guard is
    # defense-in-depth so a DET-only result can never be signed (fuel-posse-ball).
    if (
        sign
        and verdict.get("verdict") == "PASS"
        and verdict.get("runner") != "exempt"
        and verdict.get("coverage", {}).get("llm_ran") is not False
    ):
        try:
            sig = attest.sign_plan_review(verdict, material=material, repo_root=repo_root)
            verdict["signature"] = {
                "signed": True,
                "key_id": sig.get("key_id"),
                "head_sha": sig.get("head_sha"),
            }
        except Exception as exc:  # noqa: BLE001 — surface, don't crash: a missing key is a real signal; broad-but-logged + recorded in-band
            # Don't crash the review on a signing failure, but record it in-band AND on
            # the logger (a missing/broken signing key is operator-actionable).
            logger.warning("attestation signing failed; verdict unsigned", exc_info=True)
            verdict["signature"] = {"signed": False, "error": str(exc)}
    else:
        verdict.setdefault("signature", {"signed": False, "reason": verdict.get("verdict")})

    # Validate the assembled verdict against its documented contract (shape-only,
    # permissive) — the same final re-validation the completion op does. Pins the
    # CLI/library `--output json` shape to plan_review_verdict.schema.json.
    from rebar.llm import findings

    return findings.validate_structured(verdict, "plan_review_verdict")


def registry_coverage() -> tuple[bool, list[str]]:
    """The criteria-registry completeness guard (re-exported for CI)."""
    from .registry import check_registry_coverage

    return check_registry_coverage()
