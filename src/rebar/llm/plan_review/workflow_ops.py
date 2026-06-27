"""Scripted ``uses`` ops that express the plan-review gate AS a v3 engine workflow (epic B,
story B2).

These are THIN adapters over the already-correct, already-tested bespoke pipeline in
:mod:`rebar.llm.plan_review` — each op delegates to the shared units
(:mod:`.det_floor`, :mod:`.registry`, :func:`.orchestrator.route_criteria` /
``partition_findings`` / ``pass3_over_findings`` / ``finalize_verdict``,
:func:`.passes.render_coach_notes`) rather than re-implementing the gate. So the
workflow path is structurally equivalent to ``orchestrator.run_review`` (exact
behavioural PARITY is a separate story, B4). The workflow shape (mirrors the B3
completion gate):

    plan_review_precheck (uses)            # DET floor P1-P9
      └─ branch on `run_llm`:
           then: plan_review_assemble_criteria (uses)   # route_criteria → inclusion booleans
                 → batch <plan-review-finder>           # Pass-1 (ProductionBatchRunner)
                 → verify  <prompt: plan-review-verifier># Pass-2 (one aggregate call)
                 → plan_review_decide (uses)            # too_big/shed routing + Pass-3
                 → coach   <prompt: plan-review-coach>   # Pass-4 LLM move picks
                 → plan_review_coach (uses)             # render + assemble the verdict
           else: plan_review_passthrough (uses)         # the deterministic short-circuit verdict

Like B3 the short-circuit is a `branch` (an `if:`-skipped step's outputs cannot be
referenced): a DET block / an exempt type / a too-big plan reaches the ELSE arm and the
(billable) LLM steps NEVER run. Signing is NOT done here (deferred to B5).

The op bodies lazy-import the plan-review units so importing this module only runs the
registration decorators (import-light, no heavy LLM deps, no import cycle).
"""

from __future__ import annotations

from typing import Any

from rebar.llm.workflow.executor import StepContext, register_step

_OUTPUT_SCHEMA = "plan_review_verdict"


def _ticket_id(ctx: StepContext) -> str:
    tid = ctx.inputs.get("ticket_id") or ctx.target_ticket
    if not tid:
        raise ValueError(
            f"step {ctx.step_id!r} needs a ticket: pass `with: {{ticket_id: ...}}` or run "
            f"the workflow against a target ticket"
        )
    return str(tid)


@register_step(
    "plan_review_precheck",
    input_schema="plan_review_precheck_input",
    output_schema="plan_review_precheck_output",
    description=(
        "The deterministic Layer-1 floor (P1-P9) of the plan-review gate. Emits `run_llm` "
        "(true → the four-pass LLM review should run) and, when it should NOT (an exempt "
        "ticket type, or a P1/P5/P8 DET block), the terminal short-circuit plan_review_verdict "
        "so the billable LLM passes never run. Wraps rebar.llm.plan_review without duplicating it."
    ),
)
def plan_review_precheck(ctx: StepContext) -> dict[str, Any]:
    """Run the DET floor; short-circuit to a deterministic verdict on exempt/blocking."""
    from . import det_floor, orchestrator

    tid = _ticket_id(ctx)
    pctx = orchestrator.assemble_context(tid, repo_root=ctx.repo_root)
    base = {
        "canonical_id": pctx.ticket_id,
        "ticket_type": pctx.ticket_type,
        "det_blocking": [],
        "det_advisory": [],
        "det_coverage": {},
    }

    # Exempt types short-circuit to a PASS (no review runs) — mirrors run_review.
    if pctx.ticket_type in ("bug", "session_log"):
        reason = (
            "bug tickets are exempt from the plan-review gate"
            if pctx.ticket_type == "bug"
            else "session_log tickets are gate-exempt"
        )
        return {
            **base,
            "run_llm": False,
            "verdict": orchestrator._exempt_verdict(pctx, reason=reason),
        }

    det_results = det_floor.run_det_floor(pctx)
    det_blocks = det_floor.det_blocking_findings(det_results)
    det_advisories = det_floor.det_advisory_findings(det_results)
    det_cov = det_floor.det_coverage(det_results)
    base = {
        **base,
        "det_blocking": det_blocks,
        "det_advisory": det_advisories,
        "det_coverage": det_cov,
    }

    # P1/P5/P8 reconcile with bespoke run_review (story B5): the bespoke gate STOPS before
    # the LLM tier ONLY when P8 says the plan is too big to review at all (any LLM review
    # would see a plan that doesn't fit). A P1/P5 DET block does NOT stop the LLM — bespoke
    # still runs the four-pass review and MERGES the DET blocks at decide-time. So:
    #   * P8-too-big  → short-circuit (ELSE arm): a BLOCK verdict from the DET blocks, no LLM.
    #   * any other DET block (P1/P5) → run the LLM (THEN arm); det_blocking flows into
    #     plan_review_decide, which merges it via partition_findings → a BLOCK verdict that
    #     ALSO carries the LLM advisories + coaching (matching run_review).
    p8_too_big = any(
        getattr(r, "id", None) == "P8" and getattr(r, "blocked", False) for r in det_results
    )
    if p8_too_big:
        from rebar.llm.config import LLMConfig

        cfg = LLMConfig.from_env(repo_root=ctx.repo_root)
        parts = orchestrator.partition_findings(
            det_blocks, det_advisories, [], advisory_cap=orchestrator.DEFAULT_ADVISORY_CAP
        )
        verdict = orchestrator.finalize_verdict(
            pctx,
            parts,
            coaching=[],
            coverage={"det": det_cov, "llm_ran": False},
            runner_name=cfg.runner,
            model=cfg.model,
        )
        return {**base, "run_llm": False, "verdict": verdict}
    return {**base, "run_llm": True, "verdict": None}


@register_step(
    "plan_review_assemble_criteria",
    input_schema="plan_review_assemble_criteria_input",
    output_schema="plan_review_assemble_criteria_output",
    description=(
        "Route the LLM criteria for the ticket (proportionate scrutiny + overlay triggering) "
        "via route_criteria, and emit a per-criterion `include_<ID>` boolean the batch step's "
        "`when` reads (the INCLUDED set drives the Pass-1 finder batch). Plus the routing record "
        "(single-turn vs agent-tier) for coverage. The single source of routing truth is "
        "route_criteria — this op never re-implements applies()/overlay filtering."
    ),
)
def plan_review_assemble_criteria(ctx: StepContext) -> dict[str, Any]:
    """route_criteria(ctx) → {include_<ID>: bool, ..., routing}. The included criteria are
    gated INTO the batch by their `when: ${{ steps.assemble.outputs.include_<ID> }}`."""
    from . import orchestrator, registry

    tid = _ticket_id(ctx)
    pctx = orchestrator.assemble_context(tid, repo_root=ctx.repo_root)
    single, agent = orchestrator.route_criteria(pctx)
    included = {c["id"] for c in single + agent}
    # ISF is fed the linked session log by the finder itself (never a rubric chunk), so it is
    # never a batch criterion — excluded from the inclusion vocabulary here.
    canonical = sorted(set(registry.CANONICAL_LLM) - {"ISF"})
    out: dict[str, Any] = {f"include_{cid}": (cid in included) for cid in canonical}
    out["routing"] = {
        "single_turn": [c["id"] for c in single],
        "agent_tier": [c["id"] for c in agent],
    }
    return out


@register_step(
    "plan_review_grounding",
    input_schema="plan_review_grounding_input",
    output_schema="plan_review_grounding_output",
    description=(
        "Emit `code_grounded` = does ANY Pass-1 finding cite a CODEBASE_GROUNDED criterion "
        "(E4/G1G2/A1/G6)? This is the boolean the dynamic Pass-2 verify branch reads: when "
        "true the workflow runs the AGENTIC verifier (tools, re-grounds against real code), "
        "matching bespoke run_review's pass2_verify(agentic=grounded); when false the cheaper "
        "single-turn verifier. Mirrors the bespoke grounding test EXACTLY (findings-based, not "
        "inclusion-based) so the agentic-vs-single-turn call-mode is parity-faithful."
    ),
)
def plan_review_grounding(ctx: StepContext) -> dict[str, Any]:
    """code_grounded = any finding cites a CODEBASE_GROUNDED criterion (E4/G1G2/A1/G6).

    Mirrors orchestrator.run_review (passes.pass3 site) EXACTLY: the size-ladder's `_too_big`
    findings and budget-`_shed` findings are EXCLUDED first (bespoke filters them out before
    computing `grounded`), so a code-grounded criterion that was SHED does NOT make verify
    agentic — the verifier only re-grounds findings that actually ran."""
    from . import registry

    findings = [
        f
        for f in (ctx.inputs.get("findings") or [])
        if isinstance(f, dict) and not f.get("_too_big") and not f.get("_shed")
    ]
    grounded = any(
        any(c in registry.CODEBASE_GROUNDED for c in (f.get("criteria") or [])) for f in findings
    )
    return {"code_grounded": bool(grounded)}


@register_step(
    "plan_review_verify_inputs",
    input_schema="plan_review_verify_inputs_input",
    output_schema="plan_review_verify_inputs_output",
    description=(
        "Emit the {{plan}} text + the Pass-2 verifier INSTRUCTIONS for the verify prompt step "
        "on the LIVE path: `plan` = assemble_context(ticket_id).plan_text and `instructions` = "
        "the SAME per-finding listing the bespoke pass2_verify builds (passes.verify_instructions) "
        "over ALL Pass-1 findings — one aggregate call (a >12-finding plan is a single workflow "
        "verify, not bespoke's batches-of-12; a documented divergence). Reuses passes.verify_"
        "instructions so the listing format never diverges from the bespoke gate."
    ),
)
def plan_review_verify_inputs(ctx: StepContext) -> dict[str, Any]:
    """Emit {plan, instructions} feeding the workflow's Pass-2 verify prompt step."""
    from . import orchestrator, passes

    tid = _ticket_id(ctx)
    pctx = orchestrator.assemble_context(tid, repo_root=ctx.repo_root)
    findings = list(ctx.inputs.get("findings") or [])
    instructions = passes.verify_instructions(list(enumerate(findings)))
    return {"plan": pctx.plan_text, "instructions": instructions}


@register_step(
    "plan_review_coach_inputs",
    input_schema="plan_review_coach_inputs_input",
    output_schema="plan_review_coach_inputs_output",
    description=(
        "Emit the {{plan}} text + the Pass-4 coach INSTRUCTIONS for the coach_notes prompt step "
        "on the LIVE path: `plan` = assemble_context(ticket_id).plan_text and `instructions` = "
        "the SAME move-registry + surviving-findings listing the bespoke pass4_coach builds "
        "(passes.coach_instructions) over the surviving advisory findings. Reuses passes.coach_"
        "instructions + passes.load_move_registry so the format never diverges from bespoke."
    ),
)
def plan_review_coach_inputs(ctx: StepContext) -> dict[str, Any]:
    """Emit {plan, instructions} feeding the workflow's Pass-4 coach prompt step."""
    from . import orchestrator, passes

    tid = _ticket_id(ctx)
    pctx = orchestrator.assemble_context(tid, repo_root=ctx.repo_root)
    surviving = list(ctx.inputs.get("surviving") or [])
    moves = passes.load_move_registry(ctx.repo_root)
    instructions = passes.coach_instructions(surviving, moves)
    return {"plan": pctx.plan_text, "instructions": instructions}


@register_step(
    "plan_review_decide",
    input_schema="plan_review_decide_input",
    output_schema="plan_review_decide_output",
    description=(
        "Pass-3 of the gate: route the size-ladder's too_big findings (DET-style BLOCKS) and "
        "budget-shed findings (INDETERMINATE), run the deterministic pass3_decide over the rest "
        "(Pass-1 findings + the Pass-2 verifier's verifications), then merge the DET-floor "
        "findings and apply the advisory cap. Emits the verdict partition the coach assembles. "
        "Reuses orchestrator.pass3_over_findings + partition_findings (no duplicated decision)."
    ),
)
def plan_review_decide(ctx: StepContext) -> dict[str, Any]:
    """too_big/shed routing + Pass-3 over (batch findings, verifier verifications) →
    merge DET findings → cap → the verdict partition (blocking/surfaced/overflow/...)."""
    from . import orchestrator

    findings = list(ctx.inputs.get("findings") or [])
    raw_verifs = list(ctx.inputs.get("verifications") or [])
    det_blocks = list(ctx.inputs.get("det_blocking") or [])
    det_advisories = list(ctx.inputs.get("det_advisory") or [])

    # The Pass-2 verifier (the workflow's `verify` prompt step) emits a flat list of
    # `{index, severity_attributes, binary}`; reshape it to the `{index: {...}}` map Pass-3
    # consumes (the same shape passes.pass2_verify returns in the bespoke path).
    verifs: dict[int, dict[str, Any]] = {}
    for v in raw_verifs:
        idx = v.get("index") if isinstance(v, dict) else None
        if isinstance(idx, int):
            verifs[idx] = {
                "severity_attributes": v.get("severity_attributes", {}) or {},
                "binary": v.get("binary", {}) or {},
            }

    # The size-ladder's "too big at the largest model" findings are DET-style BLOCKS;
    # budget-shed findings are pre-decided INDETERMINATE. Both bypass Pass-2/3. The rest are
    # decided by pass3_over_findings with verifications re-keyed to the rest's 0-based index
    # (the verifier ran over the full batch list; we pick the matching verifications).
    too_big = [
        {
            **f,
            "decision": "block",
            "severity": "critical",
            "priority": 1.0,
            "validity": 1.0,
            "impact": 1.0,
        }
        for f in findings
        if f.get("_too_big")
    ]
    shed = [f for f in findings if f.get("_shed")]
    rest: list[dict[str, Any]] = []
    rest_verifs: dict[int, dict[str, Any]] = {}
    for i, f in enumerate(findings):
        if f.get("_too_big") or f.get("_shed"):
            continue
        rest_verifs[len(rest)] = verifs.get(i)
        rest.append(f)
    decided = [*too_big, *shed, *orchestrator.pass3_over_findings(rest, rest_verifs)]

    parts = orchestrator.partition_findings(
        det_blocks, det_advisories, decided, advisory_cap=orchestrator.DEFAULT_ADVISORY_CAP
    )
    return dict(parts)


@register_step(
    "plan_review_coach",
    input_schema="plan_review_coach_input",
    output_schema=_OUTPUT_SCHEMA,
    description=(
        "Pass-4 + verdict assembly: render the coach prompt's raw move picks into deterministic "
        "affirmative coaching (locked move templates; the LLM never authors prose), then assemble "
        "the terminal plan_review_verdict (verdict + findings + coaching + coverage) via shared "
        "finalize_verdict. NO signing (B5). Reuses passes.render_coach_notes + finalize_verdict."
    ),
)
def plan_review_coach(ctx: StepContext) -> dict[str, Any]:
    """Render coaching from the coach step's raw notes + assemble the plan_review_verdict."""
    from rebar.llm import findings as _findings
    from rebar.llm.config import LLMConfig

    from . import orchestrator, passes
    from .det_floor import PlanContext

    cfg = LLMConfig.from_env(repo_root=ctx.repo_root)
    parts = {
        "blocking": list(ctx.inputs.get("blocking") or []),
        "surfaced": list(ctx.inputs.get("surfaced") or []),
        "overflow": list(ctx.inputs.get("overflow") or []),
        "indeterminate": list(ctx.inputs.get("indeterminate") or []),
        "dropped": list(ctx.inputs.get("dropped") or []),
    }
    moves = passes.load_move_registry(ctx.repo_root)
    coaching = passes.render_coach_notes(list(ctx.inputs.get("notes") or []), moves)

    # finalize_verdict needs only ctx.ticket_id + ctx.ticket_type — a minimal context (no
    # rebar read) suffices here (the precheck already canonicalized the id/type).
    pctx = PlanContext(
        ticket_id=str(ctx.inputs.get("canonical_id") or _ticket_id(ctx)),
        ticket_type=str(ctx.inputs.get("ticket_type") or ""),
        title="",
        description="",
    )
    coverage = {
        "det": ctx.inputs.get("det_coverage") or {},
        "routing": ctx.inputs.get("routing") or {},
        "llm_ran": True,
    }
    verdict = orchestrator.finalize_verdict(
        pctx, parts, coaching=coaching, coverage=coverage, runner_name=cfg.runner, model=cfg.model
    )
    return _findings.validate_structured(verdict, _OUTPUT_SCHEMA)


@register_step(
    "plan_review_passthrough",
    input_schema="plan_review_passthrough_input",
    output_schema=_OUTPUT_SCHEMA,
    description=(
        "Emit the precheck's deterministic short-circuit plan_review_verdict verbatim — the "
        "branch ELSE arm taken when the LLM review is skipped (an exempt type, or a P1/P5/P8 DET "
        "block). Keeps the workflow's terminal output a plan_review_verdict on both arms."
    ),
)
def plan_review_passthrough(ctx: StepContext) -> dict[str, Any]:
    """Pass the precheck's deterministic verdict through as the terminal output."""
    verdict = ctx.inputs.get("verdict")
    if not isinstance(verdict, dict):
        raise ValueError(
            f"step {ctx.step_id!r} expects a `verdict` object from the precheck; "
            f"got {type(verdict)}"
        )
    return dict(verdict)
