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

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner

from . import attest, context_assembly, drift_floor, material_diff, orchestrator, sidecar
from .attest import claim_gate_check, plan_review_status
from .floors import (
    _apply_completion_floor_to_verdict,
    _apply_floor_to_verdict,
    _classify_completion,
    _group_blocking_fix_units,
    _score_floor_novelty,
)
from .resign import resign_plan_review

logger = logging.getLogger(__name__)

__all__ = [
    "claim_gate_check",
    "plan_review_status",
    "registry_coverage",
    "resign_plan_review",
    "review_plan",
]


def _verifier_cfg(cfg: LLMConfig) -> LLMConfig:
    """The cfg the Pass-2 verify (and Pass-4 coach) steps run under: the STANDARD model class,
    whatever ``cfg.model`` is (ticket 172e).

    The rule this REPLACES kept the frontier model whenever ``cfg.model != DEFAULT_MODEL``, reading
    any non-default string as "the operator chose this". MEASURED consequence: provider-qualifying
    the SAME model (``anthropic:claude-opus-4-8``) — or naming any Bedrock id — silently kept
    Pass-2/Pass-4 on the frontier model, losing the cost downgrade AND, on a model that rejects
    sampling parameters, the greedy decoding the verification depends on.

    With no class configured, ``standard`` resolves to ``VERIFIER_DEFAULT_MODEL`` — the SAME
    MODEL today's rule picks. It is NOT byte-identical: the returned string is now PROVIDER-
    QUALIFIED (``anthropic:claude-sonnet-4-6`` where today it is the bare ``claude-
    sonnet-4-6``), because :func:`resolve_class` always qualifies. Functionally equivalent —
    ``infer_provider`` maps the bare ``claude-*`` id to the same provider — but the string is
    OBSERVABLE in usage logs and in the signed verdict's ``provider_provenance``, so it is a
    real, if small, behaviour change. Qualifying is the desirable direction: an unqualified id
    in an attestation is ambiguous about which provider served it, which is the misattribution
    this epic exists to remove.

    The class config is now the steering wheel: an operator changes the verifier by configuring the
    ``standard`` class, not by side-effecting ``cfg.model``.

    Still applied on cfg rather than as a static per-step ``model:`` in ``gates/plan-review.yaml``,
    because ``resolve_model`` precedence is ``step > workflow > cfg`` — a literal step model would
    always beat the operator's configuration.

    "THE PASS-1 FINDER IS UNAFFECTED" IS AN INVARIANT, NOT AN OBSERVATION — this result becomes the
    WHOLE RUN's cfg at the ``produce_plan_review_verdict`` boundary, so the runner is built from it
    and Pass-1 will inherit this downgrade unless two things hold: ``RunRequest.config`` takes
    precedence over the runner's own (b690), and the Pass-1 ladder's entry rung names ``frontier``
    (77ed). Both are pinned by ``tests/unit/test_pass1_finder_model.py``, which asserts the model
    REACHING THE RUNNER — the YAML declaration alone is not evidence. See 77ed for the history."""
    from dataclasses import replace

    from rebar.llm.model_classes import STANDARD_CLASS, resolve_model_string
    from rebar.llm.review_kernel import max_output_cfg

    # Model-max output budget (bug 30a2): every verifier-cfg consumer rides at the resolved
    # model's maximum output capacity — applied AFTER the model swap so the raise matches the
    # model that actually runs.
    return max_output_cfg(replace(cfg, model=resolve_model_string(STANDARD_CLASS, cfg.repo_path)))


def _remediation_decision(ticket_id: str, repo_root) -> dict[str, Any] | None:
    """The remediation-mode eligibility DECISION for ``ticket_id`` (epic 7d43, child ec89),
    or ``None`` when config is unreadable — in which case the gate runs a byte-identical full
    review. Remediation mode is always on (off switch retired in story 4cdf); this returns
    :func:`attest.remediation_mode_candidate`'s decision dict (the Pass-3 drop math that consumes
    ``eligible`` is child cc5b; this only decides eligibility)."""
    from rebar import config as _config

    try:
        verify_cfg = _config.compose_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → conservative full review (no remediation)
        return None
    return attest.remediation_mode_candidate(
        ticket_id, window_minutes=verify_cfg.remediation_window_minutes, repo_root=repo_root
    )


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
    """The gated Pass-3 rising-floor entry (child cc5b): apply the floor when a remediation
    re-review is in progress (``remediation`` is non-None) and the per-review eligibility holds
    (ec89's decision ``eligible``). The rising floor is always active (operator-authorized on field
    evidence, 2026-07-11 — in lieu of 150b's ``discriminates_novelty`` eval; the off switch was
    retired in story 4cdf); the remediation-eligibility + no-prior-memory self-gates below still
    keep a normal review byte-identical."""
    from rebar import config as _config

    if not (remediation and remediation.get("eligible")):
        return
    try:
        verify_cfg = _config.compose_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → run un-floored
        return
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
        verify_cfg = _config.compose_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → run un-floored
        return
    if not verify_cfg.completion_floor_active:
        return  # evidence gate: inert until the calibration gold-set has cleared its bar
    advisory = verdict.get("advisory") or []
    if not advisory:
        return
    try:
        manifest = orchestrator.delivered_children_manifest(ticket_id, repo_root=repo_root)
    except Exception:
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
    # Observability (story c366): the successful-drop path is otherwise silent (only failures
    # warn). Emit one INFO line naming the floored finding ids so live suppressions are visible
    # without opening the sidecar; the full drop record still lands in the sidecar dropped[].
    floored = (verdict.get("coverage") or {}).get("completion_floored_finding_ids") or []
    if floored:
        logger.info(
            "completion floor dropped %d advisory finding(s) on %s: %s "
            '(audit via sidecar dropped[] drop_reason="completion")',
            len(floored),
            ticket_id,
            ", ".join(floored),
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
    force: bool = False,
    retry: bool = False,
) -> dict[str, Any]:
    """Run the plan-review gate on ``ticket_id`` and return a ``plan_review_verdict``.

    Assembles the whole-ticket context, runs the DET floor + the find → verify →
    decide LLM passes, mints finding ids, applies the advisory cap, runs the Pass-4
    coach, emits
    the ``REVIEW_RESULT`` sidecar (best-effort), and — on a non-blocking ``PASS`` —
    signs a plan-review attestation (so a subsequent ``claim`` passes the gate).

    Returns ``{verdict: PASS|BLOCK|INDETERMINATE, ticket_id, ticket_type, blocking[],
    advisory[], coaching[], indeterminate[], coverage, signature?, ...}``. ``session_log`` /
    ``code_review`` / ``identity`` short-circuit to a bare exempt PASS (``runner=exempt``, no
    review runs). A ``bug`` does NOT: since the bug review tier (epic 6982/R4) it gets a light
    advisory review — the DET floor plus the restricted ``BUG_TIER_CRITERIA`` probe. P1/P10
    readiness-floor failures and P4 description admission failures still BLOCK before any LLM
    pass; otherwise the bug tier surfaces advisory findings while a well-formed bug can PASS.
    Raises only on a hard context-assembly failure; an unavailable LLM degrades to a DET-only
    review.

    A ticket that is not yet claimable (status closed/idea/blocked, or ``open`` but
    blocked by an unclosed dependency) is FAST-FAILED with no LLM — an unsigned
    INDETERMINATE verdict — since the review's only product is a claim attestation the
    ticket cannot use yet; ``in_progress`` is never fast-failed and ``force=True`` bypasses
    the gate (see :mod:`rebar.llm.plan_review.claimability`).

    ``ref``/``source`` select the code read-root (attested snapshot at the pinned SHA by
    default; ``local`` reads the in-place checkout). Verdict production runs through the v3
    engine workflow (``gates/plan-review.yaml``) and is SIGNED by this unchanged wrapper.
    An explicit non-default ``config`` is resolved ONCE at this boundary and honored uniformly
    — both for the LLM calls AND the verdict's ``model``/``runner`` fields (epic
    veiny-trout-brink; the gate ops read it via ``resolve_gate_config``).
    """
    from rebar.llm import gate_source
    from rebar.llm.config_binding import compose_and_bind_llm_config
    from rebar.llm.gate_admission import gate_admission
    from rebar.llm.peak_rss import gate_peak_rss

    # Concurrency admission (ADR 0112 decision 5) is taken BEFORE resolve_gate_handle,
    # because materializing the snapshot is what spends the bytes the cap bounds. Wrapping
    # HERE rather than the MCP daemon and the CLI separately covers both call paths at one
    # seam, exactly as gate_peak_rss does, and shares ONE counter with verify_completion.
    # At capacity this RAISES GateCongestedError instead of running the gate.
    with gate_admission("plan_review", ticket_id, repo_root):
        handle = gate_source.resolve_gate_handle(ref, source, repo_root)
        with (
            # Measurement only (bug 9ea3): emits the GATE_PEAK_RSS marker on completion,
            # including on the raising paths. Wrapping HERE covers both the MCP daemon and
            # the CLI, which both reach the gate through this function.
            gate_peak_rss("plan_review", ticket_id),
            gate_source.gate_read_root(handle),
            compose_and_bind_llm_config(repo_root=repo_root, explicit=config) as bound,
        ):
            cfg = gate_source.apply_handle(bound, handle)
            verdict = _run_plan_review(
                ticket_id,
                cfg=cfg,
                runner=runner,
                sign=sign,
                emit_sidecar=emit_sidecar,
                advisory_cap=advisory_cap,
                repo_root=repo_root,
                force=force,
                retry=retry,
                # Resolved source: stamped on the verdict only AFTER this returns (bug 5128).
                source_mode=handle.source,
            )
        return gate_source.annotate_result(verdict, handle)


def _finalize_signature(
    verdict: dict[str, Any],
    *,
    sign: bool,
    source_mode: str | None,
    material,
    review_phase,
    priority_floor,
    repo_root,
    review_snapshot,
    initial_generation,
    ctx,
) -> None:
    """Sign a non-blocking PASS attestation on ``verdict`` (in place), else stamp the
    machine-readable unsigned reason. Signs only when the LLM tier actually ran (the
    ``llm_ran`` guard is defense-in-depth so a DET-only result can never be signed); an
    unattested local read is never certifiable (ADR 0005). A signing failure is recorded
    in-band and logged, never raised."""
    certifiable_pass = (
        sign
        and verdict.get("verdict") == "PASS"
        and verdict.get("runner") != "exempt"
        and verdict.get("coverage", {}).get("llm_ran") is not False
    )
    if certifiable_pass and source_mode == "local":
        verdict["signature"] = {"signed": False, "reason": "local-source-never-signs"}
        return
    if not certifiable_pass:
        verdict.setdefault("signature", {"signed": False, "reason": verdict.get("verdict")})
        return
    try:
        sig = attest.sign_plan_review(
            verdict,
            material=material,
            review_phase=review_phase,
            priority_floor=priority_floor,
            repo_root=repo_root,
            relation_snapshot=review_snapshot,
            initial_generation=initial_generation,
            # A container's dep set inherits the children's file_impact (3e4b); read lazily
            # here so a non-signing path never requires ``ctx.children``.
            children=ctx.children,
        )
        verdict["signature"] = {
            "signed": True,
            "key_id": sig.get("key_id"),
            "head_sha": sig.get("head_sha"),
        }
    except Exception as exc:
        logger.warning("attestation signing failed; verdict unsigned", exc_info=True)
        signature_error = {"signed": False, "error": str(exc)}
        from . import generation

        if isinstance(exc, generation.PlanReviewGenerationError):
            signature_error.update(event=exc.event, retryable=exc.retryable)
        verdict["signature"] = signature_error


def _run_plan_review(
    ticket_id: str,
    *,
    cfg: LLMConfig,
    runner: Runner | None,
    sign: bool,
    emit_sidecar: bool,
    advisory_cap: int | None,
    repo_root,
    force: bool = False,
    retry: bool = False,
    source_mode: str | None = None,
) -> dict[str, Any]:
    from . import claimability

    # Fast-fail (no LLM) BEFORE any context assembly when the ticket cannot be claimed
    # anyway — see claimability.not_claimable_verdict. `--force` bypasses this gate.
    if not force:
        not_claimable = claimability.not_claimable_verdict(ticket_id, cfg=cfg, repo_root=repo_root)
        if not_claimable is not None:
            return not_claimable

    # Snapshot all plan-material relations before any path can reach an LLM preflight/probe;
    # the same value is threaded into signing so one review does exactly one store reduction.
    from . import generation, relation_snapshot

    try:
        # Ignore UNTRACKED files in the SHARED tickets-tracker: this preflight READ fingerprints
        # the committed HEAD, which untracked files cannot change; treating stray artifacts as
        # fatal would collapse review-plan (and claim) machine-wide. The under-lock signing
        # re-check also ignores them; tracked dirty state (modified/staged/unmerged) still fails.
        review_snapshot = relation_snapshot.collect_plan_relation_snapshot(
            ticket_id, repo_root=repo_root, ignore_untracked=True
        )
    except relation_snapshot.PlanRelationSnapshotError as exc:
        record = {
            "event": "plan_relation_snapshot_error",
            "reason": exc.reason,
            "canonical_id": exc.canonical_id,
            "reference": exc.reference,
        }
        logger.error("plan relation snapshot failed: %s", record, extra=record)
        return claimability.indeterminate_verdict(
            exc.canonical_id or ticket_id,
            ticket_type="",
            finding={
                "id": "plan-relation-snapshot-error",
                "reason": exc.reason,
                "canonical_id": exc.canonical_id,
                "reference": exc.reference,
            },
            coverage_extra={"plan_relation_snapshot_error": record},
            signature_reason=exc.reason,
            remediation=(
                "Repair or remove the unreadable plan relationship, then rerun "
                "`rebar review-plan`; no plan-review attestation was signed."
            ),
            cfg=cfg,
        )

    initial_generation = generation.from_snapshot(review_snapshot)

    ctx = context_assembly.assemble_context(ticket_id, repo_root=repo_root, cfg=cfg)
    review_phase = initial_generation.phase
    priority_floor = initial_generation.priority_floor
    # Exact retry (story RP-06 S5): resume ONLY the latest review, and ONLY when it is a
    # retryable INDETERMINATE with a current discovery journal. An ineligible retry REFUSES
    # here (before any model call, reuse short-circuit, or sidecar write); an eligible retry
    # proceeds through the normal review below, whose Pass-1 chunk-checkpoint resume reuses the
    # successful units and re-runs only the missing ones under this invocation's fresh budget.
    retry_prior: dict[str, Any] | None = None
    if retry:
        from . import retry as retry_mod

        retry_refusal, retry_prior = retry_mod.gate(ticket_id, ctx, repo_root=repo_root, cfg=cfg)
        if retry_refusal is not None:
            return retry_refusal
    # Idempotence short-circuit (feature b3e5): reuse a still-VALID plan-review attestation
    # (the SAME validity the claim gate consumes) instead of re-running the billable review.
    # Signing path only; `--force`/`--retry` bypass it.
    from . import cited_anchor, reuse

    if sign and not force and not retry:
        reused = reuse.idempotent_reuse(ticket_id, ctx, repo_root=repo_root)
        if reused is not None:
            return reused
    # BLOCK verdict-reuse (bug 7e77): a BLOCK never signs, so reuse the stored BLOCK verdict from
    # the latest sidecar when its material fingerprint AND review code sha still match (zero LLM,
    # no new sidecar). Not sign-gated; `--force`/`--retry` bypass it.
    if not force and not retry:
        block_reused = reuse.verdict_reuse(ticket_id, ctx, repo_root=repo_root)
        if block_reused is not None:
            return block_reused
    # Warn-only cited-anchor pre-check (task ccba) — deterministic, zero-LLM, before drift_refresh.
    anchor_precheck = cited_anchor.precheck(ticket_id, ctx, repo_root=repo_root)
    # Progressive drift-refresh (Story 2): when the attestation is stale ONLY because reviewed
    # code drifted (material + registry unchanged) and a cheap probe confirms the plan still
    # matches, refresh instead of a full re-review. Self-gated by ``if sign``; local never
    # refreshes (a refresh re-signs, local never signs — bug 5128).
    if sign and source_mode != "local" and not retry:
        refreshed = orchestrator.drift_refresh(
            ctx,
            cfg,
            runner=runner,
            repo_root=repo_root,
            relation_snapshot=review_snapshot,
        )
        if refreshed is not None:
            from rebar.llm import findings

            return findings.validate_structured(refreshed, "plan_review_verdict")
    # Remediation-mode + drift-floor eligibility (epic 7d43 / bug 5e40) — decided here on the
    # code/material/registry signals, PARALLEL and mutually exclusive (code_unchanged vs
    # code_drifted). Neither early-returns: the full criteria set still runs and the DECISION is
    # recorded on the verdict for the Pass-3 floors to consume. Off/absent ⇒ byte-identical review.
    remediation = _remediation_decision(ticket_id, repo_root) if sign else None
    drift = drift_floor.decision(ticket_id, repo_root) if sign else None
    cap = advisory_cap if advisory_cap is not None else orchestrator.DEFAULT_ADVISORY_CAP
    # Verdict PRODUCTION runs through the v3 engine workflow (gates/plan-review.yaml); the
    # signing/sidecar wrapper below is unchanged. Verify/coach steps run under the verifier cfg.
    from rebar.llm.workflow import gate_dispatch

    prerequisite_blocks = [
        {
            "canonical_id": prerequisite_id,
            "rendered_text": (
                f"# {review_snapshot.ticket_states_by_id[prerequisite_id].get('title', '')}\n\n"
                f"{review_snapshot.ticket_states_by_id[prerequisite_id].get('description', '')}"
            ),
        }
        for prerequisite_id in review_snapshot.prerequisite_ids
    ]
    verdict = gate_dispatch.produce_plan_review_verdict(
        ctx,
        _verifier_cfg(cfg),
        runner=runner,
        advisory_cap=cap,
        repo_root=repo_root,
        prerequisite_blocks=prerequisite_blocks,
    )

    # Mid-run cancellation (story 2c89): the ticket's OWN material changed while the review ran —
    # return the cancelled INDETERMINATE verbatim, BEFORE the floors/signing/sidecar emit.
    if verdict.get("coverage", {}).get("cancelled"):
        return verdict

    material = initial_generation.own_material
    verdict["material_fingerprint"] = material

    # Record the remediation + drift-floor decisions on coverage (the seams the Pass-3 floors read).
    # Off/absent ⇒ coverage untouched, so a normal review's verdict shape is byte-identical.
    if remediation is not None:
        verdict.setdefault("coverage", {})["remediation"] = remediation
    if drift is not None:
        verdict.setdefault("coverage", {})["drift"] = drift

    # Pass-3 floors (rising / completion / drift — child cc5b, story 6533, bug 5e40), applied
    # BEFORE the sidecar emit so dropped findings land in the sidecar while the surfaced verdict is
    # narrowed. Each is gated + inert by default, so a normal review's verdict stays byte-identical.
    _maybe_apply_rising_floor(
        ticket_id, verdict, remediation, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )
    _maybe_apply_completion_floor(
        ticket_id, verdict, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )

    # Pass-3 DRIFT FLOOR (bug 5e40) — the code-drift-axis analogue, applied after the others and
    # before the sidecar emit. Gated on a plan-UNCHANGED + code-DRIFTED re-review; inert otherwise.
    drift_floor.maybe_apply(
        ticket_id, verdict, drift, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )

    # VALIDATION-ASSESSMENT cross-checks (bug 5e40) — two per-verdict CONSISTENCY drops (intra-
    # verdict contradiction + comment-trail re-litigation) the axis floors do not cover. Applied
    # before the sidecar emit; each gated inert (verdict byte-identical) by default, fail-safe.
    from . import xcheck

    xcheck.maybe_apply_contradiction(
        ticket_id, verdict, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )
    xcheck.maybe_apply_comment_trail(
        ticket_id, verdict, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )

    # Blocking fix-unit grouping (story 5e64) — stamp-only, before the emit so stamps persist.
    _group_blocking_fix_units(verdict)

    _finalize_signature(
        verdict,
        sign=sign,
        source_mode=source_mode,
        material=material,
        review_phase=review_phase,
        priority_floor=priority_floor,
        repo_root=repo_root,
        review_snapshot=review_snapshot,
        initial_generation=initial_generation,
        ctx=ctx,
    )

    # Record the outcome (True AND False) so the warning's precision is measurable offline.
    cited_anchor.record_metrics(verdict, anchor_precheck)

    # Persist the recovery sidecar only after the atomic sign attempt (writing it earlier
    # advances the store revision and invalidates this review's generation); best-effort.
    # Retry bookkeeping (story RP-06 S5): on the retry path, mark the verdict and compute the
    # cumulative retry lineage BEFORE the emit so both land in the persisted payload; a normal
    # review's shape is byte-identical.
    retry_lineage = None
    if retry:
        from . import retry as retry_mod

        verdict.setdefault("coverage", {})["retry"] = True
        retry_lineage = retry_mod.build_lineage(retry_prior or {}, verdict)

    verdict["sidecar_emitted"] = (
        sidecar.emit(
            verdict,
            material=material,
            material_parts=material_diff.reviewed_material_parts(review_snapshot, material),
            reviewed_related_material=review_snapshot.related_material,
            review_phase=review_phase,
            priority_floor=priority_floor,
            repo_root=repo_root,
            # Lets sign-review refuse a local PASS; verified_at_sha cannot tell (bug 5128).
            source=source_mode,
            retry_lineage=retry_lineage,
        )
        if emit_sidecar
        else False
    )
    # The reducer-ignored discovery journal is persisted to the sidecar (above) purely to seed
    # `--retry` eligibility; it is NEVER part of the surfaced verdict (story RP-06 S5 AC6).
    from rebar.llm.workflow import plan_review_recovery

    plan_review_recovery.strip_surfaced_journal(verdict.get("coverage"))

    # Store-wide cross-ticket overlap (epic only-crave-art, story 0f70) — ADVISORY ONLY. Runs
    # AFTER signing + sidecar.emit; rides in a SEPARATE `overlap[]` key that never affects the
    # verdict/claim gate. Gated OFF by default, to real runs only, and graceful-skips (→ []) when
    # the LLM/agents extra is absent. `overlap[]` is added only when enabled (shape unchanged off).
    if emit_sidecar:
        from rebar import config as _overlap_config

        if _overlap_config.compose_config(repo_root).verify.suggest_duplicate_tickets:
            from rebar.llm.overlap.wire import overlap_findings

            verdict["overlap"] = overlap_findings(
                ticket_id, repo_root=repo_root, config=cfg, runner=runner
            )

    # Validate the assembled verdict against its documented contract (shape-only, permissive).
    from rebar.llm import findings

    return findings.validate_structured(verdict, "plan_review_verdict")


def registry_coverage() -> tuple[bool, list[str]]:
    """The criteria-registry completeness guard (re-exported for CI)."""
    from .registry import check_registry_coverage

    return check_registry_coverage()
