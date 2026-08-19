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

import logging
from typing import Any

from .executor import StepContext, register_step

logger = logging.getLogger(__name__)

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
    runs on the parent's OWN criteria (the parent may close but not certify).

    EPIC closes additionally run the epic-close bug screen (ticket 4b54): the deterministic
    ``caused_by`` floor short-circuits exactly like an unclosed child (an open/in_progress bug
    the epic's own work broke is delegated work unfinished), and the candidate screen appends
    its compact A-verdict block INSIDE the fenced context below — the screen degrades open on
    non-systemic failures, but a provider error (``LLMUnavailableError``) propagates so the
    close FAILS CLOSED (bug 1019)."""
    from rebar import _reads
    from rebar.llm.completion import (
        child_closure_findings,
        deterministic_child_failure,
    )
    from rebar.llm.config import resolve_gate_config
    from rebar.llm.epic_bug_screen import epic_bug_floor_findings

    tid = ctx.inputs.get("ticket_id") or ctx.target_ticket
    if not tid:
        raise ValueError(
            f"step {ctx.step_id!r} needs a ticket: pass `with: {{ticket_id: ...}}` or run "
            f"the workflow against a target ticket"
        )
    root = _reads.show_ticket(str(tid), repo_root=ctx.repo_root)
    canonical = root.get("ticket_id", str(tid))
    is_epic = root.get("ticket_type") == "epic"
    from rebar import config as _config

    description_limit = _config.compose_config(ctx.repo_root).verify.max_ticket_description_chars
    description_chars = len(root.get("description") or "")
    if description_chars > description_limit:
        cfg = resolve_gate_config(ctx.repo_root)
        finding = {
            "criterion": f"ticket description is at most {description_limit:,} characters",
            "severity": "high",
            "dimension": "completion",
            "detail": (
                f"the authoritative ticket description is {description_chars:,} characters, "
                f"above the {description_limit:,}-character completion admission limit; reduce "
                "it before verification, usually by splitting independent work into coherent "
                "child tickets"
            ),
            "citations": [
                {
                    "kind": "source",
                    "description": f"authoritative ticket {canonical} description length",
                }
            ],
        }
        oversize_summary = (
            f"Ticket description is {description_chars:,} characters; completion verification "
            f"accepts at most {description_limit:,}. Reduce the description before retrying."
        )
        verdict = deterministic_child_failure(canonical, [finding], cfg, summary=oversize_summary)
        return {
            "run_verify": False,
            "precheck_failed": True,
            "canonical_id": canonical,
            "verdict": verdict,
            "context": "",
            "certifiable": False,
        }
    blocking, uncertified = child_closure_findings(canonical, ctx.repo_root)
    floor: list[dict] = []
    if is_epic and not blocking:
        # The DET caused_by floor (4b54): only reached when the direct-children gate passes,
        # so the two deterministic tiers never mix in one verdict's messaging. A read error
        # mirrors the child-closure arm: never block (a glitch shouldn't stop a legitimate
        # close) and never crash the workflow, but WITHHOLD certification — an unread bug set
        # must not be laundered into a signed "no caused_by bugs" attestation.
        try:
            floor = epic_bug_floor_findings(canonical, ctx.repo_root)
        except Exception as exc:
            logger.warning(
                "epic-close caused_by floor read failed for %s; withholding certification",
                canonical,
                exc_info=True,
            )
            uncertified = [
                *list(uncertified),
                {
                    "criterion": f"open caused_by bugs against {canonical} could not be read",
                    "severity": "high",
                    "dimension": "completion",
                    "detail": f"could not enumerate open bugs to compute the caused_by floor for "
                    f"{canonical} ({exc}); the close may proceed on the epic's own "
                    "criteria but UNSIGNED. Re-close once the store read succeeds.",
                    "citations": [
                        {"kind": "source", "description": f"epic_bug_floor_findings: {exc}"}
                    ],
                },
            ]
    if blocking or floor:
        # A direct child is NOT closed (or an open caused_by bug indicts the epic's own work)
        # → the parent is incomplete: fail fast, NO LLM call, BLOCK.
        cfg = resolve_gate_config(ctx.repo_root)  # caller-resolved run config (veiny-trout-brink)
        summary = None
        if floor:
            summary = (
                f"{len(floor)} open/in_progress bug(s) record caused_by against this epic's "
                "subtree — the epic's own work broke them. Fix (close) each bug, re-parent it "
                "under the epic, or dispute the caused_by link, then re-close."
            )
        verdict = deterministic_child_failure(canonical, blocking + floor, cfg, summary=summary)
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
    from rebar.llm.workflow.completion_prefetch import PrefetchSpec, fit_within_ceiling

    graph = bool(ctx.inputs.get("graph"))
    # Story a9dd: pre-load the ticket's declared file_impact contents + referencing-commit
    # diffs into a bounded <prefetched_file_contents> section so the verifier need not
    # re-discover its declared files agentically. Assemble the BASE (prefetch=None) and the
    # prefetch section SEPARATELY so the ceiling accounting is explicit at the gate: trim the
    # section to the resolved model's physical context ceiling before concatenating (an
    # oversize prefetch is TRIMMED, never allowed to overflow — see completion_prefetch and
    # completion_criteria.physical_context_ceiling).
    from rebar.llm.workflow import completion_prefetch

    base, _ids = operations.assemble_context(str(tid), graph=graph, repo_root=ctx.repo_root)
    # Prefetch is ON by default; REBAR_VERIFY_PREFETCH=0 disables it. This is the escape hatch
    # the story-a9dd live A/B uses to capture a prefetch-DISABLED baseline on the SAME binary,
    # ticket, and tool surface (config otherwise identical) before the prefetch run.
    import os

    if os.environ.get("REBAR_VERIFY_PREFETCH") == "0":  # read-via: subsystem-kill-switch
        context = base
    else:
        spec = PrefetchSpec(ticket_id=str(tid), graph=graph)
        section, _manifest = completion_prefetch.assemble_prefetch(spec, repo_root=ctx.repo_root)
        model = resolve_gate_config(ctx.repo_root).model
        fitted = fit_within_ceiling(base, section, model)
        context = base + ("\n\n" + fitted if fitted else "")
    if is_epic:
        # Epic-close bug screen (4b54), stages 2-3: filter + haiku screen; A-verdicts land as
        # a compact evidence block INSIDE the fence (untrusted, like all ticket content). The
        # screen degrades open on NON-SYSTEMIC failures — an empty block costs the verifier
        # nothing — but a provider error (LLMUnavailableError) propagates: the interpreter
        # fails this step and the close gate fail-closes (bug 1019, operator-ratified).
        from rebar.llm import epic_bug_screen

        screen = epic_bug_screen.run_screen(
            canonical, root, ctx.repo_root, cfg=resolve_gate_config(ctx.repo_root)
        )
        if screen["block"]:
            context = f"{context}\n\n{screen['block']}"
    # Ticket 6ec8: surface the deterministic child-closure/certification proof (already computed
    # above via child_closure_findings) as EVIDENCE inside the fence — counts + the ids of any
    # closed-but-uncertified children — so an "every child is closed/certified" criterion resolves
    # WITHOUT a tool call. Emits "" (no block) for a childless ticket. `blocking` is necessarily
    # empty here (a non-empty `blocking` short-circuited earlier), so only certification is
    # reported; the Gerrit `Verified +1` half is flagged out-of-reach in the block and governed by
    # the trusted verifier prompt.
    from rebar.llm.completion import build_child_closure_evidence

    child_evidence = build_child_closure_evidence(canonical, ctx.repo_root, uncertified)
    if child_evidence:
        context = f"{context}\n\n{child_evidence}"
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
    from rebar.llm import step_failures as step_failure_sink
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
    # Provider provenance (343b): wired from the verify agent step, which carries the runner's
    # OWN record for the call that ran — carried, never recomputed here (a second resolution can
    # diverge from the endpoint/caps that served the run). Set only when non-None, like `summary`
    # below: a None means the runner resolved no provider, and an absent key is the honest
    # record of that — it also keeps this op byte-identical to completion.py's bespoke tail.
    provenance = ctx.inputs.get("provider_provenance")
    if provenance is not None:
        result["provider_provenance"] = provenance
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
    # The verify step's OWN certifiable (2948) can independently withhold certification when a
    # banked deterministic fallback assembled the verdict without a model call — combine (AND) so
    # either source withholds. `verify_certifiable` defaults True (absent / normal LLM verdict).
    verify_certifiable = ctx.inputs.get("verify_certifiable")
    verify_certifiable = True if verify_certifiable is None else bool(verify_certifiable)
    result["certifiable"] = bool(ctx.inputs.get("certifiable", True)) and verify_certifiable
    # The verify step's self-verdict provenance: how the verdict was produced. The default
    # "primary" (a normal successful verify) is DROPPED so the reconcile stays byte-identical to
    # completion.py's tail on the common path; only a fallback marker (llm_finalizer /
    # deterministic_fallback) is carried onto the verdict so the sidecar/signing path can see it.
    finalizer = ctx.inputs.get("finalizer")
    if finalizer is not None and finalizer != "primary":
        result["finalizer"] = finalizer
    if not result["certifiable"] and "summary" not in result:
        result["summary"] = (
            "Closed without certification: a force-closed (uncertified) descendant leaves the "
            "subtree unattested; re-close it through the gate to certify."
        )
    # LLM step calls that FAILED but did not fail this run (a completion sub-call degrading to
    # "score nothing", ...). Placement mirrors review-plan and review-code so ONE JSON path,
    # `coverage.llm_step_failures`, works across all three gates. `coverage` itself is created
    # ONLY when the tally is non-empty: this verdict is SIGNED, so a clean run must stay
    # byte-identical rather than gain an empty container. Never changes the verdict string.
    step_failures = step_failure_sink.drain()
    if step_failures:
        result["coverage"] = {"llm_step_failures": step_failures}
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
