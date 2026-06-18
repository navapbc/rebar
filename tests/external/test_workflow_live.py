"""Live-runtime validation of the WORKFLOW ENGINE end-to-end (epic a88f follow-up).

The hermetic tier runs workflows with the offline FakeRunner (dry_run) — strong on
control flow + persistence, but it never exercises the real agent leg of a
workflow. This is the external counterpart: the packaged ``code_review`` workflow
run against a LIVE model, so the full scripted→agent→gate→comment path is proven
on the real runner (the RunnerAgentStep bridge), not just the fake.

Marked ``external`` (excluded from the default run; needs REBAR_RUN_EXTERNAL=1) and
skips unless an API key + the ``agents`` extra are present. Run locally::

    REBAR_RUN_EXTERNAL=1 ANTHROPIC_API_KEY=… pytest -m external tests/external/test_workflow_live.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import rebar
from rebar import schemas

pytestmark = pytest.mark.external


def _have_live_model() -> bool:
    try:
        import rebar.llm as llm
    except Exception:
        return False
    if not llm.agents_extra_installed():
        return False
    # The workflow agent step uses REBAR_LLM_MODEL (default claude-opus-4-8); an
    # Anthropic key is the default credential. An OpenAI key also works if the
    # model is overridden, but the default path needs Anthropic.
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_skip = pytest.mark.skipif(not _have_live_model(), reason="no ANTHROPIC_API_KEY / agents extra")


@_skip
def test_live_code_review_workflow_end_to_end(rebar_repo: Path) -> None:
    tid = rebar.create_ticket(
        "task",
        "Harden auth token check",
        description=(
            "The token check is a stub.\n\n## Acceptance Criteria\n"
            "- [ ] tokens are actually verified"
        ),
        repo_root=str(rebar_repo),
    )
    # Give the reviewer something concrete to ground a finding in.
    (rebar_repo / "auth.py").write_text(
        "def check(token):\n    return True  # TODO: actually verify\n", encoding="utf-8"
    )

    result = rebar.run_workflow(
        "code_review",  # the packaged example
        {"ticket_id": tid},
        ticket_id=tid,
        repo_root=str(rebar_repo),
    )

    # 1. The run-result conforms to the canonical contract (same schema the CLI +
    #    MCP reads validate against).
    schemas.validator(schemas.WORKFLOW_RUN).validate(result)
    assert result["status"] == "succeeded", result.get("error")
    assert result["dry_run"] is False  # the REAL agent leg ran (tokens spent)

    # 2. Every step reached a terminal status and the agent step produced findings.
    steps = result.get("steps", {})
    assert steps.get("fetch") == "succeeded"
    assert steps.get("review") == "succeeded"
    assert steps.get("gate") == "succeeded"
    assert steps.get("comment") == "succeeded"

    # 3. The status/result reads replay the same run from the ticket's events.
    status = rebar.get_workflow_status(result["run_id"], tid, repo_root=str(rebar_repo))
    schemas.validator(schemas.WORKFLOW_RUN).validate(status)
    full = rebar.get_workflow_result(result["run_id"], tid, repo_root=str(rebar_repo))
    schemas.validator(schemas.WORKFLOW_RUN).validate(full)
    review_out = full.get("outputs", {}).get("review", {})
    assert isinstance(review_out.get("findings"), list)

    # 4. The comment_verdict step recorded the verdict back on the ticket.
    shown = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    blob = str(shown)
    assert result["run_id"] in blob  # the idempotency marker [rebar-run <run_id>/comment]
