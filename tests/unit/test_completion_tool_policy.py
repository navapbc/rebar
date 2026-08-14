"""Deterministic unit contract for completion-only evidence-tool governance."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.completion_tool_policy import (
    ALL_CRITERIA_BANKED_NOTICE,
    COMMIT_STEERING_NOTICE,
    COMPLETION_EVIDENCE_POLICY_ATTR,
    CompletionEvidencePolicy,
    attach_completion_evidence_policy,
    wrap_completion_evidence_policy,
)

pytestmark = pytest.mark.unit


class _Ctx:
    def __init__(self, run_step: int) -> None:
        self.run_step = run_step


class _FakeToolset:
    def __init__(self, *, block: asyncio.Event | None = None) -> None:
        self.block = block
        self.calls: list[tuple[str, dict, int]] = []
        self.definitions = {
            "read_file": object(),
            "record_criterion_verdict": object(),
            "unrelated": object(),
        }

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls.append((name, tool_args, ctx.run_step))
        if self.block is not None:
            await self.block.wait()
        return f"result:{name}:{ctx.run_step}"

    async def get_tools(self, ctx):
        return self.definitions


def _policy(
    ids=("c00-a", "c01-b"),
    *,
    banked: set[str] | None = None,
    fallbacks: list[tuple[str, str]] | None = None,
) -> CompletionEvidencePolicy:
    live = banked if banked is not None else set()
    recorded = fallbacks if fallbacks is not None else []

    def fallback_record(criterion_id: str, evidence: str) -> None:
        recorded.append((criterion_id, evidence))
        live.add(criterion_id)

    return CompletionEvidencePolicy(
        criterion_ids=tuple(ids),
        max_evidence_responses=3,
        evidence_tool_names=frozenset({"read_file", "list_directory", "search_files"}),
        banked_ids=lambda: set(live),
        fallback_record=fallback_record,
    )


def _wrapped(fake: _FakeToolset, policy: CompletionEvidencePolicy):
    return wrap_completion_evidence_policy([fake], policy)[0]


def test_policy_is_frozen_and_metadata_does_not_change_record_schema() -> None:
    policy = _policy(("c00-a",))

    def record_criterion_verdict(criterion_id: str, met: bool, evidence: str) -> str:
        return criterion_id

    signature = inspect.signature(record_criterion_verdict)
    attached = attach_completion_evidence_policy(record_criterion_verdict, policy)
    assert attached is record_criterion_verdict
    assert inspect.signature(attached) == signature
    assert getattr(attached, COMPLETION_EVIDENCE_POLICY_ATTR) is policy
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.max_evidence_responses = 9  # type: ignore[misc]


def test_empty_manifest_does_not_attach_metadata() -> None:
    def record_criterion_verdict() -> str:
        return "x"

    attach_completion_evidence_policy(record_criterion_verdict, _policy(()))
    assert not hasattr(record_criterion_verdict, COMPLETION_EVIDENCE_POLICY_ATTR)


def test_parallel_evidence_batch_executes_every_evidence_call() -> None:
    """A lost batch must cost a round trip at most, never the criterion's evidence.

    Suppressing the losers returned a synthetic notice in place of repository content while
    still consuming one of the three evidence responses, so a persistently batching model
    banked met=false against files that exist.
    """

    async def scenario() -> tuple[list[str], list[_FakeToolset]]:
        fakes = [_FakeToolset(), _FakeToolset(), _FakeToolset()]
        wrappers = wrap_completion_evidence_policy(fakes, _policy())
        results = await asyncio.gather(
            wrappers[0].call_tool("read_file", {"path": "a"}, _Ctx(1), None),
            wrappers[1].call_tool("search_files", {"query": "b"}, _Ctx(1), None),
            wrappers[2].call_tool("list_directory", {"path": "."}, _Ctx(1), None),
        )
        return results, fakes

    results, fakes = asyncio.run(scenario())
    assert sum(len(fake.calls) for fake in fakes) == 3
    assert COMMIT_STEERING_NOTICE not in results
    assert sorted(results) == [
        "result:list_directory:1",
        "result:read_file:1",
        "result:search_files:1",
    ]


def test_batched_evidence_consumes_exactly_one_evidence_response() -> None:
    """N evidence calls in one response still spend one of the three finite responses."""
    banked: set[str] = set()
    fallbacks: list[tuple[str, str]] = []
    fake = _FakeToolset()
    wrapper = _wrapped(fake, _policy(("c00-a",), banked=banked, fallbacks=fallbacks))

    async def scenario() -> None:
        for step in (1, 2):
            await asyncio.gather(
                wrapper.call_tool("read_file", {}, _Ctx(step), None),
                wrapper.call_tool("search_files", {}, _Ctx(step), None),
                wrapper.call_tool("list_directory", {}, _Ctx(step), None),
            )

    asyncio.run(scenario())
    assert len(fake.calls) == 6
    assert fallbacks == []
    assert banked == set()


def test_action_marker_is_set_atomically_before_the_first_await() -> None:
    async def scenario() -> tuple[str, _FakeToolset]:
        release = asyncio.Event()
        fake = _FakeToolset(block=release)
        wrapper = _wrapped(fake, _policy())
        first = asyncio.create_task(wrapper.call_tool("read_file", {}, _Ctx(4), None))
        await asyncio.sleep(0)
        second = await wrapper.call_tool("record_criterion_verdict", {}, _Ctx(4), None)
        release.set()
        await first
        return second, fake

    second, fake = asyncio.run(scenario())
    assert second == COMMIT_STEERING_NOTICE
    assert [name for name, _, _ in fake.calls] == ["read_file"]


def test_third_unique_evidence_response_banks_bounded_fallback_before_return() -> None:
    banked: set[str] = set()
    fallbacks: list[tuple[str, str]] = []
    fake = _FakeToolset()
    wrapper = _wrapped(fake, _policy(("c00-a",), banked=banked, fallbacks=fallbacks))

    async def scenario() -> str:
        await wrapper.call_tool("read_file", {}, _Ctx(1), None)
        await wrapper.call_tool("search_files", {}, _Ctx(2), None)
        return await wrapper.call_tool("list_directory", {}, _Ctx(3), None)

    result = asyncio.run(scenario())
    assert result.startswith("result:list_directory:3")
    assert "insufficient evidence" in result
    assert fallbacks and fallbacks[0][0] == "c00-a"
    assert "bounded" in fallbacks[0][1].casefold()
    assert "c00-a" in banked


def test_banking_current_id_resets_evidence_count_for_next_manifest_id() -> None:
    banked: set[str] = set()
    fallbacks: list[tuple[str, str]] = []
    wrapper = _wrapped(_FakeToolset(), _policy(banked=banked, fallbacks=fallbacks))

    async def scenario() -> None:
        await wrapper.call_tool("read_file", {}, _Ctx(1), None)
        await wrapper.call_tool("search_files", {}, _Ctx(2), None)
        banked.add("c00-a")
        await wrapper.call_tool("read_file", {}, _Ctx(3), None)
        await wrapper.call_tool("search_files", {}, _Ctx(4), None)
        await wrapper.call_tool("list_directory", {}, _Ctx(5), None)

    asyncio.run(scenario())
    assert [cid for cid, _ in fallbacks] == ["c01-b"]


def test_revision_of_older_id_preserves_current_id_evidence_state() -> None:
    banked = {"c00-a"}
    fallbacks: list[tuple[str, str]] = []
    wrapper = _wrapped(_FakeToolset(), _policy(banked=banked, fallbacks=fallbacks))

    async def scenario() -> None:
        await wrapper.call_tool("read_file", {}, _Ctx(1), None)
        await wrapper.call_tool("search_files", {}, _Ctx(2), None)
        await wrapper.call_tool(
            "record_criterion_verdict", {"criterion_id": "c00-a"}, _Ctx(3), None
        )
        await wrapper.call_tool("list_directory", {}, _Ctx(4), None)

    asyncio.run(scenario())
    assert [cid for cid, _ in fallbacks] == ["c01-b"]


def test_parallel_record_batch_executes_exactly_one_record() -> None:
    async def scenario() -> tuple[list[str], _FakeToolset]:
        fake = _FakeToolset()
        wrapper = _wrapped(fake, _policy())
        results = await asyncio.gather(
            wrapper.call_tool("record_criterion_verdict", {"criterion_id": "c00-a"}, _Ctx(1), None),
            wrapper.call_tool("record_criterion_verdict", {"criterion_id": "c01-b"}, _Ctx(1), None),
        )
        return results, fake

    results, fake = asyncio.run(scenario())
    assert len(fake.calls) == 1
    assert COMMIT_STEERING_NOTICE in results


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("record_criterion_verdict", "read_file"),
        ("read_file", "record_criterion_verdict"),
    ],
)
def test_mixed_batch_first_governed_action_wins(first: str, second: str) -> None:
    async def scenario() -> tuple[str, _FakeToolset]:
        release = asyncio.Event()
        fake = _FakeToolset(block=release)
        wrapper = _wrapped(fake, _policy())
        winner = asyncio.create_task(wrapper.call_tool(first, {}, _Ctx(9), None))
        await asyncio.sleep(0)
        loser = await wrapper.call_tool(second, {}, _Ctx(9), None)
        release.set()
        await winner
        return loser, fake

    loser, fake = asyncio.run(scenario())
    assert loser == COMMIT_STEERING_NOTICE
    assert [name for name, _, _ in fake.calls] == [first]


def test_all_banked_steers_evidence_to_final_output_but_allows_revision() -> None:
    banked = {"c00-a", "c01-b"}
    fake = _FakeToolset()
    wrapper = _wrapped(fake, _policy(banked=banked))

    async def scenario() -> tuple[str, str]:
        evidence = await wrapper.call_tool("read_file", {}, _Ctx(1), None)
        revision = await wrapper.call_tool(
            "record_criterion_verdict", {"criterion_id": "c00-a"}, _Ctx(2), None
        )
        return evidence, revision

    evidence, revision = asyncio.run(scenario())
    assert evidence == ALL_CRITERIA_BANKED_NOTICE
    assert revision == "result:record_criterion_verdict:2"
    assert [name for name, _, _ in fake.calls] == ["record_criterion_verdict"]


def test_non_governed_tools_pass_through_and_do_not_consume_response_action() -> None:
    fake = _FakeToolset()
    wrapper = _wrapped(fake, _policy())

    async def scenario() -> tuple[str, str]:
        unrelated = await wrapper.call_tool("unrelated", {}, _Ctx(1), None)
        evidence = await wrapper.call_tool("read_file", {}, _Ctx(1), None)
        return unrelated, evidence

    unrelated, evidence = asyncio.run(scenario())
    assert unrelated == "result:unrelated:1"
    assert evidence == "result:read_file:1"


def test_wrapper_keeps_underlying_tool_definitions_advertised() -> None:
    fake = _FakeToolset()
    wrapper = _wrapped(fake, _policy())
    assert asyncio.run(wrapper.get_tools(_Ctx(1))) is fake.definitions
