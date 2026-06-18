"""Review operations reframed as built-in workflows (WS-K2, strangler-fig).

The review ops (``review_ticket`` …) are the engine's first real workloads. This
module expresses ``review_ticket`` as a one-step **agent workflow** that runs
through the executor + the :class:`RunnerAgentStep` bridge, and provides a
parallel-run-and-diff that proves the workflow path yields the SAME findings as the
legacy path. Per the strangler-fig rule, the documented entry points
(``rebar.llm.review_ticket`` etc.) are NOT changed here — they remain the legacy
path; this adds the equivalent workflow path + the diff that gates a future cutover.
"""

from __future__ import annotations

from typing import Any

from rebar.llm import operations
from rebar.llm.workflow import runs as _runs


def review_ticket_workflow_doc(reviewer_id: str) -> dict[str, Any]:
    """The built-in ``review_ticket`` workflow: a single agent step whose ``prompt``
    is the reviewer, producing a ``review_result`` (mode=findings)."""
    return {
        "schema_version": "1",
        "name": "review_ticket",
        "inputs": {"ticket_id": {"type": "string", "required": True}},
        "steps": [
            {
                "id": "review",
                "prompt": reviewer_id,
                "mode": "findings",
                "output_schema": "review_result",
                "with": {"ticket_id": "${{ inputs.ticket_id }}"},
            }
        ],
    }


def run_review_ticket_as_workflow(
    ticket_id: str,
    reviewer_id: str | None = None,
    *,
    graph: bool = False,
    repo_root=None,
    runner=None,
) -> dict[str, Any]:
    """Run ``review_ticket`` via the workflow engine and return its ``review_result``.

    Assembles context exactly like the legacy op, then runs the one-step agent
    workflow through the executor with the (optionally injected) review runner. The
    terminal step's outputs ARE the review_result."""
    rid = reviewer_id or operations._default_reviewer_id()
    context, ids = operations._assemble_context(ticket_id, graph=graph, repo_root=repo_root)
    doc = review_ticket_workflow_doc(rid)
    # Pass the assembled context to the agent step (the bridge resolves the prompt).
    doc["steps"][0]["with"]["context"] = context
    res = _runs.run(
        doc,
        {"ticket_id": ids[0]},
        repo_root=repo_root,
        review_runner=runner,
    )
    if res["status"] != "succeeded":
        from rebar.llm.errors import LLMRunnerError

        raise LLMRunnerError(f"review_ticket workflow failed: {res.get('error')}")
    return res["terminal_output"]


def diff_review_paths(
    ticket_id: str,
    reviewer_id: str | None = None,
    *,
    graph: bool = False,
    repo_root=None,
    runner=None,
) -> dict[str, Any]:
    """Parallel-run-and-diff (WS-K2): run the LEGACY review_ticket and the WORKFLOW
    path with the SAME runner and compare their substance (findings + summary).

    Returns ``{equivalent, legacy_findings, workflow_findings, legacy_summary,
    workflow_summary}``. Provenance (target.kind, runner name) legitimately differs
    between the paths; equivalence is judged on the FINDINGS the runner produced —
    the contract WS-K1 froze — so this gates a cutover without demanding byte
    equality."""
    legacy = operations.review_ticket(
        ticket_id, reviewer_id, graph=graph, repo_root=repo_root, runner=runner
    )
    wf = run_review_ticket_as_workflow(
        ticket_id, reviewer_id, graph=graph, repo_root=repo_root, runner=runner
    )
    equivalent = legacy.get("findings") == wf.get("findings") and legacy.get("summary") == wf.get(
        "summary"
    )
    return {
        "equivalent": equivalent,
        "legacy_findings": legacy.get("findings"),
        "workflow_findings": wf.get("findings"),
        "legacy_summary": legacy.get("summary"),
        "workflow_summary": wf.get("summary"),
    }
