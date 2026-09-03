"""Retirement guards for superseded plan-review workflow helpers."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_no_live_owner_text_mentions_pass4_coach() -> None:
    for rel in (
        "src/rebar/llm/plan_review/coach_moves.py",
        "src/rebar/llm/plan_review/orchestrator.py",
        "src/rebar/llm/plan_review/passes.py",
        "src/rebar/llm/plan_review/workflow_ops.py",
        "src/rebar/llm/workflow/gates/plan-review.yaml",
        "src/rebar/schemas/plan_review_coach_inputs_output.schema.json",
        "docs/plan-review-gate.md",
    ):
        assert "pass4_coach" not in _read(rel), f"{rel} still names pass4_coach"


def test_no_live_owner_text_mentions_guard_passes() -> None:
    for rel in (
        "src/rebar/llm/workflow/executor.py",
        "tests/unit/workflow/test_prior_art_hardening.py",
    ):
        assert "_guard_passes" not in _read(rel), f"{rel} still names _guard_passes"
