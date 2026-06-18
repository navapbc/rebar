"""Completion-verification operation: verify a ticket's completion requirements are met.

``verify_completion(ticket_id)`` runs a tool-using LLM agent (the ``completion-verifier``
reviewer) that checks every completion requirement on a ticket — acceptance/success/close
criteria, definitions of done, and (for bugs) that the bug is resolved — is demonstrably
satisfied by the implementation, and returns a **``completion_verdict``** (``{verdict, findings,
…}``). The agent is read-only: line-numbered repo file tools plus a read-only rebar
``show_ticket`` tool; it never writes, transitions, signs, or closes.

Like the review ops, this owns the **deterministic** parts (assembling the ticket context from
rebar's own reads, resolving the reviewer prompt, picking the runner) and delegates the agent
run to a :class:`~rebar.llm.runner.Runner`. The structured-output **contract** is selected by
``output_schema="completion_verdict"`` (the pluggable-contract seam). The agent emits the
verdict; the operation then deterministically normalizes/reconciles it (the verdict is the
agent's, with a guardrail — see :func:`_reconcile`) and resolves citations against the repo.

Optionality: stdlib-only at import; the agent stack is lazy-imported by the runner; the
read-only ticket tool is built only when langchain is present (so the offline FakeRunner path
needs no extra).
"""

from __future__ import annotations

from dataclasses import replace

from rebar.llm import findings, operations, prompts
from rebar.llm.config import DEFAULT_MODEL, LLMConfig
from rebar.llm.runner import Runner, RunRequest, get_runner

__all__ = ["verify_completion"]

_REVIEWER_ID = "completion-verifier"
_OUTPUT_SCHEMA = "completion_verdict"
# Bounded completion verification wants a DECISIVE model, not a maximally-thorough one: the
# framework default (opus) over-explores — it rabbit-holes on confirming code is "wired",
# blowing the step budget even on a 2-criterion ticket (it tripped recursion_limit=300 / 385s
# in testing) — whereas sonnet converges in ~12s. So default the verifier to sonnet (matching
# the DSO completion-verifier's `model: sonnet`). An operator who EXPLICITLY sets
# REBAR_LLM_MODEL to a non-default still wins (below).
_VERIFIER_DEFAULT_MODEL = "claude-sonnet-4-6"
# Completion verification is inherently more tool-heavy than a single-dimension review: it
# must check potentially many criteria, each against several files. The framework review
# default (REBAR_LLM_MAX_STEPS=25 ≈ 12 tool calls) is far too low and trips the recursion cap
# mid-verification (→ a false fail-closed block at the gate). Use a generous verification FLOOR;
# an operator who explicitly sets a HIGHER REBAR_LLM_MAX_STEPS still wins. Very large tickets
# (e.g. a whole framework epic) may still need it raised further, or --force-close.
_VERIFY_MIN_STEPS = 120


def _readonly_ticket_tools(repo_path):
    """A read-only rebar ``show_ticket`` tool for the verifier, or ``None`` when langchain
    isn't installed (the offline FakeRunner path — the real runners require the ``agents``
    extra and build their own tools, so None there is fine). Never grants comment/write."""
    try:
        from rebar.llm.runner import _scoped_ticket_tools

        return _scoped_ticket_tools(repo_path, allow_comment=False)
    except (ImportError, ModuleNotFoundError):
        return None


def _reconcile(result: dict) -> None:
    """Normalize the verdict and enforce the FAIL⇔findings invariant IN PLACE.

    The agent emits the verdict; this is a deterministic guardrail, NOT a re-judge:
    * normalize ``verdict`` — upper-case; exactly ``PASS`` is PASS, anything else FAIL
      (fail-safe: a garbled verdict never silently passes);
    * ``FAIL`` with no findings → synthesize one placeholder finding (the contract is
      FAIL ⇒ ≥1 finding; this is the sloppy-model case the shape-only schema lets reach here);
    * ``PASS`` with findings → flip to ``FAIL`` (the prompt defines findings as failures-only,
      so a listed failure must block — keyed on the EXISTENCE of a failure finding, not on
      severity, so it stays consistent with "the agent emits the verdict").
    """
    raw = str(result.get("verdict", "")).strip().upper()
    verdict = "PASS" if raw == "PASS" else "FAIL"
    items = result.get("findings") or []
    if verdict == "PASS" and items:
        verdict = "FAIL"
    if verdict == "FAIL" and not items:
        items = [
            {
                "criterion": "(unspecified)",
                "severity": "high",
                "dimension": "completion",
                "detail": "verifier returned FAIL without itemizing the failing criterion.",
            }
        ]
    result["verdict"] = verdict
    result["findings"] = items


def verify_completion(
    ticket_id: str,
    *,
    graph: bool | None = None,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> dict:
    """Verify a ticket's completion requirements and return a ``completion_verdict`` dict.

    Args:
        ticket_id: the ticket to verify (id, short id, or alias).
        graph: include the ticket's descendants in the context. Default: ``True`` for an
            epic (its success criteria are met across children), else ``False``.
        repo_root: rebar repo root (defaults to the resolved root).
        config: an :class:`LLMConfig` (defaults to :meth:`LLMConfig.from_env`).
        runner: an explicit runner (test seam; defaults to the config-selected runner).

    Returns a validated ``completion_verdict`` dict ``{verdict: "PASS"|"FAIL", findings[],
    summary?, target, reviewers, runner, model, trace_id}``. On FAIL, ``findings`` is
    non-empty; each finding carries the failing ``criterion``, an explanation (``detail``),
    and ``citations`` resolved against the real repo. Raises :class:`LLMError` subclasses on
    missing deps/credentials or a failed/empty structured run.
    """
    import rebar

    cfg = config or LLMConfig.from_env(repo_root=repo_root)
    # Default to a decisive verifier model unless the operator EXPLICITLY chose a non-default
    # one (cfg.model == DEFAULT_MODEL means REBAR_LLM_MODEL/[tool.rebar.llm].model was unset or
    # left at the framework default → use the verifier default; any other value is an explicit
    # choice and wins). Mirrors the step-floor pattern below.
    if cfg.model == DEFAULT_MODEL:
        cfg = replace(cfg, model=_VERIFIER_DEFAULT_MODEL)
    # Raise the agent step budget to a verification-appropriate floor (an explicit higher
    # REBAR_LLM_MAX_STEPS still wins) so a multi-criteria verification doesn't trip the
    # recursion cap mid-run.
    if cfg.max_iterations < _VERIFY_MIN_STEPS:
        cfg = replace(cfg, max_iterations=_VERIFY_MIN_STEPS)
    reviewer = prompts.get_reviewer(_REVIEWER_ID)

    # graph default depends on ticket type (epics verify across children). One extra local
    # read of the root ticket (reduce_ticket-backed; no network).
    if graph is None:
        root = rebar.show_ticket(ticket_id, repo_root=repo_root)
        graph = root.get("ticket_type") == "epic"

    context, ids = operations._assemble_context(ticket_id, graph=graph, repo_root=repo_root)
    # Fence the UNTRUSTED context so the prompt's instruction-hierarchy clause can refer to it
    # unambiguously (the delimiting half of the OWASP/Anthropic prompt-injection mitigation).
    fenced = f"<untrusted_ticket_context>\n{context}\n</untrusted_ticket_context>"
    variables = {
        "ticket_id": ids[0],
        "ticket_context": fenced,
        "repo_path": cfg.repo_path or "",
    }
    system_prompt, langfuse_prompt = prompts.resolve_prompt(reviewer, variables, cfg.langfuse)
    instructions = (
        f"Verify whether ticket {ids[0]} has met every completion requirement it states"
        + (" (including requirements met by its child tickets)" if graph else "")
        + ".\n\n"
        "You have read-only repository tools and a read-only ticket tool — USE them, do not "
        "rely on memory or guess at the code:\n"
        "- list_directory(path): explore structure (generated/ignored files are hidden)\n"
        "- search_files(regex, path): locate code; returns `path:line` matches\n"
        "- read_file(path, line_start, line_end): read exact lines; PAGE large files\n"
        "- show_ticket(ticket_id): read this ticket or any related/child ticket (JSON)\n\n"
        "Ground EVERY finding in what the tools actually return — cite real `path:line` from "
        "read_file output and never invent paths, line numbers, or file contents. Be DECISIVE: "
        "spend a few targeted searches/reads per criterion, then judge it and move on (don't "
        "exhaustively trace wiring or re-read files) — you have a limited step budget. Emit one "
        "finding per FAILING requirement only, then report the verdict (PASS/FAIL) and findings "
        "via the structured output as soon as every criterion is judged."
    )

    runner_sel = get_runner(cfg, override=runner)
    # Probe runner readiness up front (import-only, no model call) so a missing `agents`
    # extra / misconfig degrades cleanly BEFORE any billable call — this is what makes the
    # close gate's fail-closed path fire on missing infra.
    runner_sel.preflight()

    req = RunRequest(
        system_prompt=system_prompt,
        instructions=instructions,
        config=cfg,
        reviewers=[_REVIEWER_ID],
        target={"kind": "ticket", "ticket_ids": ids},
        langfuse_prompt=langfuse_prompt,
        mode="structured",
        output_schema=_OUTPUT_SCHEMA,
        extra_tools=_readonly_ticket_tools(cfg.repo_path),
    )
    result = runner_sel.run(req)  # {verdict, findings, summary?, runner, model, trace_id}

    result["target"] = {"kind": "ticket", "ticket_ids": ids}
    result["reviewers"] = [_REVIEWER_ID]
    # The structured path skips normalize_finding (unlike the findings path); normalize each
    # finding here (clamp severity, coerce citations to {kind,…}, strip nulls). normalize_finding
    # KEEPS unknown keys, so the per-finding `criterion` survives.
    result["findings"] = [
        findings.normalize_finding(f, reviewer_id=_REVIEWER_ID)
        for f in result.get("findings", [])
    ]
    findings.resolve_citations(result, cfg.repo_path)  # downgrade hallucinated file: citations
    _reconcile(result)  # normalize verdict; enforce FAIL⇔findings
    # Double validation is intentional: the runner validated the raw payload once; this checks
    # the op's own normalize/reconcile mutations stay in-shape. Both are shape-only (no conflict).
    return findings.validate_structured(result, _OUTPUT_SCHEMA)
