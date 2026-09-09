"""Reconstruct plan-review verdicts and metrics from gate run records.

This strict leaf keeps rebar imports lazy and never imports ``gate_dispatch``, which
re-exports these names for compatibility. Orchestrator attribute access is intentional:
binding bare functions would defeat lifecycle monkeypatches.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Central step-id vocabulary for recovery and metrics. Validate it against the loaded gate
# at dispatch so YAML drift fails loudly instead of silently degrading the verdict.
STEP_PRECHECK = "precheck"
STEP_ASSEMBLE = "assemble"
STEP_FINDERS = "finders"
STEP_VERIFY = "verify"
STEP_DECIDE = "decide"
STEP_COACH = "coach"

# Stable scripted-step failure vocabulary.  This marker crosses the generic workflow recorder
# boundary so gate dispatch can distinguish a local criteria/configuration fault from an LLM
# outage without parsing exception prose.
CRITERIA_CONFIG_FAILURE_KIND = "criteria_config"

# The step ids the recovery/metrics logic depends on being present in the loaded gate doc.
_PLAN_REVIEW_REQUIRED_STEP_IDS = frozenset(
    {STEP_PRECHECK, STEP_ASSEMBLE, STEP_FINDERS, STEP_VERIFY, STEP_DECIDE, STEP_COACH}
)


class GateContractError(RuntimeError):
    """The loaded gate lacks a step id required by recovery or metrics reconstruction."""


def _collect_step_ids(node: Any) -> set[str]:
    """Every step ``id`` in a loaded workflow doc, including ids nested inside ``branch``
    then/else arms (a recursive walk over the plain dict/list doc structure)."""
    ids: set[str] = set()
    if isinstance(node, dict):
        sid = node.get("id")
        if isinstance(sid, str):
            ids.add(sid)
        for value in node.values():
            ids |= _collect_step_ids(value)
    elif isinstance(node, list):
        for item in node:
            ids |= _collect_step_ids(item)
    return ids


def _validate_gate_step_ids(doc: dict[str, Any], required: frozenset, *, gate_name: str) -> None:
    """Reject a loaded gate missing any recovery step id before execution."""
    present = _collect_step_ids(doc.get("steps"))
    missing = sorted(required - present)
    if missing:
        raise GateContractError(
            f"gate workflow {gate_name!r} is missing step id(s) {missing} that the dispatcher's "
            f"recovery/metrics logic references (present step ids: {sorted(present)}). A step was "
            f"likely renamed in gates/{gate_name}.yaml — update the STEP_* constants in "
            f"gate_dispatch.py to match, or restore the id."
        )


# Step ids/kinds that partition a plan-review run into its latency tiers (toy-kink-ire).
_DET_STEP_IDS = frozenset({STEP_PRECHECK})  # the deterministic floor tier
_LLM_STEP_KINDS = frozenset({"agent", "batch"})  # the billable LLM tier (finders/verify/coach)


def _attach_plan_review_metrics(verdict: dict[str, Any], rec, total_ms: float) -> None:
    """Attach recorder-derived plan-review timing, call-count, and usage metrics.

    ``det_ms`` covers precheck, ``llm_ms`` covers agent/batch steps, and ``total_ms``
    also includes scripted overhead. Batch usage supplies token totals, raw records,
    and per-criterion data. Existing coverage survives partial or untimed records.
    """
    det_ms = 0.0
    llm_ms = 0.0
    finder_criteria = 0
    agent_calls = 0
    verify_requests = 0  # Pass-2 verifier model-request count — step usage vs its budget (bug 59bc)
    usage_per_call: list[dict[str, Any]] = []  # d52a: per-call records off the batch `_usage`
    batch_plans: list[dict[str, Any]] = []  # RP-06 S5: the opaque pass1 coverage plan(s)
    for s in rec.steps:
        if not isinstance(s, dict) or s.get("status") != "succeeded":
            continue
        step_id = s.get("step_id")
        kind = s.get("kind")
        dur = s.get("duration_ms")
        if isinstance(dur, (int, float)):
            if step_id in _DET_STEP_IDS:
                det_ms += dur
            elif kind in _LLM_STEP_KINDS:
                llm_ms += dur
        if kind == "batch":
            finder_criteria += int((s.get("outputs") or {}).get("criteria_count") or 0)
            step_usage = (s.get("outputs") or {}).get("_usage")
            if isinstance(step_usage, dict):
                per_call = step_usage.get("per_call") or []
                usage_per_call += [r for r in per_call if isinstance(r, dict)]
            batch_plan = (s.get("outputs") or {}).get("batch_plan")
            if isinstance(batch_plan, dict):
                batch_plans.append(batch_plan)
        elif kind == "agent":
            agent_calls += 1
            if step_id == STEP_VERIFY:
                verify_requests += int(
                    ((s.get("outputs") or {}).get("_usage") or {}).get("requests") or 0
                )
    metrics = {
        "det_ms": round(det_ms, 1),
        "llm_ms": round(llm_ms, 1),
        "total_ms": round(total_ms, 1),
        "llm_calls": finder_criteria + agent_calls,
        # Pass-2 verify step usage: model requests (~tool-call cycles) the verifier actually
        # consumed, so headroom vs the per-finding budget (`step_budget_per_item`) is observable.
        "verify_requests": verify_requests,
        "claim_path": "no-llm/no-network (structural; the fast claim check is a local HMAC verify)",
    }
    coverage = verdict.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
        verdict["coverage"] = coverage
    if usage_per_call:
        # d52a: fold the Pass-1/prerequisite token totals into the metrics and attach the
        # raw records + per-criterion derivation for the sidecar (coverage.usage).
        from rebar.llm.plan_review.pass1 import aggregate_usage

        usage_agg = aggregate_usage(usage_per_call)
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            metrics[field] = usage_agg["totals"][field]
        coverage["usage"] = {
            "per_call": usage_agg["per_call"],
            "per_criterion": usage_agg["per_criterion"],
        }
    _attach_read_set(coverage, rec)
    coverage["metrics"] = metrics
    _attach_discovery_trace(coverage, batch_plans)


def _attach_discovery_trace(coverage: dict[str, Any], batch_plans: list[dict[str, Any]]) -> None:
    """Expose the batch discovery journal for sidecar retry evidence.

    The reducer ignores it, and surfaced output strips it after sidecar persistence.
    """
    trace: list[dict[str, Any]] = []
    resumed = 0
    total = 0
    for plan in batch_plans:
        unit_trace = plan.get("discovery_trace")
        if isinstance(unit_trace, list):
            trace += [u for u in unit_trace if isinstance(u, dict)]
        checkpoint = plan.get("checkpoint")
        if isinstance(checkpoint, dict):
            resumed += int(checkpoint.get("chunks_resumed") or 0)
            total += int(checkpoint.get("chunks_total") or 0)
    if trace:
        coverage["discovery_trace"] = trace
        coverage["checkpoint"] = {"chunks_resumed": resumed, "chunks_total": total}


def strip_surfaced_journal(coverage: Any) -> None:
    """Remove persisted retry-journal keys before surfacing a verdict; tolerate non-dicts."""
    if isinstance(coverage, dict):
        coverage.pop("discovery_trace", None)
        coverage.pop("checkpoint", None)


def _attach_read_set(coverage: dict[str, Any], rec) -> None:
    """Record normalized repository paths observed across successful LLM steps.

    Set ``read_set_recorded`` only after a real fetch; absent telemetry must retain the
    fail-safe whole-HEAD fallback rather than masquerade as an empty read set.
    """
    fetches: list[dict[str, Any]] = []
    for s in rec.steps:
        if not isinstance(s, dict) or s.get("status") != "succeeded":
            continue
        if s.get("kind") not in _LLM_STEP_KINDS:
            continue
        step_usage = (s.get("outputs") or {}).get("_usage")
        if isinstance(step_usage, dict):
            fetches += [f for f in step_usage.get("distinct_fetches") or [] if isinstance(f, dict)]
    if not fetches:
        return
    try:
        from rebar.llm.plan_review.manifest import _hash_basis
        from rebar.llm.plan_review.read_set import normalize_read_set

        normalized = normalize_read_set(fetches, base=_hash_basis(None))
        if not normalized:
            # Fetches happened but none survived normalization — e.g. the pass only SEARCHED,
            # or read outside the repo. The review did consult code, so a blast-radius-only
            # scope would be fail-open; leave the whole-HEAD fallback in force.
            return
        coverage["read_set"] = normalized
        coverage["read_set_recorded"] = True
    except Exception:  # noqa: BLE001 — telemetry is never allowed to fail the gate
        logger.warning("read-set normalization failed; leaving currency unscoped")


def _recover_plan_review_coach_failure(rec, cfg, *, error) -> dict[str, Any] | None:
    """Recover a decided verdict from a coach-only failure, with empty coaching.

    Return ``None`` when decision also failed so the caller degrades to INDETERMINATE.
    """
    from rebar.llm import findings as _findings
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    # Latest-wins map of succeeded step outputs by their step id (frame-key tail).
    succeeded: dict[str, dict] = {}
    for s in rec.steps:
        if s.get("status") != "succeeded":
            continue
        fk = s.get("frame_key") or s.get("step_id") or ""
        succeeded[str(fk).rsplit("/", 1)[-1]] = s.get("outputs") or {}

    decide = succeeded.get(STEP_DECIDE)
    precheck = succeeded.get(STEP_PRECHECK)
    if not decide or not precheck or "blocking" not in decide:
        return None  # Pass-3 did not complete → the LLM tier failed, not just the coach

    parts = {
        k: list(decide.get(k) or [])
        for k in ("blocking", "surfaced", "overflow", "indeterminate", "dropped")
    }
    coverage = {
        "det": precheck.get("det_coverage") or {},
        "routing": (succeeded.get(STEP_ASSEMBLE) or {}).get("routing") or {},
        "llm_ran": True,
        "coach_error": str(error) if error else "pass-4 coach failed; verdict emitted without it",
        "hierarchy_incomplete": precheck.get("hierarchy_incomplete", False),
        "hierarchy_incomplete_detail": precheck.get("hierarchy_incomplete_detail", []),
    }
    pctx = PlanContext(
        ticket_id=str(precheck.get("canonical_id") or ""),
        ticket_type=str(precheck.get("ticket_type") or ""),
        title="",
        description="",
    )
    # Verification succeeded, so preserve its runner-stamped provenance instead of
    # recomputing a provider that may not have served the call.
    verdict = orchestrator.finalize_verdict(
        pctx,
        parts,
        coaching=[],
        coverage=coverage,
        runner_name=cfg.runner,
        model=cfg.model,
        provider_provenance=(succeeded.get(STEP_VERIFY) or {}).get("provider_provenance"),
    )
    return _findings.validate_structured(verdict, "plan_review_verdict")


def _recover_plan_review_verify_failure(rec, cfg, *, error) -> dict[str, Any] | None:
    """Recover finder results after verification or decision fails.

    Preserve findings as unverified INDETERMINATE with ``verify_failed``; blocking-enabled
    findings still fail closed. Return ``None`` if finders also failed.
    """
    from rebar.llm import findings as _findings
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    succeeded: dict[str, dict] = {}
    for s in rec.steps:
        if s.get("status") != "succeeded":
            continue
        fk = s.get("frame_key") or s.get("step_id") or ""
        succeeded[str(fk).rsplit("/", 1)[-1]] = s.get("outputs") or {}

    finders = succeeded.get(STEP_FINDERS)
    precheck = succeeded.get(STEP_PRECHECK)
    if not finders or not precheck or STEP_DECIDE in succeeded:
        # finders did not run (genuine LLM-tier failure), or decide DID run (a different
        # failure the coach-recovery handles) → not a verify-only failure.
        return None
    pass1 = list(finders.get("findings") or [])
    if not pass1:
        return None  # no findings to preserve → nothing to recover; let it degrade

    # Send preserved findings through Pass-3 with empty verifications, reusing the kernel's
    # schema-valid indeterminate decision instead of inventing a partial shape.
    decided = orchestrator.pass3_over_findings(
        pass1, {}, execution_review=precheck.get("review_phase", "planning") == "execution"
    )
    parts = orchestrator.partition_findings(
        list(precheck.get("det_blocking") or []),
        list(precheck.get("det_advisory") or []),
        decided,
    )
    coverage = {
        "det": precheck.get("det_coverage") or {},
        "routing": (succeeded.get(STEP_ASSEMBLE) or {}).get("routing") or {},
        "llm_ran": True,
        "verify_failed": True,
        "verify_error": str(error)
        if error
        else "pass-2 verify failed; findings preserved unverified",
        "hierarchy_incomplete": precheck.get("hierarchy_incomplete", False),
        "hierarchy_incomplete_detail": precheck.get("hierarchy_incomplete_detail", []),
    }
    pctx = PlanContext(
        ticket_id=str(precheck.get("canonical_id") or ""),
        ticket_type=str(precheck.get("ticket_type") or ""),
        title="",
        description="",
    )
    # Verification produced no record, so omit provider_provenance; configuration cannot
    # truthfully identify a call that never completed.
    verdict = orchestrator.finalize_verdict(
        pctx, parts, coaching=[], coverage=coverage, runner_name=cfg.runner, model=cfg.model
    )
    return _findings.validate_structured(verdict, "plan_review_verdict")


def _criteria_config_failure(rec) -> str | None:
    """Return the original diagnostic for a typed criteria-config assemble failure.

    Only the exact failed assemble frame and stable structured marker qualify.  Other scripted
    failures — including failures with similar prose — continue through the generic recovery
    path unchanged.
    """
    for step in reversed(rec.steps):
        frame_key = step.get("frame_key") or step.get("step_id") or ""
        if step.get("status") != "failed" or str(frame_key).rsplit("/", 1)[-1] != STEP_ASSEMBLE:
            continue
        outputs = step.get("outputs") or {}
        if outputs.get("failure_kind") != CRITERIA_CONFIG_FAILURE_KIND:
            return None
        diagnostic = outputs.get("failure_diagnostic")
        return str(diagnostic) if diagnostic is not None else ""
    return None


def _config_fault_plan_review_verdict(
    ctx, cfg, *, error: str, advisory_cap: int, runner_name: str | None
) -> dict[str, Any]:
    """Fail closed for a local criteria/config fault without claiming an LLM outage."""
    from rebar.llm.plan_review import det_floor, orchestrator

    det_results = det_floor.run_det_floor(ctx)
    det_blocks = det_floor.det_blocking_findings(det_results)
    det_advisories = det_floor.det_advisory_findings(det_results)
    config_error = (
        "The running installed rebar build is suspect because it could not load this "
        "repository's criteria configuration. Use the repository checkout build and retry "
        f"the plan review. Original diagnostic: {error}"
    )
    coverage = {
        "det": det_floor.det_coverage(det_results),
        "llm_ran": False,
        "config_fault": True,
        "config_fault_kind": CRITERIA_CONFIG_FAILURE_KIND,
        "config_error": config_error,
        "hierarchy_incomplete": getattr(ctx, "hierarchy_incomplete", False),
        "hierarchy_incomplete_detail": getattr(ctx, "hierarchy_incomplete_detail", []),
    }
    parts = orchestrator.partition_findings(
        det_blocks, det_advisories, [], advisory_cap=advisory_cap
    )
    # No provider provenance: no LLM frame ran, so no provider served this verdict.
    return orchestrator.finalize_verdict(
        ctx, parts, coaching=[], coverage=coverage, runner_name=runner_name, model=cfg.model
    )


def _degraded_plan_review_verdict(
    ctx, cfg, *, error, advisory_cap: int, runner_name: str | None
) -> dict[str, Any]:
    """The unsigned INDETERMINATE verdict a systemic LLM outage degrades to — the SAME
    shape ``run_review`` produces (DET floor ran, LLM did not): DET findings partitioned,
    ``coverage.llm_unavailable=True`` (so ``finalize_verdict`` ⇒ INDETERMINATE and
    ``review_plan`` never signs it)."""
    from rebar.llm import failure as _failure
    from rebar.llm.plan_review import det_floor, orchestrator

    det_results = det_floor.run_det_floor(ctx)
    det_blocks = det_floor.det_blocking_findings(det_results)
    det_advisories = det_floor.det_advisory_findings(det_results)
    # Persist structured outage disposition when an error has an outcome, enabling retryable
    # exit 11. String-only failures remain plain INDETERMINATE.
    outcome = _failure.outcome_of(error)
    coverage = {
        "det": det_floor.det_coverage(det_results),
        "llm_ran": False,
        **_failure.degrade_cause_flags(error),
        "llm_error": str(error),
        **_failure.resolution_fields(outcome),
        "hierarchy_incomplete": getattr(ctx, "hierarchy_incomplete", False),
        "hierarchy_incomplete_detail": getattr(ctx, "hierarchy_incomplete_detail", []),
    }
    _failure.log_degrade(outcome, gate="plan-review", ticket_id=getattr(ctx, "ticket_id", None))
    parts = orchestrator.partition_findings(
        det_blocks, det_advisories, [], advisory_cap=advisory_cap
    )
    # NO `provider_provenance`, deliberately: coverage.llm_unavailable means no provider
    # answered at all, so there is no record to carry and no honest one to build (343b).
    return orchestrator.finalize_verdict(
        ctx, parts, coaching=[], coverage=coverage, runner_name=runner_name, model=cfg.model
    )


def _cancelled_plan_review_verdict(ctx, cfg, *, scope) -> dict[str, Any]:
    """Build an unsigned, sidecar-free INDETERMINATE for a stale mid-run cancellation.

    It bypasses floors and signing; withholding the sidecar avoids advancing the revision
    pinned by the next review.
    """
    from rebar.llm.plan_review.claimability import indeterminate_verdict

    seam = getattr(scope, "seam", None)
    reason = (
        "the ticket's own plan material changed while the review was running; "
        "the remaining passes were cancelled (everything reviewed before the edit is stale)"
    )
    remediation = (
        "The plan was edited mid-review, so this run was cancelled without signing. "
        "Re-run `rebar review-plan` against the settled plan; no plan-review "
        "attestation was signed."
    )
    return indeterminate_verdict(
        getattr(ctx, "ticket_id", ""),
        ticket_type=getattr(ctx, "ticket_type", ""),
        finding={"id": "plan-review-cancelled-stale", "reason": reason, "seam": seam},
        coverage_extra={"cancelled": {"reason": reason, "seam": seam}},
        signature_reason="cancelled-stale",
        remediation=remediation,
        cfg=cfg,
    )
