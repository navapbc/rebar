"""B1 regression: the review operations raise the agent step budget to a floor.

``review_ticket`` / ``review_code`` build their ``LLMConfig`` via
``LLMConfig.from_env()`` whose framework default ``max_iterations=25`` maps to a
langgraph ``recursion_limit=25`` (~12 tool calls) — far too few for a tool-using
review, which trips ``LLMRunnerError('agent exceeded its step budget')``. The
operations must apply a verification-style FLOOR (mirroring completion.py's
``_VERIFY_MIN_STEPS``) so a default-config review gets a workable budget; an
operator who explicitly sets a HIGHER ``REBAR_LLM_MAX_STEPS`` still wins.

Offline: a recording fake runner captures the ``max_iterations`` the operation
hands the runner. No API call.
"""

from __future__ import annotations

from pathlib import Path

import rebar
from rebar.llm.config import LLMConfig
from rebar.llm.runner import RunRequest

_FLOOR = 120


class _RecordingRunner:
    """Records the per-request config's max_iterations; returns a minimal result."""

    name = "fake"

    def __init__(self) -> None:
        self.seen_max_iterations: int | None = None

    def preflight(self) -> None:  # offline, no-op
        pass

    def run(self, req: RunRequest) -> dict:
        self.seen_max_iterations = req.config.max_iterations
        from rebar.llm import findings as _findings

        return _findings.finalize_findings(
            [],
            runner=self.name,
            model=None,
            trace_id=None,
            target=req.target,
            reviewers=req.reviewers,
            summary=None,
            reviewer_id=req.reviewers[0] if len(req.reviewers) == 1 else None,
            repo_path=req.config.repo_path,
        )


def test_review_ticket_applies_step_floor(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Review me", description="body", repo_root=r)
    cfg = LLMConfig.from_env(repo_root=r)
    assert cfg.max_iterations == 25  # the framework default we are flooring above
    fake = _RecordingRunner()

    rebar.llm.review_ticket(tid, "ticket-quality", config=cfg, runner=fake, repo_root=r)

    assert fake.seen_max_iterations is not None
    assert fake.seen_max_iterations >= _FLOOR


def test_review_code_applies_step_floor(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+print(1)\n"
    cfg = LLMConfig.from_env(repo_root=r)
    assert cfg.max_iterations == 25
    fake = _RecordingRunner()

    rebar.llm.review_code(
        diff_text=diff, reviewers=["code-quality"], config=cfg, runner=fake, repo_root=r
    )

    assert fake.seen_max_iterations is not None
    assert fake.seen_max_iterations >= _FLOOR


def test_review_ticket_operator_higher_budget_wins(rebar_repo: Path) -> None:
    """An explicit higher REBAR_LLM_MAX_STEPS is not lowered by the floor."""
    from dataclasses import replace

    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Review me", description="body", repo_root=r)
    cfg = replace(LLMConfig.from_env(repo_root=r), max_iterations=500)
    fake = _RecordingRunner()

    rebar.llm.review_ticket(tid, "ticket-quality", config=cfg, runner=fake, repo_root=r)

    assert fake.seen_max_iterations == 500
