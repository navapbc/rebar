"""Shared completion-banking observations for external and unit tests.

The upsert wrapper forwards keyword-only arguments unchanged, including evidence_sufficient
and seeded. Banking counts durable model records and policy fallback snapshots, but excludes
seeded pre-run state. Evidence gaps count distinct executed run steps, matching the policy's
max_evidence_responses limit. Synthetic notices do not count as executed responses.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise
from typing import Any


def make_observed_upsert(
    original_upsert: Callable[..., Any],
    writes: list[str],
    calls_at_first_write: list[int],
    evidence_calls: Callable[[], int],
) -> Callable[..., Any]:
    """Record each criterion's first durable write and forward every upsert argument.

    Model records and bounded-fallback snapshots count. Seeded cache entries do not count as
    run progress. Keyword-only arguments pass through unchanged.
    """

    def observed_upsert(
        self: Any,
        criterion_id: str,
        met: bool,
        evidence: str,
        *,
        seeded: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not seeded and criterion_id not in writes:
            writes.append(criterion_id)
            calls_at_first_write.append(evidence_calls())
        return original_upsert(self, criterion_id, met, evidence, seeded=seeded, **kwargs)

    return observed_upsert


def make_response_counting_wrap(
    original_wrap: Callable[..., list],
    evidence_steps: set[int],
) -> Callable[..., list]:
    """Wrap governed tools and count distinct run steps that actually execute.

    Synthetic policy notices do not count. An executed call carrying a fallback notice does.
    The policy may bank before this outer wrapper records the triggering step, so a bank
    snapshot can be one response low.
    """
    from dataclasses import dataclass

    from pydantic_ai.toolsets import WrapperToolset

    from rebar.llm.completion_tool_policy import (
        ALL_CRITERIA_BANKED_NOTICE,
        COMMIT_STEERING_NOTICE,
    )

    unexecuted = {COMMIT_STEERING_NOTICE, ALL_CRITERIA_BANKED_NOTICE}

    def counting_wrap(toolsets: list, policy: Any) -> list:
        @dataclass
        class _ResponseCountingToolset(WrapperToolset):
            async def call_tool(self, name: str, tool_args: dict, ctx: Any, tool: Any) -> Any:
                result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
                if name in policy.evidence_tool_names and result not in unexecuted:
                    evidence_steps.add(int(ctx.run_step))
                return result

        wrapped = original_wrap(toolsets, policy)
        return [_ResponseCountingToolset(wrapped=toolset) for toolset in wrapped]

    return counting_wrap


def bounded_bank_gaps(trial: dict[str, Any], expected_criteria: int, *, max_gap: int = 3) -> bool:
    """Require first banks within max_gap executed responses of each prior first bank.

    Count durable model and bounded-fallback writes, excluding seeded state. A trial with no
    banks, a late bank, or no final verdict fails.
    """
    points = trial["responses_at_bank"]
    if not points:
        return False
    gaps = [points[0], *(later - earlier for earlier, later in pairwise(points))]
    return (
        trial["banked"] == expected_criteria
        and trial["verdict"] in {"PASS", "FAIL"}
        and len(gaps) == expected_criteria
        and all(gap <= max_gap for gap in gaps)
    )
