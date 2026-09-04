"""``run()`` guards the provider session with a context manager (task a49e, ADR 0056 decision 2).

THE DEFECT, measured at c1fc3cee4 before this ticket:

    runner.py:294   provider_session = ProviderSession(cfg)   # builders open httpx clients
    runner.py:445   try:                                       # guard starts 151 lines later
    runner.py:501       provider_session.close()

Lines 294-444 sit OUTSIDE the guard and code in that window raises BY DESIGN
(``_check_tool_capability`` at :337). Anything raising there leaks an opened
``httpx.AsyncClient``. ``ProviderSession.__enter__``/``__exit__`` already exist at
providers.py:323/326 and were never used, while providers.py:18-24 documents a
``with ProviderSession(cfg) as session:`` caller that did not exist.

These tests assert the OBSERVABLE lifecycle — that the session is closed on every exit path —
through the real ``PydanticAIRunner.run()``, not by inspecting how the guard is written.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm import runner as runner_mod
from rebar.llm import structured_run as structured_run_mod
from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

_MODEL = "anthropic:claude-sonnet-4-6"


class _RecordingSession:
    """Stands in for ProviderSession, recording the lifecycle calls run() makes.

    Deliberately implements the SAME surface as the real object (providers.py), including
    ``__enter__``/``__exit__``, so this stub cannot be the reason a `with` works."""

    instances: ClassVar[list[_RecordingSession]] = []

    def __init__(self, _cfg):
        self.closes = 0
        self.entered = 0
        self.exited = 0
        self.close_raises = False
        _RecordingSession.instances.append(self)

    def supports(self, _name):
        return False

    def is_resolvable(self, _name):
        return True

    def close(self):
        self.closes += 1
        if self.close_raises:
            raise RuntimeError("aclose blew up during teardown")

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited += 1
        self.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    _RecordingSession.instances = []
    monkeypatch.setattr(runner_mod, "ProviderSession", _RecordingSession)
    return


def _ok(messages, info):
    return ModelResponse(parts=[TextPart(content="ok")])


def _run(*, boom_in_pre_call: bool = False, monkeypatch=None):
    cfg = LLMConfig(repo_path=".", model=_MODEL)
    if boom_in_pre_call:
        # `build_usage_limits` is called at runner.py:428 — INSIDE the pre-call window
        # (294-444) and after provider construction, so a raise here is exactly the leak
        # this ticket closes.
        def _explode(*_a, **_kw):
            raise RuntimeError("pre-call window failure")

        monkeypatch.setattr(structured_run_mod, "build_usage_limits", _explode)
    return PydanticAIRunner(cfg, model_override=FunctionModel(_ok)).run(
        RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text")
    )


# ── §A happy path ────────────────────────────────────────────────────────────────────────


def test_a_successful_run_closes_the_session_exactly_once():
    """A `with` plus a leftover `finally: provider_session.close()` would double-close, so
    'exactly once' is the assertion that catches a half-applied fix."""
    _run()
    assert len(_RecordingSession.instances) == 1
    assert _RecordingSession.instances[0].closes == 1, (
        f"closed {_RecordingSession.instances[0].closes} times; a leftover finally double-closes"
    )


# ── §B the leak this ticket exists to close ──────────────────────────────────────────────


def test_a_raise_in_the_pre_call_window_still_closes_the_session(monkeypatch):
    """THE DEFECT. Against the pre-a49e code this FAILS: the session is constructed at :294 but
    the try does not start until :445, so a raise at :428 escapes with the client still open."""
    # Pinned to the INJECTED error, not a bare Exception: the pre-call window is outside the
    # try, so the RuntimeError propagates raw. Asserting the specific error is what stops an
    # unrelated early failure from satisfying this test without the leak path ever running.
    with pytest.raises(RuntimeError, match="pre-call window failure"):
        _run(boom_in_pre_call=True, monkeypatch=monkeypatch)
    assert len(_RecordingSession.instances) == 1, "the session should have been constructed"
    assert _RecordingSession.instances[0].closes >= 1, (
        "a raise in the pre-call window leaked the provider session's client"
    )


def test_the_session_is_driven_through_the_context_manager_protocol():
    """`__enter__`/`__exit__` existed at providers.py:323/326 and were dead code; the module
    docstring documented a `with` caller that did not exist. This asserts it now does."""
    _run()
    s = _RecordingSession.instances[0]
    assert s.entered == 1, "run() did not enter the session as a context manager"
    assert s.exited == 1, "run() did not exit the session as a context manager"


def test_a_transport_close_that_raises_is_swallowed_by_the_session():
    """Best-effort teardown is unchanged by this ticket, and the responsibility lives INSIDE
    ``ProviderSession.close()`` (providers.py:309 — 'log, never raise'), not in the caller.

    Asserted on the REAL object: a `with` calls ``__exit__`` -> ``close()``, so if close() ever
    started propagating, the guard this ticket adds would turn a teardown failure into a
    caller-visible error on an otherwise successful run."""
    from rebar.llm.providers import ProviderSession

    class _BadClient:
        async def aclose(self):
            raise RuntimeError("aclose blew up during teardown")

    session = ProviderSession(LLMConfig(repo_path=".", model=_MODEL))
    session._closeables.append(_BadClient())
    with session as entered:  # exercises __enter__/__exit__ -> close()
        assert entered is session
    # Reaching here at all is the assertion: __exit__ swallowed the transport failure.


# ── §C the fail-closed ordering the guard must not disturb ───────────────────────────────


def test_the_gate_refusal_runs_before_any_provider_client_is_built(monkeypatch):
    """`assert_gated("agentic filesystem tools")` at runner.py:261 is a FAIL-CLOSED security
    check and MUST stay ahead of provider construction at :294. If a refactor moved construction
    above it, a refused call would still have opened a client — the exact ordering ADR 0056
    decision 4 pins. Asserted by observation: on refusal, NO session is ever constructed."""
    from rebar.llm import gate_context
    from rebar.llm.errors import LLMConfigError

    def _refuse(_what):
        raise LLMConfigError("no gate session is active")

    monkeypatch.setattr(gate_context, "assert_gated", _refuse)
    cfg = LLMConfig(repo_path=".", model=_MODEL)
    # Asserting the GATE's own error, not merely "something raised" — otherwise an unrelated
    # early failure would satisfy this test without the refusal path ever running.
    with pytest.raises(LLMConfigError, match="no gate session is active"):
        # model_override=None so the safeguard branch (which calls assert_gated) is taken.
        PydanticAIRunner(cfg).run(
            RunRequest(
                system_prompt="s",
                instructions="i",
                config=cfg,
                reviewers=["v"],
                mode="text",
                execution_mode="agentic",
            )
        )
    assert _RecordingSession.instances == [], (
        "a provider session was constructed despite the fail-closed gate refusing"
    )
