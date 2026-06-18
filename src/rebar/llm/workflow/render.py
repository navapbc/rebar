"""Read-only render of a workflow to a Mermaid flowchart (WS-I).

Humans should see a workflow's shape without a canvas editor. This renders the
coordinate-free DSL to a dagre Mermaid flowchart — a one-way projection over the
canonical text, so there is no round-trip risk and nothing to keep in sync. The
output is TEXT (Mermaid source); rendering it to SVG is the host's job and the SVG
is never committed (the render is ephemeral).

Determinism: node ids derive from the (unique) step ids, sanitized to a Mermaid-safe
token with a stable collision guard, and steps are emitted in array order — so the
same document always renders byte-identically and an edit produces a minimal diff.

Graceful degradation: above a node/edge budget the graph is replaced by a compact
text outline ("view as text"), and a Mermaid ``init`` directive raises the host's
``maxEdges``/``maxTextSize`` so large-but-renderable graphs don't get truncated.
"""

from __future__ import annotations

import re
from typing import Any

from .schema import step_kind

# Above these, a Mermaid flowchart is unreadable/likely truncated by the host, so
# render a text outline instead (the "view as text" degradation).
MAX_GRAPH_NODES = 60
MAX_GRAPH_EDGES = 120

# Pinned Mermaid version note + host limits raised so a large-but-renderable graph
# is not silently truncated. Kept conservative.
_INIT_DIRECTIVE = (
    "%%{init: {'flowchart': {'defaultRenderer': 'dagre-wrapper'}, "
    "'maxEdges': 500, 'maxTextSize': 90000}}%%"
)


def _safe_id(step_id: str, used: set[str]) -> str:
    """A deterministic, Mermaid-safe node id for ``step_id`` (collision-guarded)."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", step_id) or "n"
    candidate = base
    i = 1
    while candidate in used:
        candidate = f"{base}__{i}"
        i += 1
    used.add(candidate)
    return candidate


def _label(step: dict[str, Any]) -> str:
    kind = step_kind(step)
    if kind == "agent":
        detail = f"agent: {step.get('prompt', '?')}"
    else:
        detail = f"scripted: {step.get('uses', '?')}"
    sid = step.get("id", "?")
    # Quote the label; neutralize characters that trip stricter Mermaid renderers /
    # securityLevel settings: double-quotes, backticks, and angle brackets. (Step
    # ids/refs are already constrained by the schema, so this only ever bites an
    # unusual prompt id.)
    raw = f"{sid}\\n({detail})"
    for bad, repl in (('"', "'"), ("`", "'"), ("<", "("), (">", ")")):
        raw = raw.replace(bad, repl)
    return raw


def to_mermaid(doc: dict[str, Any]) -> str:
    """Render a workflow document to a Mermaid flowchart (or a text outline when it
    exceeds the graph budget). Pure + deterministic."""
    steps = [s for s in doc.get("steps", []) if isinstance(s, dict)]
    edge_count = sum(len(s.get("needs") or []) for s in steps)
    if len(steps) > MAX_GRAPH_NODES or edge_count > MAX_GRAPH_EDGES:
        return _text_outline(doc, steps, edge_count)

    used: set[str] = set()
    node_ids: dict[str, str] = {}
    for s in steps:
        node_ids[s["id"]] = _safe_id(s["id"], used)

    lines = [_INIT_DIRECTIVE, "flowchart TD"]
    for s in steps:
        lines.append(f'    {node_ids[s["id"]]}["{_label(s)}"]')
    for s in steps:
        for need in s.get("needs") or []:
            if need in node_ids:  # a dangling need is a lint error, not a render crash
                lines.append(f"    {node_ids[need]} --> {node_ids[s['id']]}")
    return "\n".join(lines) + "\n"


def _text_outline(doc: dict[str, Any], steps: list[dict[str, Any]], edges: int) -> str:
    """The large-graph fallback: a compact, deterministic text listing."""
    name = doc.get("name", "<workflow>")
    lines = [
        f"# {name}: {len(steps)} steps, {edges} edges "
        f"(too large for a graph render — view as text)",
    ]
    for s in steps:
        kind = step_kind(s)
        ref = s.get("prompt") if kind == "agent" else s.get("uses")
        needs = ", ".join(s.get("needs") or []) or "-"
        lines.append(f"  {s.get('id', '?')} [{kind}:{ref}]  needs: {needs}")
    return "\n".join(lines) + "\n"


def render_workflow(source, repo_root: str | None = None) -> str:
    """Load a workflow (dict/path/name) and render it to Mermaid text."""
    from .runs import load_workflow_doc

    doc = load_workflow_doc(source, repo_root)
    return to_mermaid(doc)


__all__ = ["to_mermaid", "render_workflow", "MAX_GRAPH_NODES", "MAX_GRAPH_EDGES"]
