"""Scripted `uses` ops that wrap the LLM-gate deterministic helpers (epic B).

These let the completion-verification gate be expressed AS an engine workflow without
re-implementing its (already-correct, already-tested) deterministic logic: each op is a
thin adapter over a `rebar.llm.completion` helper, so the workflow path stays
behaviourally equivalent to the bespoke `verify_completion` call (parity is structural,
not coincidental). The workflow shape is:

    completion_precheck (uses)
      └─ branch on `run_verify`:
           then: <prompt: completion-verifier> → completion_reconcile (uses)
           else: completion_passthrough (uses)   # the deterministic FAIL verdict

The branch (not a bare `if:`) models `completion.py`'s child-closure SHORT-CIRCUIT
(completion.py:223-225 returns a deterministic FAIL and skips the LLM): a branch arm
references only steps that run in it, whereas referencing an `if:`-skipped step's outputs
raises. So a failing precheck reaches the ELSE arm and NEVER runs the (billable) prompt —
behaviour and cost preserved. Signing is NOT done here (completion.py has no signer; the
close-gate signs) — that is the B5 cutover's concern.
"""

from __future__ import annotations

from typing import Any

from .executor import StepContext, register_step

_REVIEWER_ID = "completion-verifier"
_OUTPUT_SCHEMA = "completion_verdict"


@register_step(
    "completion_precheck",
    input_schema="completion_precheck_input",
    output_schema="completion_precheck_output",
    description=(
        "The deterministic child-closure precheck of the completion gate: a parent is "
        "incomplete unless every direct child is closed with a certified signature. "
        "Emits `run_verify` (true → the agentic verify should run) and, on failure, the "
        "deterministic FAIL completion_verdict (so the LLM is never called — the gate's "
        "short-circuit). Wraps rebar.llm.completion without changing its logic."
    ),
)
def completion_precheck(ctx: StepContext) -> dict[str, Any]:
    """Run the child-closure gate; short-circuit to a deterministic FAIL verdict if it trips."""
    import rebar
    from rebar.llm.completion import _child_closure_findings, _deterministic_child_failure
    from rebar.llm.config import LLMConfig

    tid = ctx.inputs.get("ticket_id") or ctx.target_ticket
    if not tid:
        raise ValueError(
            f"step {ctx.step_id!r} needs a ticket: pass `with: {{ticket_id: ...}}` or run "
            f"the workflow against a target ticket"
        )
    root = rebar.show_ticket(str(tid), repo_root=ctx.repo_root)
    canonical = root.get("ticket_id", str(tid))
    child_findings = _child_closure_findings(canonical, ctx.repo_root)
    if child_findings:
        cfg = LLMConfig.from_env(repo_root=ctx.repo_root)
        verdict = _deterministic_child_failure(canonical, child_findings, cfg)
        return {
            "run_verify": False,
            "precheck_failed": True,
            "canonical_id": canonical,
            "verdict": verdict,
        }
    return {
        "run_verify": True,
        "precheck_failed": False,
        "canonical_id": canonical,
        "verdict": None,
    }


@register_step(
    "completion_reconcile",
    input_schema="completion_reconcile_input",
    output_schema=_OUTPUT_SCHEMA,
    description=(
        "Normalize/reconcile the agentic verifier's raw output into a completion_verdict: "
        "normalize findings, downgrade hallucinated citations, and enforce the FAIL<->findings "
        "invariant. The deterministic guardrail half of the gate (the verdict stays the "
        "agent's); mirrors rebar.llm.completion's post-run reconciliation exactly."
    ),
)
def completion_reconcile(ctx: StepContext) -> dict[str, Any]:
    """Reconcile the agent verdict → a validated completion_verdict (parity with completion.py)."""
    from rebar.llm import findings
    from rebar.llm.completion import _reconcile
    from rebar.llm.config import LLMConfig

    cfg = LLMConfig.from_env(repo_root=ctx.repo_root)
    ticket_id = str(ctx.inputs["ticket_id"])
    result: dict[str, Any] = {
        "verdict": ctx.inputs.get("raw_verdict", ""),
        "findings": list(ctx.inputs.get("raw_findings") or []),
        "summary": ctx.inputs.get("summary"),
        "target": {"kind": "ticket", "ticket_ids": [ticket_id]},
        "reviewers": [_REVIEWER_ID],
        "runner": ctx.inputs.get("runner"),
        "model": ctx.inputs.get("model"),
        "trace_id": ctx.inputs.get("trace_id"),
    }
    # Same normalize → resolve_citations → reconcile → validate pipeline as
    # completion.verify_completion's tail (completion.py:288-295), so the workflow path is
    # behaviourally equivalent to the bespoke call.
    result["findings"] = [
        findings.normalize_finding(f, reviewer_id=_REVIEWER_ID) for f in result["findings"]
    ]
    findings.resolve_citations(result, cfg.repo_path)
    _reconcile(result)
    return findings.validate_structured(result, _OUTPUT_SCHEMA)


@register_step(
    "completion_passthrough",
    input_schema="completion_passthrough_input",
    output_schema=_OUTPUT_SCHEMA,
    description=(
        "Emit an already-reconciled deterministic completion_verdict verbatim — the branch "
        "ELSE arm taken when the child-closure precheck fails (no LLM ran). Keeps the "
        "workflow's terminal output uniform (a completion_verdict) across both arms."
    ),
)
def completion_passthrough(ctx: StepContext) -> dict[str, Any]:
    """Pass the precheck's deterministic FAIL verdict through as the terminal output."""
    verdict = ctx.inputs.get("verdict")
    if not isinstance(verdict, dict):
        raise ValueError(
            f"step {ctx.step_id!r} expects a `verdict` object from the precheck; "
            f"got {type(verdict)}"
        )
    return dict(verdict)
