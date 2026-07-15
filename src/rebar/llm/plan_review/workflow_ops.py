"""Scripted ``uses`` ops that express the plan-review gate AS a v3 engine workflow (epic B,
story B2).

These are THIN adapters over the shared, already-tested plan-review units in
:mod:`rebar.llm.plan_review` — each op delegates to those units
(:mod:`.det_floor`, :mod:`.registry`, :func:`.orchestrator.route_criteria` /
``partition_findings`` / ``pass3_over_findings`` / ``finalize_verdict``,
:func:`.passes.render_coach_notes`) rather than re-implementing the gate. This workflow
is now the SOLE plan-review gate (the bespoke ``orchestrator.run_review`` driver it once
mirrored was retired in story B-RETIRE). The workflow shape (mirrors the B3 completion
gate):

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

import logging
import re
from typing import Any

from rebar.llm.workflow.executor import StepContext, register_step

logger = logging.getLogger(__name__)

_OUTPUT_SCHEMA = "plan_review_verdict"


def _ticket_id(ctx: StepContext) -> str:
    tid = ctx.inputs.get("ticket_id") or ctx.target_ticket
    if not tid:
        raise ValueError(
            f"step {ctx.step_id!r} needs a ticket: pass `with: {{ticket_id: ...}}` or run "
            f"the workflow against a target ticket"
        )
    return str(tid)


# ── a8e5 Component 3: operator-attested AC awareness (pure DET, ADR-0043) ─────────────────────
# An AC tagged `- [ ] [operator-attested] …` has "done" evidence that inherently lives OUTSIDE
# the codebase (a deploy, a live drill), so a plan-review finding that flags it as
# in-session-UNVERIFIABLE (the ac_unverifiable hard-override axis) is a FALSE POSITIVE: the
# in-session unverifiability is by design. We DET-detect the tag and, for a finding that
# references such an AC, clear ac_unverifiable BEFORE impact_plan reads it — leaving the kernel
# impact_plan/pass3 math byte-unchanged (the fact is injected upstream, not taught to the kernel).
_OPERATOR_ATTESTED_AC_RE = re.compile(
    r"^\s*-\s*\[[ xX]?\]\s*\[operator-attested\]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def operator_attested_ac_texts(description: str) -> list[str]:
    """Extract the criterion text of every AC checklist line tagged with the EXACT
    case-insensitive token ``[operator-attested]`` (ADR-0043). The tag is stripped and the text
    trimmed. Matching is exact on the hyphenated token — a near-miss like ``[operator_attested]``
    is NOT operator-attested. Returns ``[]`` when none are tagged."""
    return [m.strip() for m in _OPERATOR_ATTESTED_AC_RE.findall(description or "")]


def _norm(s: str) -> str:
    """Whitespace/case-normalize for substring matching."""
    return " ".join((s or "").lower().split())


def enrich_operator_attested(
    findings: list[dict[str, Any]], verifs: dict[int, dict[str, Any]], description: str
) -> None:
    """DET-enrich verifications in place (mirrors code_review ``_det_enrich_verifications``): for a
    finding that REFERENCES an operator-attested AC, inject ``operator_attested=True`` into its
    ``severity_attributes`` and CLEAR the ``ac_unverifiable`` axis to ``"none"`` (an
    operator-attested AC's in-session unverifiability is by design, not a defect). A finding
    references an operator-attested AC iff a non-empty normalized operator-attested criterion text
    is a substring of the finding's combined normalized text (location + finding + checklist_item +
    evidence). Fail-safe: never raises on missing keys / bad shapes; a miss leaves the finding
    untouched (the conservative direction — a surviving advisory, never a spurious clear)."""
    oa_texts = [_norm(t) for t in operator_attested_ac_texts(description)]
    oa_texts = [t for t in oa_texts if t]
    if not oa_texts:
        return
    for i, f in enumerate(findings):
        verif = verifs.get(i)
        if not isinstance(verif, dict):
            continue
        attrs = verif.get("severity_attributes")
        if not isinstance(attrs, dict):
            attrs = {}
            verif["severity_attributes"] = attrs
        combined = _norm(
            " ".join(
                [
                    str(f.get("location", "")),
                    str(f.get("finding", "")),
                    str(f.get("checklist_item", "")),
                    " ".join(str(e) for e in (f.get("evidence") or [])),
                ]
            )
        )
        if any(oa in combined for oa in oa_texts):
            attrs["operator_attested"] = True
            # A recorded attestation IS the oracle, so it clears missing/underspecified —
            # but never broken_oracle: a factually wrong stated command is not cured by
            # attesting the outcome (story large-sleepful-needlefish).
            if attrs.get("ac_unverifiable") in ("missing_oracle", "underspecified_oracle"):
                attrs["ac_unverifiable"] = "none"


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
    base: dict[str, Any] = {
        "canonical_id": pctx.ticket_id,
        "ticket_type": pctx.ticket_type,
        "det_blocking": [],
        "det_advisory": [],
        "det_coverage": {},
    }

    # Exempt types short-circuit to a PASS (no review runs) — mirrors run_review.
    if pctx.ticket_type in ("bug", "session_log", "code_review", "identity"):
        reason = (
            "bug tickets are exempt from the plan-review gate"
            if pctx.ticket_type == "bug"
            else f"{pctx.ticket_type} tickets are gate-exempt"
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
        from rebar.llm.config import resolve_gate_config

        cfg = resolve_gate_config(ctx.repo_root)  # caller-resolved cfg (veiny-trout-brink)
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
    # The EFFECTIVE vocabulary = canonical built-ins ∪ activated PROJECT criteria (from the
    # `.rebar/criteria_routing.json` overlay), resolved against the SAME root route_criteria
    # loaded (pctx.repo_root) so the vocab and the loaded criteria never diverge. ISF is fed
    # the linked session log by the finder itself (never a rubric chunk), so it is never a
    # batch criterion — excluded from the inclusion vocabulary here.
    # exec:DET criteria run in the deterministic phase (det_floor), NOT the LLM batch — so they
    # own NO `include_<ID>` batch slot. Exclude them (and ISF, fed the session log directly) from
    # the inclusion vocabulary, reading `exec` from the effective routing. Story 7f0d.
    _routing = registry.effective_routing(pctx.repo_root)

    def _is_det(cid: str) -> bool:
        return str((_routing.get(cid) or {}).get("exec", "")).upper() == "DET"

    effective = [
        cid
        for cid in registry.effective_criteria(pctx.repo_root)
        if cid != "ISF" and not _is_det(cid)
    ]

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
        "Emit the {{plan}} text + the Pass-2 verifier INSTRUCTIONS for the verify prompt step. "
        "`plan` = assemble_context(ticket_id).plan_text. `instructions` is a LIST of per-chunk "
        "listings (passes.verify_instructions, global indices preserved): ONE element in the "
        "common case (the whole request fits the verifier model window) — byte-identical to a "
        "single aggregate verify — and TOKEN-BUDGETED splits (sizing.verify_request_chunks, no "
        "magic count) only when the request would exceed the window. The verify prompt step runs "
        "once per element and merges the verifications by index; a finding too big to verify at "
        "the largest model is omitted → pass3 routes it to INDETERMINATE."
    ),
)
def plan_review_verify_inputs(ctx: StepContext) -> dict[str, Any]:
    """Emit {plan, instructions[]} feeding the workflow's Pass-2 verify prompt step. The
    `instructions` list has ONE element for the common (fits-the-window) case and is split into
    token-budgeted chunks (global indices preserved) when the request would exceed the verifier
    model's window — encapsulated chunking, not a workflow fan-out (epic solid-timer-unison WS3)."""
    from rebar import config as _config
    from rebar.llm.config import resolve_gate_config

    from . import _verifier_cfg, orchestrator, passes, sizing

    tid = _ticket_id(ctx)
    pctx = orchestrator.assemble_context(tid, repo_root=ctx.repo_root)
    findings = list(ctx.inputs.get("findings") or [])
    # Size against the RESOLVED verifier model (the Sonnet downgrade, operator override honored)
    # — the same model the verify prompt step runs under (gate_dispatch passes _verifier_cfg(cfg)).
    # resolve_gate_config returns the caller-resolved run config, not a per-op from_env
    # (veiny-trout-brink), so an explicit caller model sizes the verify request correctly.
    verify_model = _verifier_cfg(resolve_gate_config(ctx.repo_root)).model
    try:
        headroom = float(_config.load_config(ctx.repo_root).verify.verify_window_headroom)
    except Exception:  # noqa: BLE001 — config unreadable → the documented default
        headroom = sizing.DEFAULT_VERIFY_WINDOW_HEADROOM
    chunks, _omitted = sizing.verify_request_chunks(findings, model=verify_model, headroom=headroom)
    # `_omitted` indices are intentionally left out of every chunk → no verification for them →
    # pass3_decide(None) marks them INDETERMINATE (never silently dropped). When there are no
    # findings (or all were omitted), still emit ONE (empty) chunk so the verify step makes its
    # single aggregate call returning an empty `verifications` list — the prior behavior the
    # decide step depends on.
    instructions = [passes.verify_instructions(chunk) for chunk in (chunks or [[]])]
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
    """Emit {plan, instructions, findings} feeding the workflow's Pass-4 coach prompt step.
    ``findings`` (story 8086) is the coachable union — BLOCKING first, then surviving
    advisory — so blocking findings (the ones an agent must remediate) get coaching too;
    it also drives the coach_gate branch condition (fires when EITHER bucket is non-empty)."""
    from . import orchestrator, passes

    tid = _ticket_id(ctx)
    pctx = orchestrator.assemble_context(tid, repo_root=ctx.repo_root)
    surviving = list(ctx.inputs.get("surviving") or [])
    blocking = list(ctx.inputs.get("blocking") or [])
    coachable = blocking + surviving
    # The deterministic applicability filter (WS3): the LLM only sees the moves that apply
    # given the active triggers (plan-review's = the criteria the coachable findings carry).
    # Existing plan-review moves declare no `applies_when` ⇒ always-applicable ⇒ the listing is
    # unchanged; the field + filter are the mechanism a future gate (b744) uses.
    moves = passes.load_move_registry(ctx.repo_root)
    triggers = {c for f in coachable for c in f.get("criteria", []) or []}
    applicable = passes.applicable_moves(moves, triggers)
    instructions = passes.coach_instructions(coachable, applicable)
    return {"plan": pctx.plan_text, "instructions": instructions, "findings": coachable}


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
    from rebar.llm import review_kernel

    from . import orchestrator

    findings = list(ctx.inputs.get("findings") or [])
    raw_verifs = list(ctx.inputs.get("verifications") or [])
    det_blocks = list(ctx.inputs.get("det_blocking") or [])
    det_advisories = list(ctx.inputs.get("det_advisory") or [])

    # The Pass-2 verifier (the workflow's `verify` prompt step) emits a flat list of
    # `{index, severity_attributes, binary}`; reshape it to the `{index: {...}}` map Pass-3
    # consumes via the SHARED structural reshape seam (review_kernel.reshape_verifications) — the
    # SINGLE place the verifier→decide keying contract lives, so this op no longer re-implements
    # the silent-drop. `valid_indices` is the batch index domain the verifier ran over. The map is
    # byte-identical to the prior inline reshape; what is NEW is the contract-violation REPORT
    # (malformed / duplicate / out-of-range indices) — surfaced loudly per the expand-contract
    # posture (ERROR log + a run-scoped record drained into verdict coverage) with NO change to
    # the decisions/verdict (a finding with no verification still degrades to INDETERMINATE).
    reshape = review_kernel.reshape_verifications(raw_verifs, valid_indices=range(len(findings)))
    verifs = reshape.verifications
    if reshape.has_violations:
        logger.error(
            "plan-review Pass-2 verification contract violation (findings degrade to "
            "INDETERMINATE; verdict unchanged): %s",
            reshape.summary(),
        )
        orchestrator.record_contract_violation(reshape.summary())

    # a8e5 Component 3: operator-attested AC awareness. Clear ac_unverifiable on a finding that
    # flags an operator-attested AC as in-session-unverifiable BEFORE Pass-3 reads it (fail-open:
    # any read failure skips enrichment, never breaks the decide step).
    try:
        _desc = orchestrator.assemble_context(_ticket_id(ctx), repo_root=ctx.repo_root).description
        enrich_operator_attested(findings, verifs, _desc)
    except Exception:  # noqa: BLE001 — best-effort enrichment; never fail the gate on it
        logger.debug("operator-attested enrichment skipped", exc_info=True)

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
        verif = verifs.get(i)
        if verif is not None:  # absent == None to the consumer's verifs.get(i)
            rest_verifs[len(rest)] = verif
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
    from rebar.llm.config import resolve_gate_config

    from . import orchestrator, passes
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
    moves = passes.load_move_registry(ctx.repo_root)
    surviving = list(ctx.inputs.get("surfaced") or [])
    blocking_in = list(ctx.inputs.get("blocking") or [])
    coachable = blocking_in + surviving
    triggers = {c for f in coachable for c in f.get("criteria", []) or []}
    applicable = passes.applicable_moves(moves, triggers)
    decision_map = {str(f.get("id")): "block" for f in blocking_in} | {
        str(f.get("id")): "advisory" for f in surviving
    }
    coaching = passes.render_coach_notes(
        list(ctx.inputs.get("notes") or []), applicable, decision_map=decision_map
    )

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
    # Surface any Pass-2 verification contract violations recorded by `plan_review_decide` this
    # run (expand-contract observability). Present ONLY when non-empty, so a clean run's verdict
    # coverage is byte-identical to before (attestation-safe); never changes the verdict string.
    violations = orchestrator.drain_contract_violations()
    if violations:
        coverage["verification_contract_violations"] = violations
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
