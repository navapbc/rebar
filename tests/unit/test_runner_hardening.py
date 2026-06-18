"""WS-D4: agent-runtime hardening — structured-output retry/repair + exact pins."""

from __future__ import annotations

from pathlib import Path

import tomllib

from rebar.llm import runner
from rebar.llm.config import LLMConfig
from rebar.llm.runner import RunRequest


class _FakeAgent:
    """A stub create_agent: returns queued outcomes, counts invocations."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.last_input = None

    def invoke(self, inp, config=None):
        self.calls += 1
        self.last_input = inp
        return self._outcomes.pop(0)


def _req(mode="findings"):
    return RunRequest(system_prompt="s", instructions="do it", config=LLMConfig(), mode=mode)


def test_retries_once_on_empty_structured_response() -> None:
    agent = _FakeAgent(
        [
            {"structured_response": None, "messages": []},  # parsed-is-None
            {"structured_response": {"findings": []}, "messages": []},  # repaired
        ]
    )
    outcome, _ = runner._invoke_structured(agent, LLMConfig(), _req())
    assert agent.calls == 2  # retried once
    assert outcome["structured_response"] == {"findings": []}
    # The repair nudge was appended to the retried instructions.
    assert "structured" in str(agent.last_input).lower()


def test_no_retry_when_structured_present() -> None:
    agent = _FakeAgent([{"structured_response": {"findings": []}, "messages": []}])
    runner._invoke_structured(agent, LLMConfig(), _req())
    assert agent.calls == 1


def test_text_mode_never_retries() -> None:
    # text mode needs no structured output, so an absent structured_response is fine.
    agent = _FakeAgent([{"structured_response": None, "messages": []}])
    runner._invoke_structured(agent, LLMConfig(), _req(mode="text"))
    assert agent.calls == 1


def test_langgraph_stack_is_exact_pinned() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    agents = data["project"]["optional-dependencies"]["agents"]
    assert "langgraph==1.2.5" in agents
    assert "langgraph-prebuilt==1.1.0" in agents
    assert "langgraph-checkpoint==4.1.1" in agents
