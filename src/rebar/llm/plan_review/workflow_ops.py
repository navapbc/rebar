"""Scripted ``uses`` ops that express the plan-review gate AS a v3 engine workflow (epic B,
story B2).

These are THIN adapters over the shared, already-tested plan-review units in
:mod:`rebar.llm.plan_review` — each op delegates to those units
(:mod:`.det_floor`, :mod:`.registry`, :func:`.orchestrator.route_criteria` /
``partition_findings`` / ``pass3_over_findings`` / ``finalize_verdict``,
:func:`.review_coach.render_coach_notes`) rather than re-implementing the gate. This workflow
is now the SOLE plan-review gate (the bespoke ``orchestrator.run_review`` driver it once
mirrored was retired in story B-RETIRE). The workflow shape (mirrors the B3 completion
gate):

    plan_review_precheck (uses)            # DET floor P1-P11
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

import importlib
import logging
from typing import Any

from rebar.llm import review_kernel
from rebar.llm import step_failures as step_failure_sink
from rebar.llm.review_kernel import verify as review_verify
from rebar.llm.workflow.executor import StepContext, StepResult, register_step
from rebar.llm.workflow.plan_review_recovery import CRITERIA_CONFIG_FAILURE_KIND
from rebar.types import PLAN_REVIEW_BARE_EXEMPT_TYPES, PLAN_REVIEW_BUG_TIER_TYPES

logger = logging.getLogger(__name__)
review_coach = importlib.import_module("rebar.llm.review_kernel.coach")

# Register the extracted workflow adapters alongside this module's historical step registry
# population: `@register_step` populates a process-global registry as an IMPORT SIDE EFFECT, so
# `plan_review_decide` (moved to `decide_ops` by ticket b5fe) and the prerequisite-specific ops
# only exist for the engine if the modules holding them are imported. `workflow/steps.py`
# deliberately imports only `workflow_ops` and stays unaware of plan-review's internal file
# layout, so both imports belong here, not there.
from . import decide_ops as _decide_ops  # noqa: E402
from . import prerequisite_workflow_ops as _prerequisite_workflow_ops  # noqa: E402,F401

_OUTPUT_SCHEMA = "plan_review_verdict"
_BUG_TIER_BLOCKING_DET_CRITERIA = {"P1", "P4", "P10"}


def _bug_tier_keeps_blocking(finding: dict[str, Any]) -> bool:
    return bool(_BUG_TIER_BLOCKING_DET_CRITERIA.intersection(finding.get("criteria") or ()))


@register_step(
    "plan_review_precheck",
    input_schema="plan_review_precheck_input",
    output_schema="plan_review_precheck_output",
    description=(
        "The deterministic Layer-1 floor (P1-P11) of the plan-review gate. Emits `run_llm` "
        "(true → the four-pass LLM review should run) and, when it should NOT (an exempt "
        "ticket type, or a P1/P4-description/P5-cycle/P8/P10/P11 DET block), the terminal "
        "short-circuit "
        "plan_review_verdict so the billable LLM passes never run. Wraps rebar.llm.plan_review "
        "without duplicating it."
    ),
)
def plan_review_precheck(ctx: StepContext) -> dict[str, Any]:
    """Run the DET floor; short-circuit to a deterministic verdict on exempt/blocking."""
    from . import context_assembly, det_floor, orchestrator
    from .prerequisites import current_blocks

    tid = _decide_ops._ticket_id(ctx)
    pctx = context_assembly.assemble_context(tid, repo_root=ctx.repo_root)
    base: dict[str, Any] = {
        "canonical_id": pctx.ticket_id,
        "ticket_type": pctx.ticket_type,
        "review_phase": pctx.state.get("plan_review_phase", "planning"),
        "det_blocking": [],
        "det_advisory": [],
        "det_coverage": {},
        "hierarchy_incomplete": pctx.hierarchy_incomplete,
        "hierarchy_incomplete_detail": pctx.hierarchy_incomplete_detail,
        "subject_plan": pctx.plan_text,
        "prerequisites": current_blocks(),
        "relation_snapshot": current_blocks(),
    }

    # session_log / code_review / identity short-circuit to a bare exempt PASS (no review runs).
    # The membership is DERIVED in rebar.types (mirror F3-b, ticket 90cb-fe23-266e-41ac) as the
    # claim-gate exemption MINUS the types that take a review tier — never re-spelled here, so a
    # renamed TicketType member cannot silently switch this short-circuit off. A bug is exempt
    # from the CLAIM gate but NOT from review; it takes the light advisory tier below.
    if pctx.ticket_type in PLAN_REVIEW_BARE_EXEMPT_TYPES:
        return {
            **base,
            "run_llm": False,
            "verdict": orchestrator._exempt_verdict(
                pctx, reason=f"{pctx.ticket_type} tickets are gate-exempt"
            ),
        }

    # BUG REVIEW TIER (epic 6982 / R4): a bug no longer short-circuits to a bare exempt-PASS.
    # It gets a LIGHT ADVISORY review — the DET floor + the necessity probe (see
    # registry.BUG_TIER_CRITERIA; the assemble step restricts a bug's included LLM criteria to
    # it). P1/P10 readiness-shape failures and P4's description admission limit remain
    # authoritative DET blocks; the remaining DET findings are downgraded to advisory so a
    # well-formed bug still gets the restricted LLM tier instead of a bare exempt PASS. The
    # CLI claim-time
    # bug exemption (rebar._commands.gates) is unchanged — a bug still needs no signed attestation
    # to be claimed; this only makes an explicit review / gate run substantive instead of exempt.
    #
    # BLAST-RADIUS ESCALATION (ad0d B1): a bug whose persisted file_impact declares any
    # NON-TEST path skips this light-tier arm and falls through to the normal
    # (blocking-capable) path below — DET findings keep their real posture (the DET
    # short-circuit applies exactly as for a non-bug ticket) and route_criteria, keyed on
    # the same orchestrator.bug_blast_radius_escalates predicate, routes the FULL criteria
    # set. Coverage records the escalation (bug_tier: False + bug_blast_escalated: True).
    bug_tier = pctx.ticket_type in PLAN_REVIEW_BUG_TIER_TYPES
    escalated_bug = bug_tier and orchestrator.bug_blast_radius_escalates(
        pctx.state.get("file_impact")
    )
    if bug_tier and not escalated_bug:
        det_results = det_floor.run_det_floor(pctx)
        all_det_blocks = det_floor.det_blocking_findings(det_results)
        det_advisories = det_floor.det_advisory_findings(det_results)
        det_cov = det_floor.det_coverage(det_results)
        # The light tier may not discard the readiness floor: P1 and P10 are the exact
        # acceptance/testing checks a signed plan-review attestation must make deterministic.
        det_blocks = [finding for finding in all_det_blocks if _bug_tier_keeps_blocking(finding)]
        downgraded = [
            finding for finding in all_det_blocks if not _bug_tier_keeps_blocking(finding)
        ]
        det_advisories = [*downgraded, *det_advisories]
        det_cov = {**det_cov, "bug_tier": True}
    else:
        det_results = det_floor.run_det_floor(pctx)
        det_blocks = det_floor.det_blocking_findings(det_results)
        det_advisories = det_floor.det_advisory_findings(det_results)
        det_cov = det_floor.det_coverage(det_results)
        if escalated_bug:
            det_cov = {**det_cov, "bug_tier": False, "bug_blast_escalated": True}
    base = {
        **base,
        "det_blocking": det_blocks,
        "det_advisory": det_advisories,
        "det_coverage": det_cov,
    }

    # DET-floor short-circuit (story 228b, widening the B5 P8-only branch): ANY DET gate
    # producing a blocking finding stops the review BEFORE the LLM tier. A DET block
    # guarantees a BLOCK verdict, so running the four-pass LLM review first only spends
    # tokens on a foregone conclusion. The short-circuit reuses the same
    # partition_findings/finalize_verdict path, so the BLOCK verdict carries every DET
    # blocking finding (plus DET advisories) with coverage.llm_ran=False. A DET-passing
    # plan runs the LLM tier unchanged (THEN arm). gate_dispatch's outage path
    # (_degraded_plan_review_verdict) already passes ALL det_blocks the same way.
    if det_blocks:
        from rebar.llm.config import resolve_gate_config

        cfg = resolve_gate_config(ctx.repo_root)  # caller-resolved cfg (veiny-trout-brink)
        parts = orchestrator.partition_findings(
            det_blocks, det_advisories, [], advisory_cap=orchestrator.DEFAULT_ADVISORY_CAP
        )
        verdict = orchestrator.finalize_verdict(
            pctx,
            parts,
            coaching=[],
            coverage={
                "det": det_cov,
                "llm_ran": False,
                "hierarchy_incomplete": pctx.hierarchy_incomplete,
                "hierarchy_incomplete_detail": pctx.hierarchy_incomplete_detail,
            },
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
def plan_review_assemble_criteria(ctx: StepContext) -> StepResult | dict[str, Any]:
    """route_criteria(ctx) → {include_<ID>: bool, ..., routing}. The included criteria are
    gated INTO the batch by their `when: ${{ steps.assemble.outputs.include_<ID> }}`.

    A criteria-overlay/configuration failure is returned as a failed ``StepResult`` with a
    stable ``failure_kind``.  The workflow recorder therefore retains the exception's identity
    for the verdict dispatcher instead of reducing it to an indistinguishable error string.
    """
    from rebar.llm.criteria import CriteriaError

    from . import context_assembly, orchestrator, registry

    tid = _decide_ops._ticket_id(ctx)
    pctx = context_assembly.assemble_context(tid, repo_root=ctx.repo_root)
    # gate_log: every deterministic-gate skip (ticket 4ee2) as {criterion_id: rule_name},
    # merged below into routing.det_gated — the sidecar's coverage.routing carries it, so
    # a criterion skipped on total vocabulary absence (zero LLM routing) stays observable.
    gate_log: dict[str, str] = {}
    try:
        single, agent = orchestrator.route_criteria(pctx, gate_log=gate_log)
        # The EFFECTIVE vocabulary = canonical built-ins ∪ activated PROJECT criteria (from the
        # `.rebar/criteria_routing.json` overlay), resolved against the SAME root route_criteria
        # loaded (pctx.repo_root) so the vocab and the loaded criteria never diverge. ISF is fed
        # the linked session log by the finder itself (never a rubric chunk), so it is never a
        # batch criterion — excluded from the inclusion vocabulary here.
        # exec:DET criteria run in the deterministic phase (det_floor), NOT the LLM batch — so they
        # own NO `include_<ID>` batch slot. Exclude them (and ISF, fed the session log directly)
        # from the inclusion vocabulary, reading `exec` from the effective routing. Story 7f0d.
        _routing = registry.effective_routing(pctx.repo_root)

        def _is_det(cid: str) -> bool:
            return str((_routing.get(cid) or {}).get("exec", "")).upper() == "DET"

        effective = [
            cid
            for cid in registry.effective_criteria(pctx.repo_root)
            if cid != "ISF" and not _is_det(cid)
        ]
    except CriteriaError as exc:
        diagnostic = str(exc)
        return StepResult(
            outputs={
                "failure_kind": CRITERIA_CONFIG_FAILURE_KIND,
                "failure_diagnostic": diagnostic,
            },
            status="failed",
            error=diagnostic,
        )

    # `.`→`_` sanitizes a `project.<name>` id to a valid workflow output key; co-located with
    # the CONSUME-site `when` reference emitted in `project_criteria` below (built-in ids have
    # no dots, so their `include_<ID>` keys are byte-identical to before).
    def _key(cid: str) -> str:
        return "include_" + cid.replace(".", "_")

    # PROBE MODE (drift-refresh tripwire): when `probe_criteria` is set, FORCE exactly that
    # allowlist (the cheap E4+G1G2 probe), bypassing applies()/overlay routing — mirroring the
    # bespoke drift probe, which ran its probe criteria directly as finders regardless of
    # routing. Empty/absent → the full routed set (normal review). Restricted to effective ids
    # that own an include slot.
    probe = {str(c) for c in (ctx.inputs.get("probe_criteria") or [])}
    if probe:
        included = {cid for cid in effective if cid in probe}
    else:
        included = {c["id"] for c in single + agent}
    out: dict[str, Any] = {_key(cid): (cid in included) for cid in effective}
    # Built-in criteria fan out via the STATIC `criteria:` list in the gate YAML (each gated
    # by its `include_<ID>` key). Activated PROJECT criteria have no static YAML slot (the v3
    # `batch` schema is immutable), so the rebar-specific ProductionBatchRunner fans them in
    # from route_criteria — see production_batch_runner._project_criteria. The sanitized
    # `include_project_<name>` booleans above remain the coverage/routing record for them.
    out["routing"] = {
        "single_turn": [c["id"] for c in single if c["id"] in included],
        "agent_tier": [c["id"] for c in agent if c["id"] in included],
        "det_gated": gate_log,
    }
    if probe:
        out["routing"]["probe_criteria"] = sorted(included)
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

    The size-ladder's `_too_big` findings and budget-`_shed` findings are EXCLUDED first
    (they are filtered out before computing `grounded`), so a code-grounded criterion that was
    SHED does NOT make verify agentic — the verifier only re-grounds findings that actually
    ran (the same rule the shared `pass3_over_findings` site applies)."""
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
        "Emit the {{shared_prefix}} text + the Pass-2 verifier INSTRUCTIONS for the verify "
        "prompt step. `shared_prefix` = prompts.shared_plan_prefix(assemble_context(ticket_id)"
        ".plan_text) — the byte-identical plan-bearing leading prefix shared with the Pass-1 "
        "finder system prompt. `instructions` is a LIST of per-chunk "
        "listings (review_verify.verify_instructions, global indices preserved): ONE element "
        "in the "
        "common case (the whole request fits the verifier model window) — byte-identical to a "
        "single aggregate verify — and TOKEN-BUDGETED splits (sizing.verify_request_chunks, no "
        "magic count) only when the request would exceed the window. The verify prompt step runs "
        "once per element and merges the verifications by index; a finding too big to verify at "
        "the largest model is omitted → pass3 routes it to INDETERMINATE."
    ),
)
def plan_review_verify_inputs(ctx: StepContext) -> dict[str, Any]:
    """Emit {shared_prefix, instructions[]} feeding the workflow's Pass-2 verify prompt step. The
    `instructions` list has ONE element for the common (fits-the-window) case and is split into
    token-budgeted chunks (global indices preserved) when the request would exceed the verifier
    model's window — encapsulated chunking, not a workflow fan-out (epic solid-timer-unison WS3)."""
    from rebar import config as _config
    from rebar.llm.config import resolve_gate_config

    from . import _verifier_cfg, context_assembly, generation, sizing

    # Between-pass cancel probe (story 2c89): after the Pass-1 finders, before the
    # billable Pass-2 verify. OWN-material only — see generation.probe_cancel.
    generation.probe_cancel("post-finders")
    tid = _decide_ops._ticket_id(ctx)
    pctx = context_assembly.assemble_context(tid, repo_root=ctx.repo_root)
    findings = list(ctx.inputs.get("findings") or [])
    # Size against the RESOLVED verifier model (the Sonnet downgrade, operator override honored)
    # — the same model the verify prompt step runs under (gate_dispatch passes _verifier_cfg(cfg)).
    # resolve_gate_config returns the caller-resolved run config, not a per-op from_env
    # (veiny-trout-brink), so an explicit caller model sizes the verify request correctly.
    verify_model = _verifier_cfg(resolve_gate_config(ctx.repo_root)).model
    try:
        headroom = float(_config.compose_config(ctx.repo_root).verify.verify_window_headroom)
    except Exception:  # noqa: BLE001 — config unreadable → the documented default
        headroom = review_verify.DEFAULT_VERIFY_WINDOW_HEADROOM
    chunks, _omitted = sizing.verify_request_chunks(findings, model=verify_model, headroom=headroom)
    # `_omitted` indices are intentionally left out of every chunk → no verification for them →
    # pass3_decide(None) marks them INDETERMINATE (never silently dropped). When there are no
    # findings (or all were omitted), still emit ONE (empty) chunk so the verify step makes its
    # single aggregate call returning an empty `verifications` list — the prior behavior the
    # decide step depends on.
    instructions = [review_verify.verify_instructions(chunk) for chunk in (chunks or [[]])]
    from rebar.llm.prompting import prompts

    return {
        "shared_prefix": prompts.shared_plan_prefix(pctx.plan_text),
        "instructions": instructions,
    }


@register_step(
    "plan_review_coach_inputs",
    input_schema="plan_review_coach_inputs_input",
    output_schema="plan_review_coach_inputs_output",
    description=(
        "Emit the {{plan}} text + the Pass-4 coach INSTRUCTIONS for the coach_notes prompt step "
        "on the LIVE path: `plan` = assemble_context(ticket_id).plan_text and `instructions` = "
        "the SAME move-registry + coachable-findings listing that the workflow coach consumes "
        "(review_coach.coach_listing) over the blocking+surviving findings. Reuses passes.coach_"
        "instructions + coach_moves.load_move_registry so the format never diverges from the "
        "live path."
    ),
)
def plan_review_coach_inputs(ctx: StepContext) -> dict[str, Any]:
    """Emit {plan, instructions, findings} feeding the workflow's Pass-4 coach prompt step.
    ``findings`` (story 8086) is the coachable union — BLOCKING first, then surviving
    advisory — so blocking findings (the ones an agent must remediate) get coaching too;
    it also drives the coach_gate branch condition (fires when EITHER bucket is non-empty)."""
    from . import coach_moves, context_assembly, generation
    from .prerequisites import current_blocks

    # Between-pass cancel probe (story 2c89): after the deterministic Pass-3 decide,
    # before the billable Pass-4 coach. OWN-material only — see generation.probe_cancel.
    generation.probe_cancel("post-decide")
    tid = _decide_ops._ticket_id(ctx)
    pctx = context_assembly.assemble_context(tid, repo_root=ctx.repo_root)
    surviving = list(ctx.inputs.get("surviving") or [])
    blocking = list(ctx.inputs.get("blocking") or [])
    reclassified = [
        finding
        for finding in (ctx.inputs.get("indeterminate") or [])
        if finding.get("reason") == "prerequisite-coverage-indeterminate"
    ]
    coachable = blocking + surviving + reclassified
    prerequisite_ids = {
        str(record.get("prerequisite_id", ""))
        for record in (ctx.inputs.get("prerequisite_coverage") or [])
        if record.get("prerequisite_id")
    }
    prerequisite_plan_texts = {
        str(block.get("rendered_text", ""))
        for block in current_blocks()
        if block.get("rendered_text")
    }

    def _coach_safe(finding: dict[str, Any]) -> dict[str, Any]:
        if not finding.get("prerequisite_id"):
            return finding
        safe = {
            key: value
            for key, value in finding.items()
            if key not in {"prerequisite_id", "evidence", "scenarios", "location"}
        }
        for key in ("finding", "checklist_item", "suggested_fix", "impact"):
            if isinstance(safe.get(key), str):
                for prerequisite_id in prerequisite_ids:
                    safe[key] = safe[key].replace(prerequisite_id, "[direct prerequisite]")
                for prerequisite_plan_text in prerequisite_plan_texts:
                    safe[key] = safe[key].replace(
                        prerequisite_plan_text, "[direct prerequisite plan]"
                    )
        return safe

    prompt_coachable = [_coach_safe(finding) for finding in coachable]
    # The deterministic applicability filter (WS3): the LLM only sees the moves that apply
    # given the active triggers (plan-review's = the criteria the coachable findings carry).
    # Existing plan-review moves declare no `applies_when` ⇒ always-applicable ⇒ the listing is
    # unchanged; the field + filter are the mechanism a future gate (b744) uses.
    moves = coach_moves.load_move_registry(ctx.repo_root)
    triggers = {c for f in prompt_coachable for c in f.get("criteria", []) or []}
    applicable = review_coach.applicable_moves(moves, triggers)
    instructions = review_coach.coach_listing(prompt_coachable, applicable)
    return {
        "plan": pctx.plan_text,
        "instructions": instructions,
        "findings": prompt_coachable,
        "prerequisite_coverage": list(ctx.inputs.get("prerequisite_coverage") or []),
    }


@register_step(
    "plan_review_coach",
    input_schema="plan_review_coach_input",
    output_schema=_OUTPUT_SCHEMA,
    description=(
        "Pass-4 + verdict assembly: render the coach prompt's raw move picks into deterministic "
        "affirmative coaching (locked move templates; the LLM never authors prose), then assemble "
        "the terminal plan_review_verdict (verdict + findings + coaching + coverage) via shared "
        "finalize_verdict. NO signing (B5). Reuses review_coach.render_coach_notes + "
        "finalize_verdict."
    ),
)
def plan_review_coach(ctx: StepContext) -> dict[str, Any]:
    """Render coaching from the coach step's raw notes + assemble the plan_review_verdict."""
    from rebar.llm import findings as _findings
    from rebar.llm.config import resolve_gate_config

    from . import coach_moves, orchestrator
    from .det_floor import PlanContext

    # The caller-resolved run config (veiny-trout-brink): so the verdict's model/runner FIELDS
    # reflect an explicit caller config, not the env — the divergence this ticket removes.
    cfg = resolve_gate_config(ctx.repo_root)
    parts = {
        "blocking": list(ctx.inputs.get("blocking") or []),
        "surfaced": list(ctx.inputs.get("surfaced") or []),
        "overflow": list(ctx.inputs.get("overflow") or []),
        "indeterminate": list(ctx.inputs.get("indeterminate") or []),
        "dropped": list(ctx.inputs.get("dropped") or []),
    }
    # Render over the SAME applicable subset the coach prompt picked among (WS3): a move_id
    # outside the applicable set is dropped, so the LLM can never select outside it. Triggers =
    # the criteria the coachable (blocking + surfaced) findings carry (matching coach_inputs,
    # story 8086). The decision map stamps each note with its finding's decision.
    moves = coach_moves.load_move_registry(ctx.repo_root)
    surviving = list(ctx.inputs.get("surfaced") or [])
    blocking_in = list(ctx.inputs.get("blocking") or [])
    coachable = blocking_in + surviving
    triggers = {c for f in coachable for c in f.get("criteria", []) or []}
    applicable = review_coach.applicable_moves(moves, triggers)
    decision_map = {str(f.get("id")): "block" for f in blocking_in} | {
        str(f.get("id")): "advisory" for f in surviving
    }
    coaching = review_coach.render_coach_notes(
        list(ctx.inputs.get("notes") or []), applicable, decision_map=decision_map
    )

    # finalize_verdict needs only ctx.ticket_id + ctx.ticket_type — a minimal context (no
    # rebar read) suffices here (the precheck already canonicalized the id/type).
    pctx = PlanContext(
        ticket_id=str(ctx.inputs.get("canonical_id") or _decide_ops._ticket_id(ctx)),
        ticket_type=str(ctx.inputs.get("ticket_type") or ""),
        title="",
        description="",
    )
    coverage = {
        "det": ctx.inputs.get("det_coverage") or {},
        "routing": ctx.inputs.get("routing") or {},
        "llm_ran": True,
        "hierarchy_incomplete": ctx.inputs.get("hierarchy_incomplete", False),
        "hierarchy_incomplete_detail": ctx.inputs.get("hierarchy_incomplete_detail", []),
        "outcome_counts": ctx.inputs.get("outcome_counts")
        or {"clean": 0, "recovered": 0, "empty_outcomes": 0, "unrecoverable": 0},
        "prerequisite_indeterminate": any(
            record.get("disposition") == "indeterminate"
            for record in (ctx.inputs.get("prerequisite_coverage") or [])
        ),
    }
    # Surface any Pass-2 verification contract violations recorded by `plan_review_decide` this
    # run (expand-contract observability). Present ONLY when non-empty, so a clean run's verdict
    # coverage is byte-identical to before (attestation-safe); never changes the verdict string.
    violations = review_kernel.drain_contract_violations()
    if violations:
        coverage["verification_contract_violations"] = violations
    # Same posture for LLM step calls that failed but did not fail this run (the overlap judge
    # abstaining because every batch died, a novelty sub-call degrading to un-floored, ...):
    # present ONLY when non-empty, so a clean run's coverage is byte-identical and the verdict
    # string is untouched. Reaching this line at all means the run survived them.
    step_failures = step_failure_sink.drain()
    if step_failures:
        coverage["llm_step_failures"] = step_failures
    # Carry the verify step's runner-stamped record (343b); never recomputed (it could diverge).
    verdict = orchestrator.finalize_verdict(
        pctx,
        parts,
        coaching=coaching,
        coverage=coverage,
        runner_name=cfg.runner,
        model=cfg.model,
        provider_provenance=ctx.inputs.get("provider_provenance"),
    )
    # R6 (epic 6982): deterministic advisory triage — bucket the surviving advisories into
    # apply-now/defer from recorded fields (no LLM), attached as a top-level verdict key. The
    # `plan_review_verdict` schema allows additional properties, so no schema change is needed.
    verdict["triage"] = coach_moves.triage_advisories(surviving)
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
