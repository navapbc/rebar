"""B1 regression: the review operations raise the agent step budget to a floor.

``review_ticket`` / ``review_code`` build their ``LLMConfig`` via
``LLMConfig.from_env()``. The framework default ``max_iterations`` is now **250**
(≈125 tool-call cycles), raised from 50 because 50 (~25 cycles) was far too few for a
tool-using review and tripped ``LLMRunnerError('agent exceeded its step budget')``.
The operations still apply a verification-style FLOOR (``_REVIEW_MIN_STEPS=120``) via
``max(floor, configured)`` — a guard for an operator who LOWERS the budget below the
floor; with the new default it no-ops (250 ≥ 120). An operator who sets a HIGHER
``REBAR_LLM_MAX_STEPS`` still wins. The invariant under test: each op runs at ``>= floor``.

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
    assert cfg.max_iterations == 250  # the review-appropriate framework default (raised 50→250)
    fake = _RecordingRunner()

    rebar.llm.review_ticket(tid, "ticket-quality", config=cfg, runner=fake, repo_root=r)

    assert fake.seen_max_iterations is not None
    assert fake.seen_max_iterations >= _FLOOR


def test_review_code_applies_step_floor(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+print(1)\n"
    cfg = LLMConfig.from_env(repo_root=r)
    assert cfg.max_iterations == 250
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
