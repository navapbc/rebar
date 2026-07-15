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

The branch (not a bare `if:`) models the child-closure SHORT-CIRCUIT
(`completion.child_closure_findings` surfaces a blocking/unclosed child, which
`completion.deterministic_child_failure` turns into a FAIL verdict that skips the LLM): a branch arm
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
    """Run the child-closure/certification gate. An UNCLOSED direct child short-circuits to a
    deterministic FAIL verdict (no LLM call — closure BLOCKED). A closed-but-UNCERTIFIED
    (force-closed) direct child does NOT block: it emits ``certifiable=False`` and the LLM still
    runs on the parent's OWN criteria (the parent may close but not certify)."""
    from rebar import _reads
    from rebar.llm.completion import child_closure_findings, deterministic_child_failure
    from rebar.llm.config import resolve_gate_config

    tid = ctx.inputs.get("ticket_id") or ctx.target_ticket
    if not tid:
        raise ValueError(
            f"step {ctx.step_id!r} needs a ticket: pass `with: {{ticket_id: ...}}` or run "
            f"the workflow against a target ticket"
        )
    root = _reads.show_ticket(str(tid), repo_root=ctx.repo_root)
    canonical = root.get("ticket_id", str(tid))
    blocking, uncertified = child_closure_findings(canonical, ctx.repo_root)
    if blocking:
        # A direct child is NOT closed → the parent is incomplete: fail fast, NO LLM call, BLOCK.
        cfg = resolve_gate_config(ctx.repo_root)  # caller-resolved run config (veiny-trout-brink)
        verdict = deterministic_child_failure(canonical, blocking, cfg)
        return {
            "run_verify": False,
            "precheck_failed": True,
            "canonical_id": canonical,
            "verdict": verdict,
            "context": "",  # short-circuit: no verify runs, so no context is needed
            "certifiable": False,
        }
    # No unclosed child → run the LLM on the parent's OWN criteria. Certification is WITHHELD iff a
    # direct child is closed-but-UNCERTIFIED (force-closed): the parent MAY close (subject to its
    # own criteria) but cannot be certified — certification propagates, so an unattested descendant
    # withholds the parent's signature. This is a close-vs-certify distinction, NOT a block.
    certifiable = not uncertified
    # Assemble the verifier's fenced ticket context (the prompt-injection delimiter). HONOR the
    # caller's `graph`: the close gate (_commands.transition) passes graph=False so an epic close
    # verifies its OWN completion criteria, not its whole descendant subtree — children are trusted
    # via the deterministic child-closure gate above (their certified signatures), not re-verified.
    # `graph` is threaded from the caller (default False for a direct workflow invocation); the
    # epic-includes-descendants default for a standalone `verify-completion` deep review is resolved
    # UPSTREAM in verify_completion, not re-derived here. Re-deriving graph here (the old bug)
    # overrode the close gate's graph=False and made an epic close re-verify every descendant,
    # blowing the step budget.
    from rebar.llm import operations

    graph = bool(ctx.inputs.get("graph"))
    context, _ids = operations.assemble_context(str(tid), graph=graph, repo_root=ctx.repo_root)
    fenced = f"<untrusted_ticket_context>\n{context}\n</untrusted_ticket_context>"
    return {
        "run_verify": True,
        "precheck_failed": False,
        "canonical_id": canonical,
        "verdict": None,
        "context": fenced,
        "certifiable": certifiable,
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
    from rebar.llm.completion import reconcile_verdict
    from rebar.llm.config import resolve_gate_config

    # The caller-resolved run config (veiny-trout-brink); this op uses cfg.repo_path for citation
    # resolution — the SAME resolved config the rest of the run uses, not a per-op from_env.
    cfg = resolve_gate_config(ctx.repo_root)
    ticket_id = str(ctx.inputs["ticket_id"])
    result: dict[str, Any] = {
        "verdict": ctx.inputs.get("raw_verdict", ""),
        "findings": list(ctx.inputs.get("raw_findings") or []),
        "target": {"kind": "ticket", "ticket_ids": [ticket_id]},
        "reviewers": [_REVIEWER_ID],
        "runner": ctx.inputs.get("runner"),
        "model": ctx.inputs.get("model"),
        "trace_id": ctx.inputs.get("trace_id"),
    }
    # Mirror the structured runner's exclude_none: only carry `summary` when present (the
    # completion_verdict schema's `summary` is a string, never null). An absent summary is the
    # common case (the verifier usually omits it); a None here would fail validation.
    summary = ctx.inputs.get("summary")
    if summary is not None:
        result["summary"] = summary
    # Same normalize → resolve_citations → reconcile → validate pipeline as
    # completion.verify_completion's tail (the normalize_finding/resolve_citations/reconcile_verdict
    # sequence), so the workflow path is behaviourally equivalent to the bespoke call.
    result["findings"] = [
        findings.normalize_finding(f, reviewer_id=_REVIEWER_ID) for f in result["findings"]
    ]
    # Carry the POSITIVE per-criterion records through the workflow (close-gate) path. This is
    # the lossless PASS capture that rides ALONGSIDE the failures-only `findings`; it is
    # untouched by reconcile_verdict (which only edits verdict/findings/remediation) and passes
    # validate_structured (an optional array). Empty on the legacy path (agent omitted criteria).
    result["criteria"] = list(ctx.inputs.get("raw_criteria") or [])
    findings.resolve_citations(result, cfg.repo_path)
    reconcile_verdict(result)
    # Carry the precheck's certification decision onto the verdict. `certifiable=False` (a
    # closed-but-uncertified descendant) does NOT change the PASS/FAIL verdict — the parent's own
    # criteria stand — but the close gate reads it to close WITHOUT signing (certification
    # propagates). Defaults True (no uncertified descendant, or a direct workflow invocation).
    result["certifiable"] = bool(ctx.inputs.get("certifiable", True))
    if not result["certifiable"] and "summary" not in result:
        result["summary"] = (
            "Closed without certification: a force-closed (uncertified) descendant leaves the "
            "subtree unattested; re-close it through the gate to certify."
        )
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
