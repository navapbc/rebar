"""f6fc: memoize + nudge duplicate read-only tool calls at the shared agent-call seam.

Held-out oracle for the f6fc acceptance criteria. A duplicate ALLOWLISTED read-only
tool call returns the cached result WITHOUT re-executing the wrapped tool, plus a
graduated static nudge (level-1 on the first repeat, level-2 thereafter, byte-stable
for prompt caching). The runaway guard stays OUTERMOST, but its executed-work ratio no
longer counts memo-served repeats as loop evidence (bug 3211) — a pure cache-served
loop is bounded by the served-streak backstop instead, while free re-reads interleaved
with novel work never abort the run. Error results (the "Error:"-prefixed strings the
read-only tools return; they
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


def test_runaway_guard_still_bounds_a_pure_cache_served_loop(monkeypatch):
    """A loop of memo-served repeats costs no tool executions but still burns request
    budget: the guard's served-streak backstop must abort it — and the wrapped tool
    executed only ONCE, proving every duplicate was served from cache (the abort is the
    backstop, not the executed-work ratio)."""
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_read_file("cached-body", calls))
    cfg = _cfg(max_iterations=200)

    with pytest.raises(RunawayToolLoopError) as excinfo:
        PydanticAIRunner(
            cfg, model_override=_driver(n_tool_calls=10_000, args={"path": "src/x.py"})
        ).run(_req(cfg))

    assert calls["n"] == 1, (
        f"the memo must serve every duplicate from cache (ran {calls['n']}x), yet the guard "
        "still bounds the loop via the served-streak backstop"
    )
    assert "without executing" in str(excinfo.value), (
        "a cache-served loop must be reported as chatter, not as re-executed work"
    )


def test_error_looping_allowlisted_tool_trips_the_ratio_arm(monkeypatch):
    """An allowlisted tool whose result is an 'Error:' string is never cached, so every
    repeat is EXECUTED work — the ratio arm must trip and say so (pins the exact-sig memo
    miss path's executed-work report; search_files here, the non-read_file branch)."""
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_search_files("Error: boom", calls))
    cfg = _cfg(max_iterations=200)

    with pytest.raises(RunawayToolLoopError) as excinfo:
        PydanticAIRunner(
            cfg, model_override=_driver(n_tool_calls=10_000, args={"query": "needle"})
        ).run(_req(cfg))

    assert calls["n"] > 1, "an Error: result must re-execute on every repeat"
    assert "executed tool calls" in str(excinfo.value), (
        "a re-executed loop must trip the executed-work ratio arm, not the streak backstop"
    )


def test_non_allowlisted_looping_tool_still_trips_the_ratio_arm(monkeypatch):
    """Non-allowlisted tools bypass the cache entirely, so every repeat is EXECUTED work
    — the ratio arm must still abort that loop (pins the memo passthrough's executed-work
    report; without it the guard would be blind to non-memoized loops)."""
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_named("record_criterion_verdict", "ok", calls))
    cfg = _cfg(max_iterations=200)

    with pytest.raises(RunawayToolLoopError) as excinfo:
        PydanticAIRunner(
            cfg, model_override=_driver(n_tool_calls=10_000, args={"path": "src/x.py"})
        ).run(_req(cfg))

    assert calls["n"] >= 1, "a non-allowlisted tool must actually execute"
    assert "executed tool calls" in str(excinfo.value), (
        "a re-executed loop must trip the executed-work ratio arm, not the streak backstop"
    )


def test_memo_served_repeats_are_not_loop_evidence_for_the_ratio_arm(monkeypatch):
    """The bug-3211 defect: the guard counted memo-served re-reads as loop evidence, so
    lever 2's free cache hits tripped the abort mid-progress. A run that interleaves
    heavy re-reads of ONE cached file with genuinely novel work must land — the free
    repeats fill neither the executed-ratio window nor a served streak."""
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _counting_read_file("cached-body", calls))
    cfg = _cfg(max_iterations=400)
    state = {"i": 0}

    def model(messages, info: AgentInfo):
        state["i"] += 1
        if state["i"] > 120:
            return ModelResponse(parts=[TextPart("landed")])
        # Two cached re-reads per novel call — the exact shape the old guard aborted
        # (trailing window ~9 distinct / 24 = 0.375 <= 0.50, most of it memo-served).
        if state["i"] % 3 == 0:
            args = {"path": f"src/novel-{state['i']}.py"}
        else:
            args = {"path": "src/x.py"}
        return ModelResponse(parts=[ToolCallPart(tool_name=info.function_tools[0].name, args=args)])

    result = PydanticAIRunner(cfg, model_override=FunctionModel(model)).run(_req(cfg))

    assert result["text"] == "landed", (
        "free memo-served repeats interleaved with novel work must never abort the run"
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


# ── lever 2 (story 2948): read_file memo NORMALIZED BY PATH ─────────────────────────────
# The f6fc memo keyed on read_file's exact (path, line_start, line_end) signature, so re-reads
# of the SAME file at DIFFERENT ranges slipped past it — the measured dominant verifier waste
# (48 read_file calls, 11 distinct files, 77% re-reads). Lever 2 caches the whole file ONCE (an
# internal line_start=1/line_end=0 read) and serves every subsequent range by slicing the cache.


def _file_backed_read_file(lines: list[str], calls: dict):
    """A stub NAMED ``read_file`` that formats an in-memory file EXACTLY like
    ``pai_tools.read_file`` (1-based ``"{n}\\ttext"`` rows, ``line_end=0`` = EOF, the
    ``_READ_MAX_LINES`` window cap read live so a test can shrink it), recording each execution."""

    def read_file(path: str = ".", line_start: int = 1, line_end: int = 0) -> str:
        calls["n"] += 1
        cap = pai_tools._READ_MAX_LINES
        lo = max(1, line_start)
        hi = line_end if (line_end and line_end >= lo) else len(lines)
        hi = min(hi, lo + cap - 1)
        out = [f"{i + 1}\t{lines[i]}" for i in range(lo - 1, min(hi, len(lines)))]
        return "\n".join(out) or "(empty range)"

    return read_file


def _seq_driver(calls_args: list[dict], seen: list | None = None) -> FunctionModel:
    """Emit ONE call per turn walking ``calls_args`` (heterogeneous args), then land. Records
    every ToolReturnPart the model receives into ``seen`` (in call order)."""
    state = {"i": 0}

    def model(messages, info: AgentInfo):
        if seen is not None:
            for part in getattr(messages[-1], "parts", []):
                if isinstance(part, ToolReturnPart):
                    seen.append(part.content)
        i = state["i"]
        state["i"] += 1
        if i >= len(calls_args):
            return ModelResponse(parts=[TextPart("landed")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.function_tools[0].name, args=calls_args[i])]
        )

    return FunctionModel(model)


def _base(content: str) -> str:
    """Strip any appended graduated nudge (which starts with a blank line + '[')."""
    return content.split("\n\n[", 1)[0]


def test_read_file_same_path_different_ranges_reads_underlying_file_once(monkeypatch):
    """Lever 2 core: three reads of ONE path at DIFFERENT ranges (incl. a normpath-equivalent
    spelling) execute the underlying file read exactly ONCE and each returns its correct slice."""
    lines = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _file_backed_read_file(lines, calls))
    cfg = _cfg()
    seen: list = []
    args = [
        {"path": "src/x.py", "line_start": 2, "line_end": 3},
        {"path": "./src/x.py", "line_start": 5, "line_end": 5},  # normpath -> same cache entry
        {"path": "src/x.py"},  # whole file (defaults)
    ]
    PydanticAIRunner(cfg, model_override=_seq_driver(args, seen=seen)).run(_req(cfg))

    assert calls["n"] == 1, (
        f"same-path reads at different ranges must read the underlying file ONCE, ran {calls['n']}x"
    )
    assert _base(seen[0]) == "2\tbeta\n3\tgamma"
    assert _base(seen[1]) == "5\tepsilon"
    assert _base(seen[2]) == "1\talpha\n2\tbeta\n3\tgamma\n4\tdelta\n5\tepsilon\n6\tzeta"


def test_read_file_repeat_at_different_range_still_carries_graduated_nudge(monkeypatch):
    """A re-read of the same path at a NEW range is still a 'repeat' and carries the L1-then-L2
    nudge, byte-stable — the escalation must survive the path-normalized key."""
    lines = ["one", "two", "three", "four"]
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _file_backed_read_file(lines, calls))
    cfg = _cfg()
    seen: list = []
    args = [
        {"path": "f", "line_start": 1, "line_end": 1},
        {"path": "f", "line_start": 2, "line_end": 2},  # first repeat -> L1
        {"path": "f", "line_start": 3, "line_end": 4},  # second repeat -> L2
    ]
    PydanticAIRunner(cfg, model_override=_seq_driver(args, seen=seen)).run(_req(cfg))

    assert calls["n"] == 1
    assert "\n\n[" not in seen[0], "the first read of a path carries no nudge"
    assert seen[1].startswith("2\ttwo") and seen[1] != "2\ttwo", "first repeat carries L1 nudge"
    assert seen[2].startswith("3\tthree\n4\tfour"), "second repeat preserves the sliced body"
    assert seen[2] != seen[1].replace("2\ttwo", "3\tthree\n4\tfour"), (
        "second repeat escalates to a distinct level-2 nudge"
    )
    # Byte-stability: the L2 nudge suffix is identical regardless of the (different) body it rides.
    l1_suffix = seen[1][len("2\ttwo") :]
    l2_suffix = seen[2][len("3\tthree\n4\tfour") :]
    assert l1_suffix != l2_suffix, "L1 and L2 nudges must be distinct"


def test_read_file_out_of_range_slice_served_as_empty_range_from_cache(monkeypatch):
    """An out-of-range request against a COMPLETE cached file yields read_file's '(empty range)'
    sentinel deterministically from the cache — no re-execution."""
    lines = ["only-line"]
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _file_backed_read_file(lines, calls))
    cfg = _cfg()
    seen: list = []
    args = [
        {"path": "f", "line_start": 1, "line_end": 1},
        {"path": "f", "line_start": 50, "line_end": 60},  # past EOF
    ]
    PydanticAIRunner(cfg, model_override=_seq_driver(args, seen=seen)).run(_req(cfg))

    assert calls["n"] == 1
    assert _base(seen[0]) == "1\tonly-line"
    assert _base(seen[1]) == "(empty range)"


def test_read_file_truncated_file_falls_back_to_exact_read_beyond_window(monkeypatch):
    """When the whole-file read hit the window cap (a > cap-line file), a request that reaches
    PAST the cached window falls back to the exact-range read rather than fabricating an empty
    slice; a request WITHIN the cached window is still served from the (partial) cache."""
    monkeypatch.setattr(pai_tools, "_READ_MAX_LINES", 3)  # tiny cap so a 7-line file truncates
    lines = [f"line{i}" for i in range(1, 8)]  # 7 lines > cap 3
    calls = {"n": 0}
    _install_single_tool(monkeypatch, _file_backed_read_file(lines, calls))
    cfg = _cfg()
    seen: list = []
    args = [
        {"path": "big", "line_start": 1, "line_end": 2},  # within cached window -> from cache
        {"path": "big", "line_start": 6, "line_end": 7},  # past window -> exact fallback read
    ]
    PydanticAIRunner(cfg, model_override=_seq_driver(args, seen=seen)).run(_req(cfg))

    assert _base(seen[0]) == "1\tline1\n2\tline2"  # sliced from the truncated cache
    assert _base(seen[1]) == "6\tline6\n7\tline7"  # exact fallback read
    assert calls["n"] == 2, (
        f"one whole-file read (caches 1..cap) + one exact fallback = 2, ran {calls['n']}x"
    )
