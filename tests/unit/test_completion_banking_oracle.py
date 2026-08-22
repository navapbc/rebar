"""Regression for the banking eval's response-denominated measurement and oracle (bug 9507).

Commit ``e1352535ace`` (ticket ``dd41-239d-6e09-4a86``) deliberately lets a batched model
response execute ALL of its governed repository reads, so the completion evidence policy's
finite boundary is denominated in evidence RESPONSES (distinct ``run_step`` values), never in
executed calls. The live eval in ``tests/external/test_completion_banking_behavior_0707.py``
must measure and bound the same unit: its previous executed-call oracle rejected the intended
batched behavior deterministically (``calls_at_bank=[11,11,11,11,11]``, 0/3 trials on the
bedrock arm — bug ``9507-4676-27af-4344``).

These tests replay batching transcripts through the REAL
``wrap_completion_evidence_policy`` wrapped by the shared response counter
(:func:`tests._bank_observer.make_response_counting_wrap`), then judge the trial with the
shared oracle (:func:`tests._bank_observer.bounded_bank_gaps`) — no live provider required.
They prove the re-denominated oracle ACCEPTS the dd41-intended batched shape and still
REJECTS the original 0707 defect shapes (zero banks at exhaustion; a first bank only after
the bounded evidence search should have fail-closed).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import _bank_observer  # noqa: E402

from rebar.llm.completion_tool_policy import (  # noqa: E402
    COMMIT_STEERING_NOTICE,
    CompletionEvidencePolicy,
    wrap_completion_evidence_policy,
)

pytestmark = pytest.mark.unit

_IDS = ("c1", "c2", "c3", "c4", "c5")


class _Ctx:
    def __init__(self, run_step: int) -> None:
        self.run_step = run_step


class _FakeToolset:
    async def call_tool(self, name: str, tool_args: dict, ctx: Any, tool: Any) -> str:
        return f"result:{name}:{ctx.run_step}"


class _Rig:
    """One trial harness mirroring the live eval's seams over the real policy layer."""

    def __init__(self) -> None:
        self.evidence_steps: set[int] = set()
        self.banked: dict[str, bool] = {}
        self.writes: list[str] = []
        self.responses_at_first_write: list[int] = []
        policy = CompletionEvidencePolicy(
            criterion_ids=_IDS,
            max_evidence_responses=3,
            evidence_tool_names=frozenset({"read_file", "list_directory", "search_files"}),
            banked_ids=lambda: set(self.banked),
            fallback_record=lambda cid, evidence: self._bank(cid, met=False),
        )
        counting_wrap = _bank_observer.make_response_counting_wrap(
            wrap_completion_evidence_policy, self.evidence_steps
        )
        self.toolset = counting_wrap([_FakeToolset()], policy)[0]

    def _bank(self, criterion_id: str, *, met: bool) -> None:
        if criterion_id not in self.banked:
            self.writes.append(criterion_id)
            self.responses_at_first_write.append(len(self.evidence_steps))
        self.banked[criterion_id] = met

    async def evidence_batch(self, step: int, calls: int) -> list[str]:
        ctx = _Ctx(step)
        return list(
            await asyncio.gather(
                *(
                    self.toolset.call_tool("read_file", {"path": f"f{i}"}, ctx, None)
                    for i in range(calls)
                )
            )
        )

    async def record(self, step: int, criterion_id: str) -> str:
        result = await self.toolset.call_tool(
            "record_criterion_verdict",
            {"criterion_id": criterion_id, "met": True, "evidence": "e"},
            _Ctx(step),
            None,
        )
        if result != COMMIT_STEERING_NOTICE:
            self._bank(criterion_id, met=True)
        return str(result)

    def trial(self, verdict: str | None) -> dict[str, Any]:
        return {
            "banked": len(set(self.writes)),
            "verdict": verdict,
            "error": None if verdict else "exhausted",
            "responses_at_bank": self.responses_at_first_write,
        }


def test_intended_batched_shape_is_accepted() -> None:
    """The dd41 shape that the stale executed-call oracle rejected 0/3 must now pass.

    Eleven governed reads batched into two responses, then five end-of-run records — the
    exact live signature ``calls_at_bank=[11,11,11,11,11]`` — collapses to two evidence
    responses, within the policy's three-response bound for every first bank.
    """
    rig = _Rig()

    async def scenario() -> None:
        assert await rig.evidence_batch(1, 6) == ["result:read_file:1"] * 6
        assert await rig.evidence_batch(2, 5) == ["result:read_file:2"] * 5
        for offset, criterion_id in enumerate(_IDS):
            await rig.record(3 + offset, criterion_id)

    asyncio.run(scenario())
    trial = rig.trial("PASS")
    assert trial["responses_at_bank"] == [2, 2, 2, 2, 2]
    assert _bank_observer.bounded_bank_gaps(trial, expected_criteria=5)


def test_zero_bank_exhaustion_is_rejected() -> None:
    """The original 0707 failure mode — exhaustion with an empty bank — must stay RED."""
    rig = _Rig()

    async def scenario() -> None:
        await rig.evidence_batch(1, 6)
        await rig.evidence_batch(2, 5)

    asyncio.run(scenario())
    trial = rig.trial(None)
    assert trial["responses_at_bank"] == []
    assert not _bank_observer.bounded_bank_gaps(trial, expected_criteria=5)


def test_persistent_search_fail_closes_within_bound_and_late_banks_stay_rejected() -> None:
    """A persistent searcher is fail-closed banked within three evidence responses.

    The real policy banks the current criterion at its third evidence-claimed response, so
    the observed first-bank point stays within the oracle bound; a shape whose first bank
    arrives later than the bound (the unenforced pre-0707 world) is rejected.
    """
    rig = _Rig()

    async def scenario() -> None:
        for step in (1, 2, 3):
            await rig.evidence_batch(step, 3)

    asyncio.run(scenario())
    assert rig.writes == ["c1"]
    assert rig.responses_at_first_write[0] <= 3

    late = {
        "banked": 5,
        "verdict": "PASS",
        "error": None,
        "responses_at_bank": [5, 5, 5, 5, 5],
    }
    assert not _bank_observer.bounded_bank_gaps(late, expected_criteria=5)


def test_steered_calls_do_not_count_as_evidence_responses() -> None:
    """A governed call the policy answers with a steering notice never executed."""
    rig = _Rig()

    async def scenario() -> None:
        assert await rig.record(1, "c1") != COMMIT_STEERING_NOTICE
        results = await rig.evidence_batch(1, 2)
        assert results == [COMMIT_STEERING_NOTICE, COMMIT_STEERING_NOTICE]

    asyncio.run(scenario())
    assert rig.evidence_steps == set()
    assert rig.responses_at_first_write == [0]
