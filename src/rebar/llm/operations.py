"""LLM operations — the high-level capabilities the framework exposes.

The original reference operation here was the single-pass review of a ticket or
ticket-graph. Story 316a deprecated it in favour of the plan-review gate (``rebar
review-plan`` / :func:`rebar.llm.review_plan`), which is now the primary review
surface, and the pre-1.0 breaking pass removed its three public entry points. The
engine itself has since been deleted, along with the last two internal callers
(the workflow-parity harness and the eval solver).

What remains is the deterministic material the surviving gates reuse:
:func:`assemble_context` renders a ticket's review context from rebar's own reads
— sorted, with no timestamps, so a gate run is reproducible run-to-run — and
:func:`select_reviewers` is the deterministic reviewer-selection rule the
code-review operation applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rebar.llm.prompting.prompts import select_reviewers  # re-export (rules layer)

if TYPE_CHECKING:
    from rebar.llm.workflow.completion_prefetch import PrefetchSpec

__all__ = ["assemble_context", "select_reviewers"]


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
    or the whole subtree when ``graph`` is true). This is the shared context-builder the
    mandatory completion gate (``llm.workflow.gate_ops``) consumes. It is public (SC1) so
    the gate reaches it through a documented contract instead of importing a
    leading-underscore sibling across modules.

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
