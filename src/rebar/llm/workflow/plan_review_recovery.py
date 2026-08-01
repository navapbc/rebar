"""Plan-review run-record -> verdict reconstruction (ticket 1484).

Everything that turns a finished gate run-record — or a FAILED one — back into a
``plan_review_verdict``: the named step-id vocabulary those lookups key off, the metrics
reconstruction, the two mid-tail recoveries, and the outage degrade.

Extracted from ``gate_dispatch`` because that module sat at 799 LOC against the 800-LOC hard cap
with ONE line of headroom, while three of the five ``orchestrator.finalize_verdict`` call sites
live in this cluster and story 343b must add an argument at each. ``gate_dispatch`` re-imports
every name below, so they remain ITS module-globals and the existing attribute-access references
and monkeypatch targets resolve unchanged — the same zero-test-edit mechanism task 2682 and
ticket 3a98 used.

STRICT LEAF: imports nothing from ``gate_dispatch`` (which imports this module), and every rebar
import stays lazy INSIDE the function bodies, so the module level cannot close an import cycle.

Note the orchestrator reference style is load-bearing: these functions do a lazy
``from rebar.llm.plan_review import orchestrator`` and then ``orchestrator.<name>`` ATTRIBUTE
access. A lifecycle test monkeypatches ``orchestrator.pass3_over_findings`` and then calls into
here; flattening those to bare-name imports would bind the original at import time and silently
defeat the patch.
"""

from __future__ import annotations

from typing import Any

# Named step ids for gates/plan-review.yaml. The dispatcher's mid-tail RECOVERY and the metrics
# reconstruction below key off these ids (a run's succeeded-step partition is looked up by id); a
# YAML rename that dropped one would make the lookup silently return None, so a recoverable run
# would degrade to a hollow INDETERMINATE with NO error (the exact silent-failure this centralizes
# away). Keep the literals here, once, and validate them against the loaded doc at dispatch time
# (see `_validate_gate_step_ids`) so a rename is caught LOUDLY instead of silently degraded.
STEP_PRECHECK = "precheck"
STEP_ASSEMBLE = "assemble"
STEP_FINDERS = "finders"
STEP_VERIFY = "verify"
STEP_DECIDE = "decide"
STEP_COACH = "coach"

# The step ids the recovery/metrics logic depends on being present in the loaded gate doc.
_PLAN_REVIEW_REQUIRED_STEP_IDS = frozenset(
    {STEP_PRECHECK, STEP_ASSEMBLE, STEP_FINDERS, STEP_VERIFY, STEP_DECIDE, STEP_COACH}
)


class GateContractError(RuntimeError):
    """A loaded gate workflow is missing a step id the dispatcher's recovery/metrics logic
    references — i.e. a YAML step was renamed/dropped out from under the recovery code. Raised
    LOUDLY at dispatch (NOT silently degraded to INDETERMINATE) so the break surfaces where it
    can be fixed instead of quietly discarding real findings."""


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
    """Fail LOUDLY if the loaded gate doc is missing any step id the dispatcher references.

    A step-id rename in ``gates/<gate_name>.yaml`` would otherwise make the recovery lookups
    silently return ``None`` and degrade a recoverable run to INDETERMINATE. Called at dispatch
    time (right after the doc is loaded) so drift is caught here, not swallowed downstream."""
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
    """Reinstate ``coverage['metrics']`` on the WORKFLOW plan-review path (toy-kink-ire).

    B-RETIRE removed bespoke ``run_review``, the only producer of the per-pass latency/cost
    metrics (db7b AC5). This reconstructs the equivalent from the workflow run's recorder
    step timings (added by the interpreter) so the sidecar carries them again for passive
    latency/cost-target refinement:

    - ``det_ms``    — wall-clock of the deterministic floor (the ``precheck`` step).
    - ``llm_ms``    — wall-clock of the billable LLM tier (the ``agent``/``batch`` steps:
                      Pass-1 ``finders``, Pass-2 ``verify``, Pass-4 ``coach_notes``).
    - ``total_ms``  — the whole run's wall-clock (measured around ``run_workflow``).
    - ``llm_calls`` — a cost proxy: the Pass-1 finder ``criteria_count`` + one per succeeded
                      agent step (``verify`` / ``coach_notes``). Mirrors run_review's proxy.
    - ``claim_path``— the structural marker (the fast claim check is a local HMAC verify,
                      LLM/network-free).

    Story d52a: a batch step's ``_usage`` output (the Pass-1 + prerequisite per-call usage
    aggregate the ProductionBatchRunner emits) is folded in — token totals into these
    metrics, the raw records + per-criterion derivation as ``coverage['usage']``.

    ``det_ms + llm_ms`` deliberately does NOT equal ``total_ms``: the scripted prep/decision
    steps (``assemble`` / ``grounding`` / ``verify_inputs`` / ``decide`` / ``coach_inputs`` /
    ``coach``) are non-LLM overhead, counted into neither tier — absorbed only into ``total_ms``
    (the same split the bespoke ``run_review`` reported).

    Mutates ``verdict['coverage']['metrics']`` in place (only that key; existing coverage is
    preserved). Tolerant of untimed/partial records (a missing ``duration_ms`` contributes 0)
    so it never raises inside the gate.
    """
    det_ms = 0.0
    llm_ms = 0.0
    finder_criteria = 0
    agent_calls = 0
    verify_requests = 0  # Pass-2 verifier model-request count — step usage vs its budget (bug 59bc)
    usage_per_call: list[dict[str, Any]] = []  # d52a: per-call records off the batch `_usage`
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
    coverage["metrics"] = metrics


def _recover_plan_review_coach_failure(rec, cfg, *, error) -> dict[str, Any] | None:
    """If the only failure was in the Pass-4 coach tail (Pass-3 ``decide`` succeeded),
    reassemble the verdict from the recorded ``decide`` partition with EMPTY coaching —
    the same non-fatal-coach result bespoke run_review emits. Returns None if ``decide``
    did not succeed (then the LLM tier genuinely failed → caller degrades to INDETERMINATE)."""
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
    # Pass-2 verify SUCCEEDED here (only the coach failed), so its outputs still carry the
    # runner-stamped record for the call that ran — carry it forward rather than recompute
    # one from cfg, which could name an endpoint/caps that never served this review.
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
    """If Pass-1 ``finders`` SUCCEEDED but Pass-2/3 did not (the verify step failed — e.g. the
    agentic verifier exhausted its step budget), reassemble the verdict from the Pass-1 findings
    PRESERVED as unverified → INDETERMINATE, with ``coverage.verify_failed`` (NOT
    ``llm_unavailable``). ``finalize_verdict`` then fails OPEN unless a preserved finding sits on
    a blocking-enabled criterion (bug 59bc). Returns None if ``finders`` did not succeed (then the
    LLM tier genuinely failed → caller degrades to INDETERMINATE)."""
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

    # Route the preserved Pass-1 findings through Pass-3 with EMPTY verifications: each finding
    # then takes pass3_decide(None) → the kernel's documented no-verification degrade
    # (decision=indeterminate, validity/impact/priority=0, severity=none, verification=None). This
    # reuses the existing decision path — the verdict stays schema-valid and NO new decision state
    # is introduced — rather than hand-stamping a partial finding shape.
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
    # NO `provider_provenance` here, deliberately: verify failed by construction, so no
    # verify-step record exists to carry. Synthesizing one from cfg would make the verdict
    # claim a provider served a verification that never ran — the misattribution this
    # record exists to remove. Absence is the honest answer (343b).
    verdict = orchestrator.finalize_verdict(
        pctx, parts, coaching=[], coverage=coverage, runner_name=cfg.runner, model=cfg.model
    )
    return _findings.validate_structured(verdict, "plan_review_verdict")


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
    # Disposition (story blackbear): when the raised error carries an ``.outcome`` (the genuine
    # outage paths — preflight / mid-run LLMUnavailableError), persist resolution_class/retryable/
    # diagnostic onto coverage so the CLI can map a retryable outage → exit 11. A string-error
    # tail (finders produced nothing) carries no outcome → no disposition → plain INDETERMINATE.
    outcome = _failure.outcome_of(error)
    coverage = {
        "det": det_floor.det_coverage(det_results),
        "llm_ran": False,
        "llm_unavailable": True,
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
    """The mid-run-cancelled verdict (story 2c89): an unsigned, sidecar-less
    INDETERMINATE carrying the ``plan-review-cancelled-stale`` finding. Built on the
    shared early-verdict shape (``claimability.indeterminate_verdict``), so — like the
    not-claimable fast-fail — ``review_plan`` returns it verbatim: no floors, no
    signing (monotone: a cancel only WITHHOLDS an attestation), and no sidecar emit
    (a sidecar write would advance the store revision the next review pins)."""
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
