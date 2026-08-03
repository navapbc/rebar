"""Live-runtime validation of the WORKFLOW ENGINE end-to-end (epic a88f follow-up).

The hermetic tier runs workflows with the offline FakeRunner (dry_run) — strong on
control flow + persistence, but it never exercises the real agent leg of a
workflow. This is the external counterpart: the retained ``review_skeleton`` sample
run against a LIVE model, so the full overlay→batch-finder→verify→decide path is
proven on the real runner (the RunnerAgentStep bridge), not just the fake.

Marked ``external`` (excluded from the default run; needs REBAR_RUN_EXTERNAL=1) and skips unless
the ``agents`` extra plus a credential for the CONFIGURED provider are present (``_live_llm``,
story f124). The workflow agent step resolves its model through the config, so a matrix arm's
``REBAR_LLM_CONFIG_FILE`` overlay repoints this whole path at that arm's provider. Run locally::

    REBAR_RUN_EXTERNAL=1 ANTHROPIC_API_KEY=… pytest -m external tests/external/test_workflow_live.py
"""

from __future__ import annotations

from pathlib import Path

import _live_llm
import pytest

import rebar
from rebar import schemas

pytestmark = pytest.mark.external

# Auto-marks this module's tests `llm_live` (tests/external/conftest.py).
_live_llm_ready = _live_llm.live_llm_ready()

_skip = _live_llm.skip_without_live_llm


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
    plan_review_fixture_plan: str,
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
        description=plan_review_fixture_plan,
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
