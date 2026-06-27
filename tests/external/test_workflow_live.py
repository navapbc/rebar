"""Live-runtime validation of the WORKFLOW ENGINE end-to-end (epic a88f follow-up).

The hermetic tier runs workflows with the offline FakeRunner (dry_run) — strong on
control flow + persistence, but it never exercises the real agent leg of a
workflow. This is the external counterpart: the retained ``review_skeleton`` sample
run against a LIVE model, so the full overlay→batch-finder→verify→decide path is
proven on the real runner (the RunnerAgentStep bridge), not just the fake.

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
def test_live_review_skeleton_workflow_end_to_end(rebar_repo: Path) -> None:
    # Run the RETAINED visual-editing sample (`review_skeleton`) on a LIVE model so the
    # real agent leg (the RunnerAgentStep bridge) is proven end-to-end on the v3 engine:
    # overlay precompute -> `batch` finder -> aggregate verify -> deterministic decide.
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

    # `review_skeleton` takes a `plan` string input; the `token` keyword fires the security
    # overlay so the conditionally-included `security` criterion participates in the batch.
    result = rebar.run_workflow(
        "review_skeleton",  # the retained packaged sample
        {"plan": "Harden the auth token check in auth.py — tokens must be verified."},
        ticket_id=tid,  # persist run-state on the ticket so status/result can replay it
        repo_root=str(rebar_repo),
    )

    # 1. The run-result conforms to the canonical contract (same schema the CLI +
    #    MCP reads validate against).
    schemas.validator(schemas.WORKFLOW_RUN).validate(result)
    assert result["status"] == "succeeded", result.get("error")
    assert result["dry_run"] is False  # the REAL agent leg ran (tokens spent)

    # 2. Every step reached a terminal status (overlay -> batch finders -> verify -> decide).
    steps = result.get("steps", {})
    assert steps.get("triggers") == "succeeded"
    assert steps.get("finders") == "succeeded"
    assert steps.get("verify") == "succeeded"
    assert steps.get("decide") == "succeeded"

    # 3. The status/result reads replay the same run from the ticket's events, and the
    #    Pass-1 finder batch produced a findings list (the real agent leg ran).
    status = rebar.get_workflow_status(result["run_id"], tid, repo_root=str(rebar_repo))
    schemas.validator(schemas.WORKFLOW_RUN).validate(status)
    full = rebar.get_workflow_result(result["run_id"], tid, repo_root=str(rebar_repo))
    schemas.validator(schemas.WORKFLOW_RUN).validate(full)
    finders_out = full.get("outputs", {}).get("finders", {})
    assert isinstance(finders_out.get("findings"), list)


@_skip
def test_live_plan_review_workflow_engine_produces_real_verdict(
    rebar_repo: Path,
) -> None:
    """The blind-spot GUARD (tepid-bus-pomp): run the plan-review gate through the WORKFLOW
    ENGINE against a LIVE model and assert it produces a real PASS/BLOCK verdict — NOT the
    INDETERMINATE the B5 cutover degraded to when the verify/coach steps lacked ``{{plan}}``.

    The offline parity harness uses canned agents that never call ``resolve_prompt``, so it
    cannot catch a missing prompt variable on the live path. This live test exercises the real
    ``RunnerAgentStep`` end-to-end (finders → verify → coach) so the regression can't recur.
    """
    import rebar.llm as llm

    tid = rebar.create_ticket(
        "story",
        "Persist the review cache to disk",
        description=(
            "## Why\nThe in-memory review cache is lost on restart.\n\n"
            "## What\nPersist it under `src/rebar/cache.py` behind the existing seam.\n\n"
            "## Scope\nJust persistence; eviction is out of scope.\n\n"
            "## Acceptance Criteria\n- [ ] the cache survives a restart\n"
            "- [ ] the seam writes through to disk\n"
        ),
        repo_root=str(rebar_repo),
    )

    verdict = llm.review_plan(tid, repo_root=str(rebar_repo), sign=False, emit_sidecar=False)

    # The fix's core guarantee: the workflow engine returns a REAL verdict, not INDETERMINATE
    # (which is what an unresolved `{{plan}}` / a failed verify step degrades to).
    assert verdict["verdict"] in ("PASS", "BLOCK"), verdict.get("coverage")
    assert verdict["coverage"].get("llm_ran") is True
    assert verdict["coverage"].get("llm_unavailable") is not True
    # The plan-review verdict conforms to its canonical schema on the workflow path.
    schemas.validator(schemas.PLAN_REVIEW_VERDICT).validate(verdict)
