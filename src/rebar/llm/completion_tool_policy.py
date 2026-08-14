"""Completion-only policy for finite, incrementally banked repository evidence.

The policy is selected by private metadata on ``record_criterion_verdict``.  The metadata is
attached to the Python callable, so it is invisible to pydantic-ai's generated tool schema and
does not add a request field, public API, workflow key, or prompt vocabulary.  Unflagged calls
therefore retain the old tool surface and wrapper behavior exactly.

``agent_call.build_agent_kwargs`` composes toolsets from innermost to outermost as: existing
step-limit steering, memoization, this completion policy, then the runaway guard.  This layer
must remain outside memoization so a governed evidence action is counted by model response even
when the repository result is served from memo, and inside runaway detection so every attempted
call remains observable to the general loop guard.

Pydantic-ai may execute all tool calls in one model response concurrently and may copy toolsets
while preparing later steps.  Consequently the lock, response-kind markers, current criterion,
and evidence-response state below are closure-owned and shared by every wrapper/toolset copy.
The response marker is checked and claimed atomically before the selected tool is awaited.

The exclusion is per *kind*, not per call.  A ``run_step`` is claimed by the kind of its first
governed call, and the COMMIT discipline that the claim protects is the separation of evidence
gathering from verdict recording: a record must never share a response with evidence, and only
one verdict is recorded per response.  Read-only repository evidence calls do not need that
protection from each other, so further evidence calls in an evidence-claimed step execute
normally rather than being answered with a synthetic notice and dropped.  Suppressing them
would cost the criterion its evidence rather than a round trip, because the finite boundary
below counts responses: a response whose only useful call was suppressed still consumes one of
the three, so a persistently batching model can bank ``met=false`` against a file that exists.
Executing them is free of that risk and adds no round trip -- ``current_evidence_steps`` is a
set of steps, so N evidence calls in one response still consume exactly one evidence response.
Non-governed tools pass through and definitions remain advertised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

COMPLETION_EVIDENCE_POLICY_ATTR = "_rebar_completion_evidence_policy"
RECORD_CRITERION_VERDICT_TOOL = "record_criterion_verdict"
COMPLETION_EVIDENCE_TOOL_NAMES: frozenset[str] = frozenset(
    {"read_file", "list_directory", "search_files"}
)

COMMIT_STEERING_NOTICE = (
    "COMMIT steering: this response already selected its one governed completion action. "
    "Do not batch repository evidence or verdict records. Use the returned result, then make "
    "the next response's single action a record_criterion_verdict COMMIT when due."
)
ALL_CRITERIA_BANKED_NOTICE = (
    "All completion criterion ids are banked. Stop repository evidence calls and produce the "
    "final structured output from the banked evidence."
)
BOUNDED_FALLBACK_EVIDENCE = (
    "Bounded repository search reached three unique evidence responses without demonstrating "
    "that this criterion is met; recorded insufficient evidence (an evidence gap, not a "
    "refutation) at the finite-search boundary."
)
BOUNDED_FALLBACK_NOTICE = (
    "Completion evidence policy: recorded insufficient evidence for the current criterion at "
    "the three-response finite-search boundary. Continue with the next unbanked criterion."
)


@dataclass(frozen=True)
class CompletionEvidencePolicy:
    """Private completion-run contract carried by the record tool callable.

    ``criterion_ids`` is ordered exactly like the manifest presented to the model.  Both
    callbacks are live views over the run's criterion bank: ``banked_ids`` chooses the next
    first-unbanked id, while ``fallback_record`` banks the bounded insufficiency record
    (``met=false`` carrying the framework-set ``evidence_sufficient=False`` marker).
    """

    criterion_ids: tuple[str, ...]
    max_evidence_responses: int
    evidence_tool_names: frozenset[str]
    banked_ids: Callable[[], set[str]]
    fallback_record: Callable[[str, str], Any]


def attach_completion_evidence_policy(
    record_tool: Callable[..., Any], policy: CompletionEvidencePolicy
) -> Callable[..., Any]:
    """Attach schema-invisible policy metadata, unless its manifest is empty."""
    if policy.criterion_ids:
        setattr(record_tool, COMPLETION_EVIDENCE_POLICY_ATTR, policy)
    return record_tool


def make_completion_record_tool(bank: Any, criterion_ids: tuple[str, ...]):
    """Bind the bank's record tool and opt a non-empty manifest into completion policy.

    ``bank`` is deliberately structural here: the policy layer needs only ``make_record_tool``,
    ``record_insufficient`` and the live ``banked_ids`` view.  The bounded fallback banks an
    INSUFFICIENCY record — ``met=false`` plus the framework-set ``evidence_sufficient=False``
    marker — rather than a bare refutation, so exhaustion of the finite evidence search stays
    distinguishable from a positive refutation.  Avoiding an import of
    ``workflow.completion_banking`` keeps this module independent of orchestration.
    """
    record_tool = bank.make_record_tool()

    def fallback_record(criterion_id: str, evidence: str) -> Any:
        return bank.record_insufficient(criterion_id, evidence)

    return attach_completion_evidence_policy(
        record_tool,
        CompletionEvidencePolicy(
            criterion_ids=criterion_ids,
            max_evidence_responses=3,
            evidence_tool_names=COMPLETION_EVIDENCE_TOOL_NAMES,
            banked_ids=bank.banked_ids,
            fallback_record=fallback_record,
        ),
    )


def completion_evidence_policy_from_tools(
    tools: Iterable[Callable[..., Any]],
) -> CompletionEvidencePolicy | None:
    """Return only a valid private policy object found on a function tool.

    Detection deliberately does not infer completion mode from a tool name, request prompt,
    reviewer, or output schema.  Only the private metadata opt-in activates the wrapper.
    """
    for tool in tools:
        candidate = getattr(tool, COMPLETION_EVIDENCE_POLICY_ATTR, None)
        if isinstance(candidate, CompletionEvidencePolicy) and candidate.criterion_ids:
            return candidate
    return None


def wrap_completion_evidence_policy(toolsets: list, policy: CompletionEvidencePolicy) -> list:
    """Wrap every joined toolset with one shared completion evidence state machine."""
    from pydantic_ai.toolsets import WrapperToolset

    lock = asyncio.Lock()
    # run_step -> the kind of governed action that claimed it: "evidence" or "record".
    step_kind: dict[int, str] = {}
    current_id: str | None = None
    current_evidence_steps: set[int] = set()

    def _first_unbanked(live: set[str]) -> str | None:
        return next((cid for cid in policy.criterion_ids if cid not in live), None)

    def _sync_current(live: set[str]) -> str | None:
        nonlocal current_id, current_evidence_steps
        first = _first_unbanked(live)
        if first != current_id:
            current_id = first
            current_evidence_steps = set()
        return first

    @dataclass
    class _CompletionEvidenceToolset(WrapperToolset):
        async def call_tool(self, name, tool_args, ctx, tool):
            is_evidence = name in policy.evidence_tool_names
            is_record = name == RECORD_CRITERION_VERDICT_TOOL
            if not (is_evidence or is_record):
                return await self.wrapped.call_tool(name, tool_args, ctx, tool)

            step = int(ctx.run_step)
            target_id: str | None = None
            evidence_count = 0
            kind = "evidence" if is_evidence else "record"
            # Claim the response's governed kind before awaiting the selected tool.  The
            # closure-owned lock makes this check-and-set shared across every wrapped toolset.
            # Evidence may join an evidence-claimed step; every other repeat is still steered,
            # which keeps a verdict record from ever sharing a response with evidence.
            async with lock:
                claimed = step_kind.get(step)
                if claimed is not None and not (claimed == "evidence" and is_evidence):
                    return COMMIT_STEERING_NOTICE
                step_kind[step] = kind
                live = set(policy.banked_ids())
                first = _sync_current(live)
                if is_evidence:
                    if first is None:
                        return ALL_CRITERIA_BANKED_NOTICE
                    target_id = first
                    current_evidence_steps.add(step)
                    evidence_count = len(current_evidence_steps)

            result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
            if target_id is not None and evidence_count >= policy.max_evidence_responses:
                async with lock:
                    # A concurrently completed model record remains authoritative.  Only the
                    # still-unbanked id receives the immediate bounded-search fallback.
                    if target_id not in set(policy.banked_ids()):
                        policy.fallback_record(target_id, BOUNDED_FALLBACK_EVIDENCE)
                        return f"{result}\n\n{BOUNDED_FALLBACK_NOTICE}"
            return result

    return [_CompletionEvidenceToolset(wrapped=toolset) for toolset in toolsets]


__all__ = [
    "ALL_CRITERIA_BANKED_NOTICE",
    "BOUNDED_FALLBACK_EVIDENCE",
    "BOUNDED_FALLBACK_NOTICE",
    "COMMIT_STEERING_NOTICE",
    "COMPLETION_EVIDENCE_POLICY_ATTR",
    "COMPLETION_EVIDENCE_TOOL_NAMES",
    "CompletionEvidencePolicy",
    "attach_completion_evidence_policy",
    "completion_evidence_policy_from_tools",
    "make_completion_record_tool",
    "wrap_completion_evidence_policy",
]
