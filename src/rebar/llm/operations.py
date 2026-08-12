"""LLM operations — the high-level capabilities the framework exposes.

The original reference operation here was the single-pass review of a ticket or
ticket-graph. Story 316a deprecated it in favour of the plan-review gate (``rebar
review-plan`` / :func:`rebar.llm.review_plan`), which is now the primary review
surface, and the pre-1.0 breaking pass removed its three public entry points. The
engine survives as :func:`_review_ticket_impl` for the two internal callers (the
workflow-parity harness and the eval solver).
This module also exposes :func:`select_reviewers`, the deterministic
reviewer-selection rules the code-review operation uses.

The operation owns the **deterministic** parts (assembling the target ticket
context from rebar's own reads, resolving the reviewer prompt, picking the runner)
and delegates the non-deterministic agent run to a :class:`~rebar.llm.runner.Runner`.
The assembled context is deterministic (sorted, no timestamps) so reviews are
reproducible run-to-run.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMError
from rebar.llm.prompting import prompts
from rebar.llm.prompting.prompts import select_reviewers  # re-export (rules layer)
from rebar.llm.runner import Runner, RunRequest, get_runner

if TYPE_CHECKING:
    from rebar.llm.workflow.completion_prefetch import PrefetchSpec

__all__ = ["assemble_context", "default_reviewer_id", "select_reviewers"]

# A tool-using review is inherently multi-step (explore → search → read several files →
# report). The framework review default (REBAR_LLM_MAX_STEPS=50 ≈ 25 tool calls) is far too
# low and trips the recursion cap mid-review (→ LLMRunnerError 'agent exceeded its step
# budget'). Raise the agent step budget to a review-appropriate FLOOR; an operator who
# explicitly sets a HIGHER REBAR_LLM_MAX_STEPS still wins. Mirrors completion.py's verifier
# step-floor pattern (a review is lighter than a multi-criteria completion verify, whose floor
# is criteria-scaled via verify_step_floor); this review floor stays a flat min-only raise.
_REVIEW_MIN_STEPS = 120


def default_reviewer_id() -> str:
    """The catalog's default reviewer id (PUBLIC API). Raises :class:`LLMError` when no
    catalog entry is marked default. Public so cross-module callers (the review-workflow
    op and the completion gate) select the reviewer through a documented entry point rather
    than a leading-underscore sibling import (SC1)."""
    catalog = prompts.load_catalog()
    for rid, rv in catalog.items():
        if rv.default:
            return rid
    raise LLMError("no default reviewer is configured in the catalog")


def _format_ticket(t: dict) -> str:
    """Render one ticket dict (from rebar.show_ticket) as deterministic markdown."""
    lines = [
        f"### Ticket {t.get('ticket_id', '?')} — {t.get('title', '')}",
        f"- type: {t.get('ticket_type')}  status: {t.get('status')}  priority: {t.get('priority')}",
    ]
    if t.get("assignee"):
        lines.append(f"- assignee: {t['assignee']}")
    if t.get("tags"):
        lines.append(f"- tags: {', '.join(t['tags'])}")
    deps = t.get("deps") or []
    if deps:
        rels = sorted(f"{d.get('relation')}->{d.get('target_id')}" for d in deps)
        lines.append(f"- deps: {', '.join(rels)}")
    lines.append("")
    lines.append((t.get("description") or "(no description)").strip())
    comments = t.get("comments") or []
    if comments:
        lines.append("")
        lines.append("#### Comments")
        for c in comments:
            lines.append(f"- {(c.get('body') or '').strip()}")
    return "\n".join(lines)


def assemble_context(
    ticket_id: str,
    *,
    graph: bool,
    repo_root,
    prefetch: PrefetchSpec | None = None,
) -> tuple[str, list[str]]:
    """Build the deterministic LLM review context for a ticket (PUBLIC API).

    Returns ``(context_text, reviewed_ids)`` — the rendered, deterministic context block
    fed to a review/verify agent, and the list of ticket ids it covers (just ``ticket_id``,
    or the whole subtree when ``graph`` is true). This is the shared context-builder BOTH
    the single-pass review engine and the mandatory completion gate
    (``llm.workflow.gate_ops``) consume. It is public (SC1) so the gate reaches it through a
    documented contract instead of importing a leading-underscore sibling across modules.

    ``prefetch`` is OPT-IN (story a9dd): when ``None`` (the default, and every existing
    caller) this function is byte-identical to its pre-story behaviour. When a
    :class:`~rebar.llm.workflow.completion_prefetch.PrefetchSpec` is passed, the ticket's
    declared ``file_impact`` contents + referencing-commit diffs are pre-loaded into a bounded
    ``<prefetched_file_contents>`` section appended to the returned context (the completion
    gate does this so the verifier need not re-discover its declared files agentically)."""
    from rebar import _reads

    root = ticket = _reads.show_ticket(ticket_id, repo_root=repo_root)
    resolved_id = root.get("ticket_id", ticket_id)
    blocks = [_format_ticket(ticket)]
    ids = [resolved_id]
    if graph:
        from collections import deque

        seen = {resolved_id}
        frontier = deque([resolved_id])
        while frontier:
            parent = frontier.popleft()
            children = _reads.list_tickets(parent=parent, repo_root=repo_root)
            for child in sorted(children, key=lambda c: c.get("ticket_id", "")):
                cid = child.get("ticket_id")
                if cid and cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
                    frontier.append(cid)
                    blocks.append(_format_ticket(child))
    context = "\n\n".join(blocks)
    if prefetch is not None:
        from rebar.llm.workflow import completion_prefetch as _pf

        section, _manifest = _pf.assemble_prefetch(prefetch, repo_root=repo_root)
        if section:
            context = context + "\n\n" + section
    return context, ids


def _review_ticket_impl(
    ticket_id: str,
    reviewer_id: str | None = None,
    *,
    graph: bool = False,
    ref: str | None = None,
    source: str | None = None,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> dict:
    """The single-pass review-ticket engine.

    Its public entry points (the ``rebar review`` verb, ``rebar.llm.review_ticket``,
    and the ``review_ticket`` MCP tool) were removed in the pre-1.0 breaking pass;
    the remaining callers are the workflow-parity harness and the eval solver, which
    call this directly. Use ``rebar.llm.review_plan`` for the public review op."""
    from rebar.llm import gate_source

    handle = gate_source.resolve_gate_handle(ref, source, repo_root)
    with gate_source.gate_read_root(handle):
        cfg = config or LLMConfig.from_env(repo_root=repo_root)
        cfg = gate_source.apply_handle(cfg, handle)
        # Runs the OPERATOR'S configured model, deliberately — no class is bound here. This is a
        # top-level op's single LLM call, so there are no passes to differentiate, and the class
        # vocabulary exists to spend differently ACROSS the passes of a multi-pass gate. Binding a
        # class here would take `llm.model` away as a steering knob for this command while giving
        # nothing back. Same reasoning as `spec_scan`; both are registered as by-design in
        # `tests/unit/test_subcall_class_selection.py`. Story 316a retired this op's CLI verb;
        # it survives as a deprecated library/MCP surface, so this reasoning still applies to it.
        if cfg.max_iterations < _REVIEW_MIN_STEPS:
            cfg = replace(cfg, max_iterations=_REVIEW_MIN_STEPS)
        rid = reviewer_id or default_reviewer_id()
        prompt_repo_root = cfg.repo_path or repo_root
        reviewer = prompts.get_prompt(rid, repo_root=prompt_repo_root)

        context, ids = assemble_context(ticket_id, graph=graph, repo_root=repo_root)
        variables = {
            "ticket_id": ids[0],
            "ticket_context": context,
            "repo_path": cfg.repo_path or "",
        }
        # Tool-awareness steering: name the tools, tell the agent to USE them (not
        # guess), how to page large files, and to ground every claim in tool output —
        # the prompt-level reliability technique used by Claude Code / SWE-agent.
        base_instructions = (
            f"Review ticket {ids[0]}"
            + (" together with its child tickets, as a unit," if graph else "")
            + f", along the '{reviewer.dimension}' dimension.\n\n"
            "You have read-only repository tools — USE them, do not rely on memory or "
            "guess at the code:\n"
            "- list_directory(path): explore structure (generated/ignored files are hidden)\n"
            "- search_files(regex, path): locate code; returns `path:line` matches\n"
            "- read_file(path, line_start, line_end): read exact lines; PAGE large "
            "files with the line range instead of assuming their contents.\n\n"
            "Ground EVERY finding in what the tools actually return — cite real "
            "`path:line` from read_file output and never invent paths, line numbers, "
            "or file contents. Then report your findings via the structured output."
        )
        # Engine-wide caching (story c6e5): cache the byte-stable reviewer rubric and route
        # the volatile per-run ticket context to the user message. Unmarked → pre-S2 behavior.
        system_prompt, instructions, langfuse_prompt = prompts.resolve_prompt_cached(
            reviewer,
            variables,
            base_instructions=base_instructions,
            langfuse_cfg=cfg.langfuse,
            repo_root=prompt_repo_root,
        )

        # Model-max output budget (bug 30a2) — the shared review-kernel rule: a review
        # call's output is bounded by its actual findings, so it rides at the resolved
        # model's maximum output capacity (the raise-only per-request seam).
        from rebar.llm.review_kernel import max_output_cfg

        req = RunRequest(
            system_prompt=system_prompt,
            instructions=instructions,
            config=max_output_cfg(cfg),
            reviewers=[rid],
            target={"kind": "ticket_graph" if graph else "ticket", "ticket_ids": ids},
            langfuse_prompt=langfuse_prompt,
        )
        result = get_runner(cfg, override=runner).run(req)
    return gate_source.annotate_result(result, handle)
