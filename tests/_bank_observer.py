"""Shared bookkeeping seams for the completion-banking behavioral eval.

``make_observed_upsert`` is the signature-faithful bookkeeping wrapper for
``CriterionBank.upsert``. The completion-banking behavioral eval (``tests/external``)
monkeypatches ``CriterionBank.upsert`` with a stub that records which criteria are banked
while still delegating to the real upsert. That stub MUST stay faithful to the real
``upsert`` signature: the real method carries keyword-only ``evidence_sufficient`` and
``seeded`` markers (framework-set on the bounded-fallback and cache-seed paths), and
production passes them. A stub that only accepts ``source`` silently drops those kwargs and
raises ``TypeError`` the moment the verifier enters the bounded-fallback path — the exact
drift that broke the live bedrock arm (bug ``9c7c-4844-f53c-4eac``).

``make_response_counting_wrap`` and ``bounded_bank_gaps`` are the eval's measurement and
oracle, denominated in evidence RESPONSES (distinct ``ctx.run_step`` values at which a
governed repository tool actually executed). That is the unit the completion evidence policy
enforces (``max_evidence_responses`` counts run_steps in
``src/rebar/llm/completion_tool_policy.py``); an executed-CALL denomination went stale when
commit ``e1352535ace`` (ticket ``dd41-239d-6e09-4a86``) deliberately let batched governed
reads execute, decoupling calls from responses (bug ``9507-4676-27af-4344``).

Factoring these here lets the live eval and fast, live-dependency-free unit regressions
(``tests/unit/test_completion_bank_observer_forwarding.py``,
``tests/unit/test_completion_banking_oracle.py``) share one implementation, so drift is
exercised by the unit tier rather than only surfacing on a live provider arm.
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
    """Build a drop-in ``CriterionBank.upsert`` replacement that records the first ``tool``
    write of each criterion (into ``writes`` / ``calls_at_first_write``) and then forwards
    verbatim to ``original_upsert``.

    Every keyword argument other than ``source`` (which the bookkeeping inspects) is passed
    through unchanged via ``**kwargs``, so ``evidence_sufficient``, ``seeded``, and any future
    keyword-only param the real ``upsert`` grows flow through without the stub having to know
    about them.
    """

    def observed_upsert(
        self: Any,
        criterion_id: str,
        met: bool,
        evidence: str,
        *,
        source: str = "tool",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if source == "tool" and criterion_id not in writes:
            writes.append(criterion_id)
            calls_at_first_write.append(evidence_calls())
        return original_upsert(self, criterion_id, met, evidence, source=source, **kwargs)

    return observed_upsert


def make_response_counting_wrap(
    original_wrap: Callable[..., list],
    evidence_steps: set[int],
) -> Callable[..., list]:
    """Build a drop-in ``wrap_completion_evidence_policy`` that also counts evidence responses.

    The returned function calls ``original_wrap`` (the real policy layer) and wraps each
    policy-governed toolset so that every governed evidence tool call that actually EXECUTED
    adds its ``ctx.run_step`` to ``evidence_steps``. A call the policy answered with a
    synthetic notice (``COMMIT_STEERING_NOTICE`` or ``ALL_CRITERIA_BANKED_NOTICE``) never
    executed, so it is not counted; an executed call whose result carries the appended
    ``BOUNDED_FALLBACK_NOTICE`` did execute and is counted. ``len(evidence_steps)`` is the
    number of evidence responses so far — the exact unit ``max_evidence_responses`` bounds.

    A bounded-fallback bank fires inside the policy layer before this outer counter records
    the triggering step, so a snapshot taken at that bank can read one response low. That
    direction is conservative for a ``<= max_gap`` oracle bound.
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
    """The banking oracle: every criterion banked incrementally within bounded evidence gaps.

    ``trial["responses_at_bank"]`` holds, per criterion in first-bank order, the number of
    evidence responses observed when that criterion first banked. The first bank must land
    within ``max_gap`` evidence responses, and each later first-bank within ``max_gap`` of
    the previous one — mirroring the policy's per-current-criterion
    ``max_evidence_responses`` enforcement. A trial that banked nothing, banked late, or
    ended without a verdict fails.
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
