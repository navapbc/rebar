"""f6fc: memoize + nudge duplicate read-only tool calls at the shared agent-call seam.

Held-out oracle for the f6fc acceptance criteria. A duplicate ALLOWLISTED read-only
tool call returns the cached result WITHOUT re-executing the wrapped tool, plus a
graduated static nudge (level-1 on the first repeat, level-2 thereafter, byte-stable
for prompt caching). The runaway guard stays OUTERMOST and still observes every
duplicate signature — so a synthetic loop still trips the actuator with the memo
active. Error results (the "Error:"-prefixed strings the read-only tools return; they
never raise into the agent loop) are NOT memoized and re-execute on retry; the
deterministic sentinel-empty results ("(no matches)") ARE memoized — the core waste
case. Only the four read-only tools are wrapped; write/non-deterministic tools always
execute.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rebar.llm import pai_tools
from rebar.llm.config import LLMConfig
from rebar.llm.errors import RunawayToolLoopError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit


def _cfg(max_iterations: int = 200) -> LLMConfig:
    return replace(
        LLMConfig.from_env(), runner="pydantic_ai", repo_path=".", max_iterations=max_iterations
    )


def _req(cfg: LLMConfig) -> RunRequest:
    return RunRequest(
        system_prompt="x",
        instructions="gather evidence",
        config=cfg,
        mode="text",
        reviewers=[],
    )


def _install_single_tool(monkeypatch, tool):
    """Replace the verifier's read-only toolset with exactly one function tool, so
    ``info.function_tools[0]`` is that tool and no other tool names dilute the run."""
    monkeypatch.setattr(pai_tools, "filesystem_tools", lambda repo_path: [tool])
    monkeypatch.setattr(pai_tools, "grounding_tools", lambda repo_path: [])
    monkeypatch.setattr(pai_tools, "rebar_tools", lambda *a, **k: [])
    monkeypatch.setattr(pai_tools, "mcp_toolsets", lambda *a, **k: [])


def _counting_read_file(results, calls: dict):
    """A stub NAMED ``read_file`` (allowlisted) that records each execution and returns
    ``results`` — a fixed string, or a callable ``i -> str`` of the 0-based exec index."""

    def read_file(path: str = ".", line_start: int = 1, line_end: int = 0) -> str:
        i = calls["n"]
        calls["n"] += 1
        return results(i) if callable(results) else results

    return read_file


def _counting_search_files(results, calls: dict):
    """A stub NAMED ``search_files`` (allowlisted) — for the sentinel-empty case."""

    def search_files(query: str, path: str = ".") -> str:
        calls["n"] += 1
        return results

    return search_files


def _counting_named(name: str, results, calls: dict):
    """A stub with an ARBITRARY name (used for the non-allowlisted assertion)."""

    def tool(path: str = ".") -> str:
        calls["n"] += 1
        return results

    tool.__name__ = name
    return tool


def _driver(*, n_tool_calls: int, args: dict, seen: list | None = None) -> FunctionModel:
    """Emit ``n_tool_calls`` identical calls to ``function_tools[0]`` then land. When
    ``seen`` is given, record the content of every ToolReturnPart the model receives."""
    state = {"i": 0}

    def model(messages, info: AgentInfo):
        if seen is not None:
            for part in getattr(messages[-1], "parts", []):
                if isinstance(part, ToolReturnPart):
                    seen.append(part.content)
        state["i"] += 1
        if state["i"] > n_tool_calls:
            return ModelResponse(parts=[TextPart("landed")])
        return ModelResponse(parts=[ToolCallPart(tool_name=info.function_tools[0].name, args=args)])

    return FunctionModel(model)


# ── SHOWN: core memoization semantics ──────────────────────────────────────────────────


def test_duplicate_allowlisted_call_returns_cached_without_reexecuting(monkeypatch):
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_read_file("file-body", calls))
    cfg = _cfg()

    result = PydanticAIRunner(
        cfg, model_override=_driver(n_tool_calls=5, args={"path": "src/x.py"})
    ).run(_req(cfg))

    assert result["text"] == "landed"
    assert calls["n"] == 1, (
        f"5 identical allowlisted calls must execute the wrapped tool ONCE, ran {calls['n']}x"
    )


def test_deterministic_sentinel_empty_is_memoized(monkeypatch):
    """The 62x-on-'(no matches)' waste case: an honest, stable empty result IS cached."""
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_search_files("(no matches)", calls))
    cfg = _cfg()
    seen: list = []

    PydanticAIRunner(
        cfg, model_override=_driver(n_tool_calls=4, args={"query": "needle"}, seen=seen)
    ).run(_req(cfg))

    assert calls["n"] == 1, f"'(no matches)' is deterministic and must be cached, ran {calls['n']}x"
    repeats = [c for c in seen if c != "(no matches)"]
    assert repeats, "a memoized duplicate must still carry a repeat nudge"
    assert all(c.startswith("(no matches)") for c in repeats), "the cached body must be preserved"


def test_only_allowlisted_tools_are_memoized(monkeypatch):
    """A non-allowlisted tool (e.g. the banked-verification sibling's write) ALWAYS runs."""
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_named("record_criterion_verdict", "ok", calls))
    cfg = _cfg()

    PydanticAIRunner(cfg, model_override=_driver(n_tool_calls=5, args={"path": "src/x.py"})).run(
        _req(cfg)
    )

    assert calls["n"] == 5, (
        f"a non-allowlisted tool must never be memoized: ran {calls['n']}x, expected 5"
    )


def test_wrapper_order_is_guard_then_memo_then_steering(monkeypatch):
    """The memo wrapper sits BETWEEN the runaway guard (outermost) and the steering
    boundary (innermost), so the guard observes every signature before the memo can
    short-circuit and the steering notice remains the tool-budget backstop."""
    from rebar.llm.agent_call import build_agent_kwargs

    cfg = _cfg()
    req = replace(_req(cfg), tool_step_limit=4)

    def read_file(path: str = ".") -> str:
        return "body"

    kwargs = build_agent_kwargs(cfg, req, [read_file], [], model_settings=None, web_caps=None)
    toolsets = kwargs["toolsets"]
    assert toolsets, "a tool-bearing call must produce wrapped toolsets"

    layers = []
    node = toolsets[0]
    while node is not None:
        layers.append(type(node).__name__)
        node = getattr(node, "wrapped", None)

    assert len(layers) >= 3, f"expected guard -> memo -> steering, got {layers}"
    assert "Guard" in layers[0], f"runaway guard must be OUTERMOST, got {layers}"
    assert "Memo" in layers[1], f"memo wrapper must sit under the guard, got {layers}"
    assert "Steering" in layers[2], f"steering must be innermost, got {layers}"


# ── HELD OUT: nudge escalation, error passthrough, guard interaction ─────────────────────


def test_nudge_escalates_level1_then_level2_and_is_byte_stable(monkeypatch):
    def run_once() -> list:
        calls = {"n": 0}
        _install_single_tool(monkeypatch, _counting_read_file("BODY", calls))
        cfg = _cfg()
        seen: list = []
        PydanticAIRunner(
            cfg, model_override=_driver(n_tool_calls=4, args={"path": "src/x.py"}, seen=seen)
        ).run(_req(cfg))
        return seen

    seen = run_once()
    # seen == results the model received for tool calls #1..#4 (novel, repeat1, repeat2, repeat3)
    assert len(seen) == 4, f"expected 4 observed tool returns, got {len(seen)}: {seen}"
    raw, r1, r2, r3 = seen

    assert raw == "BODY", "the first (novel) execution returns the raw body with no nudge"
    assert r1.startswith("BODY") and r1 != raw, "first repeat carries the level-1 nudge"
    assert r2.startswith("BODY") and r2 != r1, "second repeat escalates to a distinct level-2 nudge"
    assert r3 == r2, "further repeats stay at the byte-stable level-2 nudge"

    # Byte-stability across runs — a templated nudge must not vary run to run.
    assert run_once() == seen, "nudge text must be byte-identical across runs (prompt caching)"


def test_error_result_is_not_memoized_and_carries_no_nudge(monkeypatch):
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_read_file("Error: cannot read 'x'", calls))
    cfg = _cfg()
    seen: list = []

    PydanticAIRunner(
        cfg, model_override=_driver(n_tool_calls=3, args={"path": "src/x.py"}, seen=seen)
    ).run(_req(cfg))

    assert calls["n"] == 3, (
        f"an 'Error:' result must re-execute on every identical call, ran {calls['n']}x"
    )
    assert seen == ["Error: cannot read 'x'"] * 3, (
        f"error results must pass through verbatim with no nudge, saw {seen}"
    )


def test_error_then_success_reexecutes_then_caches(monkeypatch):
    """An error is transient: the retry re-executes and, on success, the result is cached."""
    calls = {"n": 0}
    _install_single_tool(
        monkeypatch,
        _counting_read_file(lambda i: "Error: transient" if i == 0 else "recovered", calls),
    )
    cfg = _cfg()
    seen: list = []

    PydanticAIRunner(
        cfg, model_override=_driver(n_tool_calls=4, args={"path": "src/x.py"}, seen=seen)
    ).run(_req(cfg))

    # call#1 -> Error (not cached); call#2 -> re-exec -> success (cached);
    # call#3,#4 -> cache hits. So exactly 2 executions.
    assert calls["n"] == 2, f"error re-executes then success caches: ran {calls['n']}x, expected 2"
    assert seen[0] == "Error: transient"
    assert seen[1] == "recovered", "the retry after an error must surface the fresh success"
    assert seen[2].startswith("recovered") and seen[2] != "recovered", (
        "once cached, the duplicate success carries a nudge"
    )


def test_runaway_guard_still_observes_memoized_duplicates_and_trips(monkeypatch):
    """Guard OUTERMOST: even when the memo would cache a successful repeat, the guard's
    window sees every duplicate signature and still aborts the synthetic loop — and the
    wrapped tool executed only ONCE, proving the memo was active during the trip."""
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_read_file("cached-body", calls))
    cfg = _cfg(max_iterations=200)

    with pytest.raises(RunawayToolLoopError):
        PydanticAIRunner(
            cfg, model_override=_driver(n_tool_calls=10_000, args={"path": "src/x.py"})
        ).run(_req(cfg))

    assert calls["n"] == 1, (
        f"the memo must serve every duplicate from cache (ran {calls['n']}x), yet the guard "
        "still trips on the repeated signatures"
    )


def test_parallel_batch_of_identical_calls_executes_wrapped_tool_once(monkeypatch):
    """AC #1 covers 'N identical calls', INCLUDING a parallel batch. pydantic-ai runs a
    single turn's tool calls CONCURRENTLY; without a per-signature lock both coroutines
    check the cache before either populates it (the race a live Bedrock run surfaced),
    so the wrapped tool would execute twice. The memo must serialise same-signature calls
    and execute the wrapped tool exactly once."""
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_read_file("batch-body", calls))
    cfg = _cfg()

    def batched(messages, info: AgentInfo):
        # Land on the SECOND request; the first emits THREE identical calls in one turn.
        if any(isinstance(p, ToolReturnPart) for p in getattr(messages[-1], "parts", [])):
            return ModelResponse(parts=[TextPart("landed")])
        name = info.function_tools[0].name
        return ModelResponse(
            parts=[ToolCallPart(tool_name=name, args={"path": "src/x.py"}) for _ in range(3)]
        )

    result = PydanticAIRunner(cfg, model_override=FunctionModel(batched)).run(_req(cfg))

    assert result["text"] == "landed"
    assert calls["n"] == 1, (
        f"a parallel batch of 3 identical calls must execute the wrapped tool ONCE, "
        f"ran {calls['n']}x (concurrent cache race)"
    )
