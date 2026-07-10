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


def _apply_floor_to_verdict(
    verdict: dict[str, Any], novelty_map: dict[int, float], *, t_novel: float, floor: float
) -> None:
    """Apply the Pass-3 rising floor (child cc5b) IN PLACE on the verdict's surfaced advisory
    findings: a finding at position ``i`` is DROPPED iff ``decide.rising_floor_drop`` (novel +
    low-priority). Dropped findings move from ``advisory`` into the verdict's ``dropped`` bucket
    (the sidecar persists it with ``norm_id``), and the coverage records ``narrowed``/
    ``floored_criteria``/``floored_finding_ids`` AND its ``counts`` are corrected (advisory_surfaced
    down, dropped up) so the post-floor counts stay consistent with the buckets. Pure (no LLM); the
    novelty per index is injected. A no-drop run leaves the verdict byte-identical."""
    from rebar.llm.review_kernel import decide

    advisory = verdict.get("advisory") or []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for i, f in enumerate(advisory):
        nov = novelty_map.get(i, 0.0)
        prio = f.get("priority") or 0.0
        if decide.rising_floor_drop(prio, nov, t_novel=t_novel, floor=floor):
            dropped.append({**f, "_floored": True, "novelty": nov, "drop_reason": "novelty"})
        else:
            kept.append(f)
    if not dropped:
        return
    verdict["advisory"] = kept
    verdict.setdefault("dropped", []).extend(dropped)
    cov = verdict.setdefault("coverage", {})
    cov["narrowed"] = True
    cov["floored_criteria"] = sorted({c for f in dropped for c in (f.get("criteria") or [])})
    cov["floored_finding_ids"] = [f.get("id") for f in dropped]
    counts = cov.get("counts")
    if isinstance(counts, dict):  # keep the baked counts consistent with the post-floor buckets
        counts["advisory_surfaced"] = len(kept)
        counts["dropped"] = (counts.get("dropped") or 0) + len(dropped)


def _score_floor_novelty(
    advisory: list[dict[str, Any]],
    prior_findings: list[dict[str, Any]],
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
    repo_root,
) -> dict[int, float]:
    """Run the 150b novelty sub-call over the surfaced advisory findings (the droppable surface)
    against the prior findings, returning ``{advisory_index: novelty}``. Fail-safe: any error →
    ``{}`` (no drops). The droppable surface is bounded by the advisory cap, so a generous single
    window + a coarse char/4 estimator keep it to one sub-call."""
    from rebar.llm.review_kernel.verify import score_novelty
    from rebar.llm.runner import RunRequest, get_runner

    from . import passes

    try:
        runner_sel = runner or get_runner(cfg)
        vcfg = _verifier_cfg(cfg)
        system = passes._resolve_system(passes.PASS_NOVELTY, ctx.plan_text, vcfg)

        def run_chunk(instructions: str, context: str) -> list[dict[str, Any]]:
            req = RunRequest(
                system_prompt=system,
                instructions=f"{instructions}\n\n## Prior-review findings (context)\n{context}",
                config=vcfg,
                reviewers=["plan-novelty"],
                mode="structured",
                output_schema="plan_review_novelty",
                execution_mode="single_turn",
            )
            return runner_sel.run(req).get("novelties", []) or []

        return score_novelty(
            advisory,
            prior_findings=prior_findings,
            run_chunk=run_chunk,
            window_tokens=100_000,
            est_tokens=lambda s: len(s) // 4 + 1,
        )
    except Exception:  # noqa: BLE001 — fail-safe: a broken novelty signal yields NO drops (never suppresses)
        logger.warning("rising-floor novelty scoring failed; running un-floored", exc_info=True)
        return {}


def _maybe_apply_rising_floor(
    ticket_id: str,
    verdict: dict[str, Any],
    remediation: dict[str, Any] | None,
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
    repo_root,
) -> None:
    """The triple-gated Pass-3 rising-floor entry (child cc5b): apply the floor ONLY when config
    ``remediation_mode`` is on (``remediation`` is non-None), the per-review eligibility holds
    (ec89's decision ``eligible``), AND ``verify.novelty_drop_active`` is true (the evidence gate,
    flipped only after 150b's ``discriminates_novelty`` eval clears its bar). By default the flag is
    False, so the floor is inert and the verdict is byte-identical to a normal review."""
    from rebar import config as _config

    if not (remediation and remediation.get("eligible")):
        return
    try:
        verify_cfg = _config.load_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → run un-floored
        return
    if not verify_cfg.novelty_drop_active:
        return  # evidence gate: inert until the novelty discriminator has cleared its bar
    advisory = verdict.get("advisory") or []
    prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
    # SURFACED-ONLY (bug old-frilly-plankton): score novelty ONLY against findings that were
    # RETURNED TO THE CLIENT (block/advisory), never against previously-dropped findings. The
    # sidecar persists dropped findings too, so reading ``findings`` unfiltered would let a finding
    # permanently floored for convergence re-enter the prior set, re-match on recurrence, score as
    # low-novelty "carryover", and thereby ESCAPE the floor that dropped it. This mirrors the same
    # decision filter ``prior_concerns()`` already applies on the recall path (they now share
    # ``surfaced_findings`` so the two prior-set consumers cannot disagree).
    prior_findings = sidecar.surfaced_findings(prior)
    if not advisory or not prior_findings:
        return
    novelty_map = _score_floor_novelty(
        advisory, prior_findings, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )
    _apply_floor_to_verdict(
        verdict,
        novelty_map,
        t_novel=verify_cfg.novelty_drop_threshold,
        floor=verify_cfg.novelty_priority_floor,
    )


def _apply_completion_floor_to_verdict(
    verdict: dict[str, Any],
    completion_map: dict[int, dict[str, Any]],
    *,
    floor: float,
    preserve: frozenset[str],
    delivered_ids: frozenset[str],
) -> None:
    """Apply the Pass-3 COMPLETION floor (story 6533) IN PLACE on the surfaced advisory findings:
    a finding at position ``i`` is DROPPED iff :func:`passes.completion_floor_drop` (attribution in
    ``delivered_ids`` + limited-to-closed + plan-semantics + priority < floor + not-preserved).
    Dropped findings move from ``advisory`` into the verdict's ``dropped`` bucket carrying
    ``drop_reason="completion"`` (+ the finding's ``completion`` answers for the sidecar join), and
    the coverage records the completion-specific ``completion_floored_criteria`` /
    ``completion_floored_finding_ids`` (namespaced so they never collide with the novelty floor's
    keys) AND corrects its ``counts``. Pure (no LLM); the completion answers per index + the
    delivered-now id set are injected. A no-drop run leaves the verdict byte-identical."""
    from . import passes

    advisory = verdict.get("advisory") or []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for i, f in enumerate(advisory):
        ans = completion_map.get(i)
        if ans and passes.completion_floor_drop(
            ans,
            f.get("priority") or 0.0,
            f.get("criteria") or [],
            floor=floor,
            preserve=preserve,
            delivered_ids=delivered_ids,
        ):
            dropped.append({**f, "_floored": True, "drop_reason": "completion", "completion": ans})
        else:
            kept.append(f)
    if not dropped:
        return
    verdict["advisory"] = kept
    verdict.setdefault("dropped", []).extend(dropped)
    cov = verdict.setdefault("coverage", {})
    cov["narrowed"] = True
    cov["completion_floored_criteria"] = sorted(
        {c for f in dropped for c in (f.get("criteria") or [])}
    )
    cov["completion_floored_finding_ids"] = [f.get("id") for f in dropped]
    counts = cov.get("counts")
    if isinstance(counts, dict):  # keep the baked counts consistent with the post-floor buckets
        counts["advisory_surfaced"] = len(kept)
        counts["dropped"] = (counts.get("dropped") or 0) + len(dropped)


def _classify_completion(
    advisory: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
) -> dict[int, dict[str, Any]]:
    """Run the Pass-2 completion sub-call over the surfaced advisory findings against the given
    delivered-children ``manifest``, returning ``{advisory_index: {attribution, containment,
    layer}}``. Fail-safe: any error → ``{}`` (no drops). The sub-call itself also degrades to ``{}``
    on error, so this is defense-in-depth."""
    from rebar.llm.runner import get_runner

    from . import passes

    try:
        runner_sel = runner or get_runner(cfg)
        return passes.pass2_completion(
            runner_sel,
            _verifier_cfg(cfg),
            plan=ctx.plan_text,
            findings=advisory,
            delivered_manifest=manifest,
        )
    except Exception:  # noqa: BLE001 — fail-safe: a broken completion signal yields NO drops
        logger.warning("completion floor classification failed; running un-floored", exc_info=True)
        return {}


def _maybe_apply_completion_floor(
    ticket_id: str,
    verdict: dict[str, Any],
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
    repo_root,
) -> None:
    """The Pass-3 COMPLETION floor entry (story 6533): apply the floor ONLY when the ticket is a
    CONTAINER (``ctx.has_children`` — a leaf has no delivered children to settle) AND the evidence
    gate ``verify.completion_floor_active`` is true. Builds the delivered-children manifest (its
    ids are the ONLY droppable attributions — "delivery is proven, not assumed"), runs the
    completion sub-call over the surfaced advisory findings, and drops the fully-delivered,
    settled-plan-text findings below the floor. By default the flag is False, so the floor is inert
    and the verdict is byte-identical to a normal review. Fail-safe: no children / empty manifest /
    empty classification → no drops."""
    from rebar import config as _config

    if not getattr(ctx, "has_children", False):
        return
    try:
        verify_cfg = _config.load_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → run un-floored
        return
    if not verify_cfg.completion_floor_active:
        return  # evidence gate: inert until the calibration gold-set has cleared its bar
    advisory = verdict.get("advisory") or []
    if not advisory:
        return
    try:
        manifest = orchestrator.delivered_children_manifest(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — fail-safe: manifest build failed → no drops
        logger.warning("delivered-children manifest failed; running un-floored", exc_info=True)
        return
    delivered_ids = frozenset(m["ticket_id"] for m in manifest if m.get("ticket_id"))
    if not delivered_ids:
        return  # nothing delivered → nothing to settle
    completion_map = _classify_completion(advisory, manifest, ctx=ctx, cfg=cfg, runner=runner)
    if not completion_map:
        return
    _apply_completion_floor_to_verdict(
        verdict,
        completion_map,
        floor=verify_cfg.completion_priority_floor,
        preserve=frozenset(verify_cfg.completion_preserve_criteria),
        delivered_ids=delivered_ids,
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

    # Pass-3 RISING FLOOR (child cc5b) — applied BEFORE the sidecar emit so the dropped findings
    # land in the sidecar (with norm_id) while leaving the surfaced verdict narrowed. Triple-gated
    # (config remediation_mode + per-review eligibility + verify.novelty_drop_active); inert (and
    # the verdict byte-identical) by default.
    _maybe_apply_rising_floor(
        ticket_id, verdict, remediation, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )

    # Pass-3 COMPLETION FLOOR (epic 66ac / story 6533) — the container-completion analogue of the
    # rising floor, applied AFTER it and BEFORE the sidecar emit so completion-dropped findings land
    # in the sidecar (with drop_reason="completion"). Gated by container(has_children) +
    # verify.completion_floor_active; inert (and the verdict byte-identical) by default.
    _maybe_apply_completion_floor(
        ticket_id, verdict, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )

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

    # Store-wide cross-ticket overlap (epic only-crave-art, story 0f70) — ADVISORY ONLY.
    # Runs AFTER sidecar.emit + signing, so the sidecar, coverage counts, and attestation are
    # byte-identical whether overlap is on or off (the overlap results ride in a SEPARATE
    # `overlap[]` key that is never a blocking/advisory finding and never affects the verdict
    # or the claim gate). Gated OFF by default (verify.overlap_enabled); gated to real runs
    # (emit_sidecar) not pure-read; and graceful-skips (→ []) when the LLM/agents extra/key is
    # absent. `overlap[]` is added ONLY when enabled, so the verdict shape is unchanged when off.
    if emit_sidecar:
        from rebar import config as _overlap_config

        if _overlap_config.load_config(repo_root).verify.overlap_enabled:
            from rebar.llm.overlap.wire import overlap_findings

            verdict["overlap"] = overlap_findings(
                ticket_id, repo_root=repo_root, config=cfg, runner=runner
            )

    # Validate the assembled verdict against its documented contract (shape-only,
    # permissive) — the same final re-validation the completion op does. Pins the
    # CLI/library `--output json` shape to plan_review_verdict.schema.json.
    from rebar.llm import findings

    return findings.validate_structured(verdict, "plan_review_verdict")


def registry_coverage() -> tuple[bool, list[str]]:
    """The criteria-registry completeness guard (re-exported for CI)."""
    from .registry import check_registry_coverage

    return check_registry_coverage()
