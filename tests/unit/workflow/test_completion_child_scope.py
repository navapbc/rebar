"""The completion gate must HONOR the caller's ``graph`` when assembling the verifier's context.

The close gate (``_commands.transition``) passes ``graph=False`` so an epic close verifies the
epic's OWN completion criteria, NOT its whole descendant subtree — children are separate tickets
gated on their own close (the deterministic child-closure precheck trusts their certified
signatures). Bug: ``completion_precheck`` re-derived ``graph = (ticket_type == "epic")``,
overriding the documented ``graph=False`` — so epic closes re-verified every descendant and blew
the step budget (see the step-floor history in ``completion.py``). This pins that the precheck
threads the caller's ``graph`` through to ``_assemble_context`` (with the epic default preserved
only when ``graph`` is not threaded — the standalone ``verify-completion`` path).
"""

from __future__ import annotations

import subprocess

import pytest

import rebar
from rebar.llm import operations
from rebar.llm.workflow.executor import StepContext
from rebar.llm.workflow.gate_ops import completion_precheck

pytestmark = pytest.mark.unit


def _repo_with_epic(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True
    )
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    # A CHILDLESS epic: the deterministic child-closure gate passes (no children), so the precheck
    # proceeds to assemble the verifier context — the code path under test.
    epic = rebar.create_ticket("epic", "Umbrella epic", repo_root=str(repo))
    return str(repo), epic


def _ctx(epic, repo, inputs):
    return StepContext(
        run_id="r",
        step_id="precheck",
        kind="uses",
        step={},
        inputs=inputs,
        workflow={"name": "completion-verification"},
        target_ticket=epic,
        repo_root=repo,
    )


def test_precheck_honors_caller_graph_false_for_epic(tmp_path, monkeypatch):
    repo, epic = _repo_with_epic(tmp_path, monkeypatch)
    seen: dict = {}

    def _spy(tid, *, graph, repo_root):
        seen["graph"] = graph
        return ("ctx-text", [str(tid)])

    monkeypatch.setattr(operations, "_assemble_context", _spy)

    out = completion_precheck(_ctx(epic, repo, {"ticket_id": epic, "graph": False}))

    assert out["run_verify"] is True  # childless epic passes the deterministic child-closure gate
    assert seen.get("graph") is False, (
        "the close gate passes graph=False (verify the epic's OWN criteria, not its whole "
        "descendant subtree); the precheck must honor it, not re-derive graph=(type=='epic')"
    )


def test_precheck_defaults_to_false_when_graph_not_threaded(tmp_path, monkeypatch):
    """When no graph is threaded (a direct workflow invocation), the precheck defaults to False —
    verify the ticket's OWN criteria. The epic-includes-descendants deep-review default is resolved
    UPSTREAM in verify_completion, not re-derived in the precheck (re-deriving was the bug)."""
    repo, epic = _repo_with_epic(tmp_path, monkeypatch)
    seen: dict = {}

    def _spy(tid, *, graph, repo_root):
        seen["graph"] = graph
        return ("ctx-text", [str(tid)])

    monkeypatch.setattr(operations, "_assemble_context", _spy)

    completion_precheck(_ctx(epic, repo, {"ticket_id": epic}))  # no graph key

    assert seen.get("graph") is False, (
        "no graph threaded → default False (own criteria); the epic-descendants default lives in "
        "verify_completion, not the precheck"
    )
