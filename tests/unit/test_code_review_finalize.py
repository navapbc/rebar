"""The Pass-1 finder's tokens reach the finalized code-review totals (task 514d).

The review bot's CloudWatch counts come from ``finalize._attach_code_review_metrics``, which
folds each succeeded step's ``_usage``. The Pass-2 ``verify`` and Pass-3 ``decide`` agent steps
get theirs attached by the runner, but the Pass-1 finder batch is driven by
``CodeReviewBatchRunner`` — it dispatches one agent call per overlay and returns ONE step
output, so unless that runner aggregates and surfaces its own ``_usage`` the whole finder leg is
invisible and every review under-reports.

These tests wire the two halves together and assert on the FOLDED TOTAL rather than on the
presence of a key, so they prove the number is complete rather than that a dict has a field.
The consumer half (``_attach_code_review_metrics``'s arithmetic) is covered by
``test_review_bot_token_metrics.py`` and is deliberately unchanged here.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.code_review.finalize import _attach_code_review_metrics
from rebar.llm.workflow.runners import BatchRunRequest, BatchRunResult

pytestmark = pytest.mark.unit


class _StubAgentRunner:
    """Answer each overlay dispatch with a fixed finding + the given per-call ``_usage``.

    Stands in for ``RunnerAgentStep``: the batch runner only reads ``.run(ctx).outputs``, and
    the real runner attaches ``_usage`` there (``runner._extract_usage``).
    """

    def __init__(self, usages: list[dict[str, int] | None]) -> None:
        self._usages = list(usages)
        self.calls: list[str] = []

    def run(self, ctx) -> BatchRunResult:
        self.calls.append(ctx.step_id)
        usage = self._usages.pop(0) if self._usages else None
        outputs: dict[str, Any] = {"findings": [{"finding": "n", "location": "a.py:1"}]}
        if usage is not None:
            outputs["_usage"] = dict(usage)
        return BatchRunResult(outputs=outputs)


def _request(criteria: tuple[dict[str, str], ...]) -> BatchRunRequest:
    return BatchRunRequest(
        finder="code-review-finder",
        criteria=criteria,
        usd_budget=None,
        model_ladder=(),
        workflow={},
        target_ticket=None,
        repo_root=None,
        run_id="run-1",
        step_id="round_a",
    )


_CRITERIA = (
    {"prompt": "code-review-security", "criterion_id": "security"},
    {"prompt": "code-review-tests", "criterion_id": "tests"},
)


def _run_batch(usages: list[dict[str, int] | None]) -> dict[str, Any]:
    runner = CodeReviewBatchRunner(context="## Diff\n(fake)")
    return runner.run(_request(_CRITERIA), agent_runner=_StubAgentRunner(usages)).outputs


def test_finder_batch_tokens_reach_the_finalized_totals():
    """The discriminating assertion: the finder leg's tokens are IN the folded totals.

    RED before the emit change — the batch step carried no ``_usage``, so the fold contributed
    zero and every number below came out as only the verify step's share.
    """
    outputs = _run_batch(
        [
            {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5, "requests": 1},
            {"input_tokens": 30, "output_tokens": 4, "cache_write_tokens": 7, "requests": 1},
        ]
    )
    rec = types.SimpleNamespace(
        steps=[
            {
                "status": "succeeded",
                "kind": "batch",
                "step_id": "round_a",
                "duration_ms": 40,
                "outputs": outputs,
            },
            {
                "status": "succeeded",
                "kind": "agent",
                "step_id": "verify",
                "duration_ms": 10,
                "outputs": {"_usage": {"input_tokens": 1, "output_tokens": 2, "requests": 1}},
            },
        ]
    )
    verdict: dict[str, Any] = {"verdict": "PASS", "blocking": [], "advisory": []}
    _attach_code_review_metrics(verdict, rec, 100.0)

    m = verdict["coverage"]["metrics"]
    assert m["input_tokens"] == 131  # 100 + 30 finder + 1 verify
    assert m["output_tokens"] == 26  # 20 + 4 finder + 2 verify
    assert m["cache_read_tokens"] == 5
    assert m["cache_write_tokens"] == 7
    assert m["total_tokens"] == 157  # input + output only; cache tokens are reported separately


def test_finder_batch_usage_is_flat_so_the_finalize_fold_can_read_it():
    """``_attach_code_review_metrics`` reads the token fields at the TOP level of ``_usage``, so
    emitting the nested ``aggregate_usage`` payload there would silently fold to zero."""
    outputs = _run_batch([{"input_tokens": 100, "output_tokens": 20, "requests": 1}])
    usage = outputs["_usage"]
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["llm_calls"] == 2  # one record per overlay dispatch, usage-bearing or not
    assert "per_call" not in usage, "the step-output _usage must be the flat totals"


def test_full_finder_aggregate_rides_on_the_batch_plan():
    """The raw per-call records and per-criterion map are retained where plan-review keeps
    them (``coverage['usage']``, story d52a) so exact attribution stays reconstructable."""
    outputs = _run_batch(
        [
            {"input_tokens": 100, "output_tokens": 20, "requests": 1},
            {"input_tokens": 30, "output_tokens": 4, "requests": 1},
        ]
    )
    aggregate = outputs["batch_plan"]["usage"]
    assert [r["criteria"] for r in aggregate["per_call"]] == [["security"], ["tests"]]
    assert aggregate["per_criterion"]["security"]["input_tokens"] == 100
    assert aggregate["per_criterion"]["tests"]["input_tokens"] == 30
    assert aggregate["totals"]["input_tokens"] == 130


def test_a_runner_that_attaches_no_usage_contributes_zero_and_never_raises():
    """Usage is observability: an injected/offline runner attaches no ``_usage`` at all, which
    must degrade to an all-zero contribution rather than a KeyError mid-review."""
    outputs = _run_batch([None, None])
    assert outputs["_usage"]["input_tokens"] == 0
    assert len(outputs["findings"]) == 2  # the review itself is unaffected

    rec = types.SimpleNamespace(
        steps=[
            {
                "status": "succeeded",
                "kind": "batch",
                "step_id": "round_a",
                "duration_ms": 5,
                "outputs": outputs,
            }
        ]
    )
    verdict: dict[str, Any] = {"verdict": "PASS", "blocking": [], "advisory": []}
    _attach_code_review_metrics(verdict, rec, 12.0)
    assert verdict["coverage"]["metrics"]["total_tokens"] == 0
