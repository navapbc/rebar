"""LLM operations — the high-level capabilities the framework exposes.

This milestone ships ONE reference operation, :func:`review_ticket` (an LLM review
of a ticket or ticket-graph), wired end-to-end across library/CLI/MCP. It also
exposes :func:`select_reviewers`, the deterministic reviewer-selection rules the
future code-review operation will use.

The operation owns the **deterministic** parts (assembling the target ticket
context from rebar's own reads, resolving the reviewer prompt, picking the runner)
and delegates the non-deterministic agent run to a :class:`~rebar.llm.runner.Runner`.
The assembled context is deterministic (sorted, no timestamps) so reviews are
reproducible run-to-run.
"""

from __future__ import annotations

from rebar.llm import prompts
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMError
from rebar.llm.prompts import select_reviewers  # re-export (rules layer)
from rebar.llm.runner import RunRequest, Runner, get_runner

__all__ = ["review_ticket", "select_reviewers"]


def _default_reviewer_id() -> str:
    catalog = prompts.load_catalog()
    for rid, rv in catalog.items():
        if rv.default:
            return rid
    raise LLMError("no default reviewer is configured in the catalog")


def _format_ticket(t: dict) -> str:
    """Render one ticket dict (from rebar.show_ticket) as deterministic markdown."""
    lines = [
        f"### Ticket {t.get('ticket_id', '?')} — {t.get('title', '')}",
        f"- type: {t.get('ticket_type')}  status: {t.get('status')}  "
        f"priority: {t.get('priority')}",
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


def _assemble_context(ticket_id: str, *, graph: bool, repo_root) -> tuple[str, list[str]]:
    """Build the deterministic review context + the list of ticket ids reviewed."""
    import rebar

    root = ticket = rebar.show_ticket(ticket_id, repo_root=repo_root)
    resolved_id = root.get("ticket_id", ticket_id)
    blocks = [_format_ticket(ticket)]
    ids = [resolved_id]
    if graph:
        seen = {resolved_id}
        frontier = [resolved_id]
        while frontier:
            parent = frontier.pop(0)
            children = rebar.list_tickets(parent=parent, repo_root=repo_root)
            for child in sorted(children, key=lambda c: c.get("ticket_id", "")):
                cid = child.get("ticket_id")
                if cid and cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
                    frontier.append(cid)
                    blocks.append(_format_ticket(child))
    return "\n\n".join(blocks), ids


def review_ticket(
    ticket_id: str,
    reviewer_id: str | None = None,
    *,
    graph: bool = False,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> dict:
    """Run an LLM review of a ticket (or its graph) and return a ``review_result``.

    Args:
        ticket_id: the ticket to review (id, short id, or alias).
        reviewer_id: a reviewer from the catalog (default: the catalog's default).
        graph: also include the ticket's descendants, reviewed as one unit.
        repo_root: rebar repo root (defaults to the resolved root).
        config: an :class:`LLMConfig` (defaults to :meth:`LLMConfig.from_env`).
        runner: an explicit runner (the test-injection seam; defaults to the
            config-selected runner — ``langgraph`` unless overridden).

    Returns a validated ``review_result`` dict ({findings[], target, reviewers,
    runner, model, trace_id, summary}). Raises :class:`LLMError` subclasses on
    missing deps/credentials or a failed/empty structured review.
    """
    cfg = config or LLMConfig.from_env(repo_root=repo_root)
    rid = reviewer_id or _default_reviewer_id()
    reviewer = prompts.get_reviewer(rid)

    context, ids = _assemble_context(ticket_id, graph=graph, repo_root=repo_root)
    variables = {
        "ticket_id": ids[0],
        "ticket_context": context,
        "repo_path": cfg.repo_path or "",
    }
    system_prompt, langfuse_prompt = prompts.resolve_prompt(reviewer, variables, cfg.langfuse)
    instructions = (
        f"Review ticket {ids[0]}"
        + (" and its child tickets, as a unit," if graph else "")
        + f", along the '{reviewer.dimension}' dimension. Use your read-only file "
        "tools to ground every finding in the actual repository, then return your "
        "findings via the structured output."
    )

    req = RunRequest(
        system_prompt=system_prompt,
        instructions=instructions,
        config=cfg,
        reviewers=[rid],
        target={"kind": "ticket_graph" if graph else "ticket", "ticket_ids": ids},
        langfuse_prompt=langfuse_prompt,
    )
    return get_runner(cfg, override=runner).run(req)
