"""WS-K3: the built-in code_review example workflow (end-to-end demonstrator).

scripted fetch -> agent review -> unsecured gate -> comment. Ships as package data
(src/rebar/llm/workflow/examples/code_review.yaml) and resolves by name. Snapshot-
tested for shape + driven end-to-end with the offline FakeRunner (no tokens).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.llm.workflow import lint as L
from rebar.llm.workflow import render, runs

pytest.importorskip("jsonschema")


def _example_path() -> Path:
    import rebar.llm.workflow as wf

    return Path(wf.__file__).resolve().parent / "examples" / "code_review.yaml"


def test_example_ships_as_package_data() -> None:
    assert _example_path().is_file()


def test_example_is_lint_clean() -> None:
    findings = L.lint_workflow(_example_path().read_text(), source="code_review")
    assert L.lint_passes(findings), "\n".join(str(f) for f in findings)


def test_example_resolves_by_name_and_renders() -> None:
    # Resolves by NAME via the packaged-examples fallback, and renders the 4-step DAG.
    mermaid = render.render_workflow("code_review")
    assert "flowchart TD" in mermaid
    for edge in ("fetch --> review", "review --> gate", "gate --> comment"):
        assert edge in mermaid, mermaid


def test_example_runs_end_to_end_dry(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Reviewable", description="some code work", repo_root=r)
    # dry_run = the agent step uses the offline FakeRunner (no tokens); the scripted
    # fetch/gate/comment steps run for real against the store.
    res = runs.run("code_review", {"ticket_id": tid}, ticket_id=tid, dry_run=True, repo_root=r)
    assert res["status"] == "succeeded", res
    assert res["steps"] == {
        "fetch": "succeeded",
        "review": "succeeded",
        "gate": "succeeded",
        "comment": "succeeded",
    }
    # The gate passed (FakeRunner emitted no findings) and a verdict comment landed.
    assert res["outputs"]["gate"]["verdict"] == "pass"
    assert res["outputs"]["comment"]["commented"] is True
    state = rebar.show_ticket(tid, repo_root=r)
    assert any("Workflow verdict: pass" in (c.get("body") or "") for c in state["comments"])


def test_example_run_persists_run_state(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "T", description="x", repo_root=r)
    res = runs.run("code_review", {"ticket_id": tid}, ticket_id=tid, dry_run=True, repo_root=r)
    # Run-state is durable on the ticket (WS-C1) and readable via status.
    st = rebar.get_workflow_status(res["run_id"], tid, repo_root=r)
    assert st["status"] == "succeeded"
    assert set(st["steps"]) == {"fetch", "review", "gate", "comment"}
