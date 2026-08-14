"""The runaway tool-loop breaker and its shared observation ledger.

Extracted from ``agent_call`` along the guard's own call-graph seam (bugs c827 + 3211):
``agent_call.build_agent_kwargs`` composes the wrappers, this module owns loop
DETECTION. The memo layer (``agent_call._memo_toolsets``) reports executed work into
the :class:`ToolCallLedger`; the guard built by :func:`runaway_guard_toolsets` reads it
and aborts a looping run with :class:`~rebar.llm.errors.RunawayToolLoopError` so
bounded recovery can land a verdict instead of the UsageLimits budget killing the run
with none.

Why a ledger at all: the guard is OUTERMOST and the memo INNER, so at observation time
the guard cannot know whether a call will be served from cache (free) or actually
execute (work). The original c827 guard therefore counted memo-served repeats as loop
evidence — lever 2's path-normalized read memo (epic 10ae) made re-reads free, and the
guard aborted runs FOR those free re-reads (the bug-3211 incident: distinct-ratio 0.5
with read_file x13, every one a cache hit). The two layers fought each other. The
ledger separates "what the model attempted" from "what actually ran", and the guard's
two arms each read the stream that is honest for their question.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rebar.llm import usage_log


@dataclass
class ToolCallLedger:
    """Shared observation channel between the memo layer and the runaway guard.

    - ``observed`` — every attempted call signature (diagnostics; names what the model
      is repeating, cached or not).
    - ``executed`` — only calls that reached a wrapped tool (memo misses,
      non-allowlisted passthroughs, cache-window fallbacks). The guard's distinct-ratio
      loop predicate runs over THIS stream: repeats the memo absorbs cost nothing and
      prove nothing.
    - ``served_streak`` — consecutive observed calls answered WITHOUT executing anything
      (memo hits, governance notices). A full :data:`usage_log.REPETITION_WINDOW` of
      them means the model is chattering against caches/notices and making no progress
      at all — the backstop that keeps a pure cache-served loop bounded (it burns
      request budget even though tools are free), so detection still acts before
      UsageLimits kills the run with no verdict.

    The guard increments ``served_streak`` on every observation; the memo resets it via
    :meth:`record_executed` whenever real work runs. Instances live per Agent call
    (created in ``build_agent_kwargs``), like the closures they replace.
    """

    observed: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    served_streak: int = 0

    def record_executed(self, sig: str) -> None:
        self.executed.append(sig)
        self.served_streak = 0


def runaway_guard_toolsets(tools: list, toolsets: list, *, ledger: ToolCallLedger) -> list:
    """Wrap every toolset (function tools moved into one first, unless the steering
    boundary already moved them) with the runaway loop breaker. Two arms, both
    single-sourced on ``usage_log``'s window/threshold, both raising BEFORE the repeated
    call executes:

    - **Executed-work ratio** — when the trailing window's distinct ratio over EXECUTED
      calls (memo misses; see the ledger) falls to ``usage_log.REPETITION_TRIP_RATIO``
      the model is re-running real work in a cycle. Repeats the memo serves from cache
      are free and are NOT loop evidence — the guard no longer aborts runs for re-reads
      that lever 2 deliberately made costless.
    - **Served-streak backstop** — a full ``usage_log.REPETITION_WINDOW`` of consecutive
      calls answered without executing anything (cache hits, governance notices) means
      the model is chattering and making no progress; the run still burns request
      budget, so it is aborted into bounded recovery rather than left to burn out
      verdict-less.

    Wrapping only: tool DEFINITIONS stay advertised for the whole run (a
    provider-protocol requirement — an empty tool surface over a toolUse history is a
    Bedrock/Anthropic 400)."""
    from pydantic_ai.toolsets import FunctionToolset, WrapperToolset

    from rebar.llm.errors import RunawayToolLoopError

    checked_steps: set[int] = set()

    def _diagnostic(stream: list[str]) -> dict:
        """Repetition summary over the stream that TRIPPED (executed for the ratio arm,
        observed for the streak arm), plus both raw counts."""
        return {
            **usage_log._repetition_summary(stream),
            "tool_calls": len(ledger.observed),
            "executed_tool_calls": len(ledger.executed),
        }

    @dataclass
    class _RunawayGuardToolset(WrapperToolset):
        async def call_tool(self, name, tool_args, ctx, tool):
            # Served-streak backstop: checked BEFORE observing this call, so it fires
            # only after a full window of completed zero-execution calls. O(1) per call.
            if ledger.served_streak >= usage_log.REPETITION_WINDOW:
                raise RunawayToolLoopError(
                    "runaway tool-call loop detected: the last "
                    f"{ledger.served_streak} tool calls were all answered without "
                    "executing any tool (memo-cached results and governance notices). "
                    "The agent is repeating calls whose results it already has, so the "
                    "run was aborted — bounded recovery can now land a verdict instead "
                    "of the request budget burning out on the loop.",
                    diagnostic=_diagnostic(ledger.observed),
                )
            ledger.observed.append(usage_log.tool_call_signature(name, tool_args))
            ledger.served_streak += 1
            # At most ONE window computation per run_step (the pinned cost contract): a
            # parallel batch shares one step number, so only its first-observed call pays
            # the O(window) ratio; every other call in the batch is a plain append. The
            # executed list keys detection on EXECUTED tool calls, not steps — at 3
            # calls/request the window fills in 8 requests, not 24.
            step = ctx.run_step
            if step not in checked_steps:
                checked_steps.add(step)
                ratio = usage_log.window_distinct_ratio(ledger.executed)
                if ratio is not None and ratio <= usage_log.REPETITION_TRIP_RATIO:
                    diagnostic = _diagnostic(ledger.executed)
                    top = diagnostic["top_repeated_tool_calls"]
                    window = usage_log.REPETITION_WINDOW
                    raise RunawayToolLoopError(
                        "runaway tool-call loop detected: "
                        f"{len(set(ledger.executed[-window:]))} distinct "
                        f"tool-call signature(s) in the last {window} "
                        f"executed tool calls (distinct ratio {ratio} <= trip threshold "
                        f"{usage_log.REPETITION_TRIP_RATIO}; most repeated: {top[:1]}). "
                        "The agent is repeating the same tool calls, so the run was "
                        "aborted before executing the repeated call — bounded recovery "
                        "can now land a verdict instead of the step budget burning out "
                        "on the loop.",
                        diagnostic=diagnostic,
                    )
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)

    all_toolsets = [FunctionToolset(tools), *toolsets] if tools else list(toolsets)
    return [_RunawayGuardToolset(wrapped=ts) for ts in all_toolsets]
