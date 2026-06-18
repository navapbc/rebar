"""Unit tests for the read-only Mermaid render (WS-I)."""

from __future__ import annotations

from rebar.llm.workflow import render

WF = {
    "schema_version": "1",
    "name": "demo",
    "steps": [
        {"id": "fetch", "uses": "fetch_ticket"},
        {"id": "review", "prompt": "code_quality", "needs": ["fetch"]},
        {"id": "gate", "uses": "gate", "needs": ["review"]},
    ],
}


def test_renders_flowchart_with_nodes_and_edges() -> None:
    out = render.to_mermaid(WF)
    assert "flowchart TD" in out
    # nodes labelled by id + kind
    assert "fetch" in out and "scripted: fetch_ticket" in out
    assert "agent: code_quality" in out
    # edges follow needs
    assert "fetch --> review" in out
    assert "review --> gate" in out


def test_render_is_deterministic() -> None:
    assert render.to_mermaid(WF) == render.to_mermaid(WF)


def test_hyphenated_ids_are_sanitized() -> None:
    wf = {
        "schema_version": "1",
        "name": "h",
        "steps": [
            {"id": "a-b", "uses": "u"},
            {"id": "c-d", "uses": "u", "needs": ["a-b"]},
        ],
    }
    out = render.to_mermaid(wf)
    # node ids are Mermaid-safe (no hyphen in the id token) and the edge resolves
    assert "a_b --> c_d" in out
    # label keeps the real id
    assert "a-b" in out


def test_large_graph_degrades_to_text_outline() -> None:
    steps = [{"id": f"s{i}", "uses": "u"} for i in range(render.MAX_GRAPH_NODES + 1)]
    # make a single terminal so it's a coherent (if large) workflow shape
    for i in range(1, len(steps)):
        steps[i]["needs"] = [f"s{i - 1}"]
    wf = {"schema_version": "1", "name": "big", "steps": steps}
    out = render.to_mermaid(wf)
    assert "flowchart TD" not in out
    assert "too large for a graph render" in out
    assert "view as text" in out


def test_init_directive_pins_limits() -> None:
    out = render.to_mermaid(WF)
    assert "%%{init:" in out
    assert "maxEdges" in out


def test_render_workflow_by_dict() -> None:
    assert "flowchart TD" in render.render_workflow(WF)
