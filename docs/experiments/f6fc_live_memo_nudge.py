"""Live Bedrock session experiment for ticket f6fc-27fe-d3af-4f87.

Proves the f6fc live acceptance criterion:

    "Live Bedrock capture: a run demonstrating either a nudge-broken loop (duplicate
     signature followed by a different next call) or a nudge->actuator escalation,
     with usage-log figures, backed by a durable in-repo artifact."

It drives the REAL shared agent-call seam (``src/rebar/llm/agent_call.py``, the module
f6fc changes) through a live agentic ``PydanticAIRunner`` run against Amazon Bedrock.
The prompt steers the model into issuing the SAME read-only ``read_file`` call twice on
one file — the duplicate-tool-call waste f6fc memoizes. In practice the live model emits
both reads in ONE turn (a parallel batch), which pydantic-ai executes concurrently; the
memo's per-signature lock still collapses them to a single execution (AC #1's "N
identical calls").

To make the memo's effect directly measurable, the four read-only filesystem tools are
wrapped with an EXECUTION counter BELOW the memo layer. The memo wrapper sits above the
real tool, so:

    guard-observed ``tool_calls`` (the run-shape figure under ``result["_usage"]``)
    counts EVERY call the model emitted, including duplicates;
    ``real executions`` (this script's counter) counts only calls that reached the real
    tool.

A positive delta (tool_calls > real executions) is DIRECT proof the memo served the
duplicate from cache — the model got the cached file body plus a static nudge instead of
re-executing the tool, then produced its final answer (the nudge-broken loop). If instead
the model loops hard enough to trip the runaway actuator, the ``RunawayToolLoopError``
diagnostic (also usage-log figures) demonstrates the nudge->actuator escalation. Either
outcome satisfies the AC.

Run (requires live Bedrock credentials + region; the read-only file tools are gated, so
this ungated local experiment sets the audited override ``REBAR_GATE_ALLOW_UNGATED=1``):

    AWS_REGION=us-east-1 \
    REBAR_LLM_STANDARD_PROVIDER=bedrock \
    REBAR_LLM_STANDARD_MODEL=us.anthropic.claude-sonnet-4-6 \
    REBAR_GATE_ALLOW_UNGATED=1 \
    python docs/experiments/f6fc_live_memo_nudge.py

The transcript of a real run is recorded alongside this file in
``f6fc-live-memo-nudge.md``. This is a session experiment, deliberately NOT part of the
unit suite (the unit oracle is ``tests/unit/test_agent_call_memo_nudge.py``).
"""

from __future__ import annotations

import dataclasses
import functools
import tempfile
from pathlib import Path

from rebar.llm import pai_tools
from rebar.llm.config import LLMConfig
from rebar.llm.errors import RunawayToolLoopError
from rebar.llm.runner import PydanticAIRunner, RunRequest

MODEL = "bedrock:us.anthropic.claude-sonnet-4-6"

PROMPT = (
    "You have read-only repository tools. Do EXACTLY this and nothing else:\n"
    "1. Call read_file with ONLY the single argument path='sample.py' (do NOT pass "
    "line_start or line_end). Read the file.\n"
    "2. To be absolutely certain you read it correctly, call read_file AGAIN with the "
    "IDENTICAL single argument path='sample.py' (again do NOT pass line_start or "
    "line_end).\n"
    "3. Then STOP calling tools and answer in one sentence: what the function in "
    "sample.py returns.\n"
)


def _counting_filesystem_tools(exec_counts: dict):
    """Wrap the REAL read-only filesystem tools with a per-name execution counter that
    lives BELOW the memo layer, preserving each tool's name/signature so the memo
    allowlist and pydantic-ai schema introspection are unchanged."""
    real_builder = pai_tools.filesystem_tools

    def builder(repo_path):
        wrapped = []
        for fn in real_builder(repo_path):
            name = fn.__name__

            def make(inner, inner_name):
                @functools.wraps(inner)
                def counting(*args, **kwargs):
                    exec_counts[inner_name] = exec_counts.get(inner_name, 0) + 1
                    return inner(*args, **kwargs)

                return counting

            wrapped.append(make(fn, name))
        return wrapped

    return builder


def main() -> None:
    with tempfile.TemporaryDirectory() as repo:
        # A minimal one-file repo so search_files has a real tree to scan (and genuinely
        # finds no matches for ABSENT_TOKEN).
        (Path(repo) / "sample.py").write_text("def hello():\n    return 'world'\n")

        cfg = dataclasses.replace(
            LLMConfig.from_env(), runner="pydantic_ai", repo_path=repo, model=MODEL
        )
        exec_counts: dict[str, int] = {}
        pai_tools.filesystem_tools = _counting_filesystem_tools(exec_counts)  # type: ignore[assignment]
        # Trim the toolset to the filesystem tools so the run stays focused and cheap.
        orig_grounding, orig_rebar = pai_tools.grounding_tools, pai_tools.rebar_tools
        pai_tools.grounding_tools = lambda repo_path: []  # type: ignore[assignment]
        pai_tools.rebar_tools = lambda *a, **k: []  # type: ignore[assignment]

        req = RunRequest(
            system_prompt="You are a precise, obedient repository assistant.",
            instructions=PROMPT,
            config=cfg,
            mode="text",
            reviewers=[],
        )

        print("=== f6fc LIVE memo+nudge experiment ===")
        print("model:", MODEL)
        outcome: dict | None = None
        runaway: RunawayToolLoopError | None = None
        try:
            outcome = PydanticAIRunner(cfg).run(req)
        except RunawayToolLoopError as exc:
            runaway = exc
        finally:
            pai_tools.grounding_tools = orig_grounding  # type: ignore[assignment]
            pai_tools.rebar_tools = orig_rebar  # type: ignore[assignment]

        real_search = exec_counts.get("read_file", 0)
        print("real read_file executions (below memo):", real_search)
        print("all real tool executions (below memo):", exec_counts)

        if runaway is not None:
            diag = getattr(runaway, "diagnostic", {}) or {}
            print("OUTCOME: nudge -> actuator escalation (runaway tripped)")
            print("  tool_calls (guard-observed):", diag.get("tool_calls"))
            print("  tool_calls_distinct:", diag.get("tool_calls_distinct"))
            print("  distinct_ratio_window:", diag.get("distinct_ratio_window"))
            print("  top_repeated_tool_calls:", diag.get("top_repeated_tool_calls"))
            assert diag.get("tool_calls", 0) > real_search, (
                "the memo must have served duplicates the real tool never re-executed"
            )
            return

        assert outcome is not None
        usage = outcome.get("_usage") or {}
        tool_calls = usage.get("tool_calls")
        distinct = usage.get("tool_calls_distinct")
        top = usage.get("top_repeated_tool_calls")
        print("OUTCOME: nudge-broken loop (model answered instead of looping)")
        print("  final answer:", (outcome.get("text") or "").strip())
        print("  tool_calls (guard-observed):", tool_calls)
        print("  tool_calls_distinct:", distinct)
        print("  top_repeated_tool_calls:", top)
        assert tool_calls is not None, "run-shape usage-log figures must be present"
        assert tool_calls > real_search, (
            f"memo must have served >=1 duplicate from cache: guard saw {tool_calls} tool "
            f"calls but the real tool executed only {real_search} time(s)"
        )
        print(
            f"PROOF: {tool_calls - real_search} duplicate call(s) served from the memo "
            "cache with a static nudge — the wrapped tool did NOT re-execute them."
        )


if __name__ == "__main__":
    main()
