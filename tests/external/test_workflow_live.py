"""This module tests the workflow engine through ``RunnerAgentStep``.

The tests exercise prompt resolution and model execution through the packaged
``review_skeleton`` stages. A ``REBAR_LLM_CONFIG_FILE`` overlay routes the full path to its
configured provider. The ``external`` marker excludes these tests by default, and ``_live_llm``
requires the ``agents`` extra plus the selected provider credential. Set
``REBAR_RUN_EXTERNAL=1`` to include them.
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
    # Exercise the packaged review_skeleton path through triggers, finders, verify, and decide
    # using RunnerAgentStep.
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

    # Require every workflow stage to finish successfully.
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
    """Resolve plan-review prompts through ``RunnerAgentStep`` and return PASS or BLOCK.

    The hermetic parity harness uses canned agents, so it cannot detect missing plan variables.
    This provider-backed path covers finders, verify, and coach and prevents missing ``{{plan}}``
    from degrading the verdict to INDETERMINATE.
    """
    import rebar.llm as llm

    tid = rebar.create_ticket(
        "story",
        "Persist the review cache to disk",
        description=plan_review_fixture_plan,
        repo_root=str(rebar_repo),
    )

    verdict = llm.review_plan(tid, repo_root=str(rebar_repo), sign=False, emit_sidecar=False)

    # Missing {{plan}} in verify or coach must not degrade the verdict to INDETERMINATE.
    assert verdict["verdict"] in ("PASS", "BLOCK"), verdict.get("coverage")
    assert verdict["coverage"].get("llm_ran") is True
    assert verdict["coverage"].get("llm_unavailable") is not True
    schemas.validator(schemas.PLAN_REVIEW_VERDICT).validate(verdict)
