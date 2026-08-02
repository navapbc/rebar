"""``build_agent_kwargs`` + the post-call telemetry pair — the last of ADR 0056's four named
extractions, lifted out of ``PydanticAIRunner.run()``.

A relocation has no new behaviour, so the real regression net is the existing suite passing
UNCHANGED. What CAN be asserted here is the structure and the contracts the move has to
preserve: the kwargs dict is byte-shaped the same, the ``tool_step_limit`` convergence
boundary still rewrites tools into a filtered toolset, the optional keys stay ABSENT rather
than ``None``, the new module clears the anti-fragmentation floor, and it is a genuine leaf.

Deliberately NOT asserted: any ceiling on ``runner.py``'s own line count.
``.github/module-size-limit.txt`` is the single authoritative limit (ADR 0058), and the
module-size policy forbids splitting to hit a number.
"""

from __future__ import annotations

import ast
import json
import logging
import pathlib

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.agent_call import build_agent_kwargs, log_call_success, record_call_spend
from rebar.llm.config import LLMConfig
from rebar.llm.runner import RunRequest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "rebar" / "llm"
_RUNNER = _SRC / "runner.py"
_AGENT_CALL = _SRC / "agent_call.py"

# AGENTS.md: never create a file under 100 LOC by splitting.
_SPLIT_FLOOR = 100


def _loc(path: pathlib.Path) -> int:
    return len(path.read_text().splitlines())


def _cfg(**kw) -> LLMConfig:
    return LLMConfig(repo_path=".", **kw)


def _req(cfg: LLMConfig, **kw) -> RunRequest:
    return RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], **kw)


# ── §A happy path: the kwargs dict the Agent constructor receives ────────────────────────


def test_kwargs_carry_the_prompt_the_tools_and_the_tool_timeout():
    """With no step limit the tools/toolsets pass through untouched and the per-tool liveness
    bound is the configured float."""
    cfg = _cfg(llm_tool_timeout_s=77)
    tool = object()
    out = build_agent_kwargs(cfg, _req(cfg), [tool], [], model_settings=None, web_caps=None)
    assert out == {
        "system_prompt": "s",
        "tools": [tool],
        "toolsets": [],
        "tool_timeout": 77.0,
    }


def test_model_settings_and_capabilities_ride_along_when_present():
    cfg = _cfg()
    out = build_agent_kwargs(
        cfg, _req(cfg), [], [], model_settings={"max_tokens": 8000}, web_caps=["ws"]
    )
    assert out["model_settings"] == {"max_tokens": 8000}
    assert out["capabilities"] == ["ws"]


# ── §B edges ─────────────────────────────────────────────────────────────────────────────


def test_optional_keys_are_absent_not_none():
    """A non-anthropic / no-settings request must stay BYTE-IDENTICAL to the pre-capability
    era: the keys are omitted, never present-and-None (a ``None`` would reach the provider)."""
    cfg = _cfg()
    out = build_agent_kwargs(cfg, _req(cfg), [], [], model_settings={}, web_caps=None)
    assert "capabilities" not in out
    assert "model_settings" not in out, "a falsy model_settings must not be sent"


def test_tool_step_limit_rewrites_tools_into_a_filtered_toolset():
    """The executable convergence boundary: past the limit the tools stop being offered.
    Losing this rewrite would silently un-bound a tool loop, so it is asserted on the
    filter's own predicate rather than only on the shape."""
    from pydantic_ai.toolsets import FunctionToolset

    def a_tool() -> str:
        return "x"

    cfg = _cfg()
    out = build_agent_kwargs(
        cfg,
        _req(cfg, tool_step_limit=3),
        [a_tool],
        [FunctionToolset([])],
        model_settings=None,
        web_caps=None,
    )
    assert out["tools"] == [], "the tools must move into the filtered toolsets"
    assert len(out["toolsets"]) == 2, "the function tools plus the pre-existing toolset"

    class _Ctx:
        def __init__(self, step):
            self.run_step = step

    inner = out["toolsets"][0]
    assert inner.filter_func(_Ctx(3), None) is True, "at the limit the tool is still offered"
    assert inner.filter_func(_Ctx(4), None) is False, "past the limit it is withheld"


def test_a_negative_step_limit_clamps_to_zero_rather_than_inverting():
    cfg = _cfg()

    def a_tool() -> str:
        return "x"

    out = build_agent_kwargs(
        cfg, _req(cfg, tool_step_limit=-5), [a_tool], [], model_settings=None, web_caps=None
    )

    class _Ctx:
        run_step = 1

    assert out["toolsets"][0].filter_func(_Ctx(), None) is False


def test_step_limit_without_tools_is_a_no_op():
    """single_turn passes ``tools=[]``; there is nothing to bound and no toolset to wrap."""
    cfg = _cfg()
    out = build_agent_kwargs(
        cfg, _req(cfg, tool_step_limit=3), [], [], model_settings=None, web_caps=None
    )
    assert out["tools"] == []
    assert out["toolsets"] == []


def test_success_telemetry_survives_an_empty_usage_dict(caplog):
    """``usage`` is ``{}`` whenever the call produced no extractable usage — the telemetry
    line must still emit with zeroed counters and must never raise into the success path."""
    with caplog.at_level(logging.INFO):
        log_call_success(
            {},
            call_label="plan-reviewer",
            execution_mode="agentic",
            ran_model="anthropic:claude-sonnet-4-6",
            req_limit=12,
            eff_max_iter=24,
            started_at=0.0,
        )
    line = next(r.getMessage() for r in caplog.records if "llm call [" in r.getMessage())
    assert "[plan-reviewer]" in line
    assert "steps=0/12 budget=24" in line
    assert "(in=0 out=0 cache_read=0 cache_write=0)" in line


def test_spend_record_keeps_the_op_model_and_derived_provider_wiring(monkeypatch, tmp_path):
    """The row must still carry the call label as ``op`` and the provider DERIVED from the
    model that actually ran — the wiring most easily lost in a move, and invisible until a
    billing report is wrong."""
    sink = tmp_path / "usage.jsonl"
    monkeypatch.setenv("REBAR_USAGE_LOG", str(sink))
    record_call_spend(
        {"input_tokens": 5, "output_tokens": 2},
        call_label="plan-reviewer",
        ran_model="anthropic:claude-sonnet-4-6",
    )
    row = json.loads(sink.read_text().splitlines()[0])
    assert row["op"] == "plan-reviewer"
    assert row["model"] == "anthropic:claude-sonnet-4-6"
    assert row["provider"] == "anthropic"
    assert row["input_tokens"] == 5


def test_spend_record_is_a_no_op_without_the_opt_in(monkeypatch, tmp_path):
    """The sink is opt-in; unset, recording must write nothing and must not raise (it runs on
    EVERY successful call)."""
    monkeypatch.delenv("REBAR_USAGE_LOG", raising=False)
    monkeypatch.chdir(tmp_path)
    record_call_spend({"input_tokens": 5}, call_label="op", ran_model="anthropic:x")
    assert list(tmp_path.iterdir()) == []


# ── §C structural: the size policy and the leaf rule ─────────────────────────────────────


def test_agent_call_clears_the_split_floor():
    """AGENTS.md forbids creating a sub-100-LOC file by splitting — a split that small is a
    sign the seam was mechanical rather than real."""
    loc = _loc(_AGENT_CALL)
    assert _SPLIT_FLOOR <= loc < 500, (
        f"agent_call.py is {loc} LOC; expected >= {_SPLIT_FLOOR} (the anti-fragmentation "
        "floor) and < 500 (the AGENTS.md target ceiling)"
    )


def test_agent_call_has_no_runtime_import_from_runner():
    """The leaf rule, asserted on the AST rather than on a grep. A ``TYPE_CHECKING``-guarded
    import of ``RunRequest`` is permitted — it is a string annotation at runtime and creates
    no cycle. A module-level runtime import of ``runner`` is not."""
    tree = ast.parse(_AGENT_CALL.read_text())

    def _guarded_by_type_checking(node: ast.AST) -> bool:
        for parent in ast.walk(tree):
            if isinstance(parent, ast.If):
                test = parent.test
                name = getattr(test, "id", None) or getattr(test, "attr", None)
                if name == "TYPE_CHECKING" and node in ast.walk(parent):
                    return True
        return False

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("llm.runner"):
            if not _guarded_by_type_checking(node):
                offenders.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("llm.runner") and not _guarded_by_type_checking(node):
                    offenders.append(f"line {node.lineno}: import {alias.name}")

    assert not offenders, (
        "agent_call.py must not import `runner` at RUNTIME (a TYPE_CHECKING-guarded "
        f"annotation import is fine): {offenders}"
    )


def test_runner_no_longer_builds_the_kwargs_dict_inline():
    """The extraction is real, not a copy: the literal the four fragments used to write is
    gone from run(), so a future edit cannot quietly reintroduce a second assembly site."""
    src = _RUNNER.read_text()
    assert "kwargs: dict[str, Any] = {" not in src
    assert "build_agent_kwargs(" in src
    assert "log_call_success(" in src
    assert "record_call_spend(" in src
