"""Ticket review runs with at least ``_REVIEW_MIN_STEPS``.

The default ``max_iterations`` is 250, and the review floor is 120. A configured lower value rises
to the floor, while a higher ``REBAR_LLM_MAX_STEPS`` value remains unchanged. A recording runner
captures the request budget without an API call.
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
    assert cfg.max_iterations == 250  # the review-appropriate framework default (raised 50→250)
    fake = _RecordingRunner()

    rebar.llm.operations._review_ticket_impl(
        tid, "ticket-quality", config=cfg, runner=fake, repo_root=r
    )

    assert fake.seen_max_iterations is not None
    assert fake.seen_max_iterations >= _FLOOR


# ``review_code`` uses the gate workflow, whose verification step owns per-item budgeting.
# The disabled path makes no model calls. Both modes are covered by
# ``tests/unit/test_code_review_ws4.py``.


def test_review_ticket_operator_higher_budget_wins(rebar_repo: Path) -> None:
    """An explicit higher REBAR_LLM_MAX_STEPS is not lowered by the floor."""
    from dataclasses import replace

    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Review me", description="body", repo_root=r)
    cfg = replace(LLMConfig.from_env(repo_root=r), max_iterations=500)
    fake = _RecordingRunner()

    rebar.llm.operations._review_ticket_impl(
        tid, "ticket-quality", config=cfg, runner=fake, repo_root=r
    )

    assert fake.seen_max_iterations == 500
