"""Transport-layer LLM retry (story morbid-uncultured-arcticduck, epic jira-reb-687).

The retrying `httpx.AsyncClient` (`AsyncTenacityTransport`, SDK `max_retries=0`) re-sends a
transient blip BELOW the agent loop, so completed tool calls are never re-executed. These
tests drive the real `_build_retrying_anthropic_model` helper with a counting MockTransport
(the `_wrapped_transport` test seam), offline, no billable call.
"""

from __future__ import annotations

import time

import httpx
import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("tenacity")

import pydantic_ai.models
from pydantic_ai import Agent

from rebar.llm.config import (
    DEFAULT_LLM_RETRY_MAX_ATTEMPTS,
    DEFAULT_LLM_RETRY_MAX_WAIT_S,
    LLMConfig,
)
from rebar.llm.errors import LLMConfigError
from rebar.llm.runner import _build_retrying_anthropic_model

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _dummy_anthropic_key(monkeypatch):
    """These tests drive a REAL ``AsyncAnthropic`` client (its HTTP served by a MockTransport,
    never the network) — but the SDK still builds auth headers, so it needs *a* key present or
    it raises ``TypeError: Could not resolve authentication method`` before the MockTransport is
    reached. Local dev has ``ANTHROPIC_API_KEY``; CI does not — so provide a dummy so the mock
    (not a real key) serves the responses. The key is never sent anywhere (no real request)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")


def _ok_body(text: str = "OK") -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _sequence_transport(responses):
    """A MockTransport that returns ``responses[i]`` on the i-th request, holding the last.
    Returns (transport, state) where state['n'] counts requests."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(state["n"], len(responses) - 1)
        state["n"] += 1
        return responses[i]()

    return httpx.MockTransport(handler), state


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


def _run(model) -> object:
    # A MockTransport makes NO real network call, so allow the (fake) model request; the
    # conftest socket guard still blocks any accidental real connect.
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
    try:
        return Agent(model).run_sync("go")
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


# ── The spike: 429(+Retry-After)->200 retried below the SDK, both base_url paths ──
@pytest.mark.parametrize("base_url", [None, "https://api.anthropic.com"])
def test_429_with_retry_after_retries_below_the_sdk(base_url):
    """A 429 with Retry-After:0 then a 200 is retried at the transport, within one agent
    turn, on BOTH the normal (base_url=None) and loopback-bypass paths."""
    transport, state = _sequence_transport(
        [
            lambda: httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "rate_limit_error"}},
            ),
            lambda: httpx.Response(200, json=_ok_body("HEALED")),
        ]
    )
    model, http_client = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=base_url, cfg=_cfg(), _wrapped_transport=transport
    )
    out = _run(model)
    assert state["n"] == 2  # one retry
    assert "HEALED" in str(out.output)


def test_500_without_retry_after_retries_via_exponential_fallback():
    """A 500 with NO Retry-After header is retried via the exponential fallback
    (fallback_strategy=None -> wait_exponential), not zero-wait hammering."""
    transport, state = _sequence_transport(
        [
            lambda: httpx.Response(500, json={"type": "error", "error": {"type": "api_error"}}),
            lambda: httpx.Response(200, json=_ok_body("OK500")),
        ]
    )
    model, _ = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=None, cfg=_cfg(), _wrapped_transport=transport
    )
    t0 = time.monotonic()
    out = _run(model)
    assert state["n"] == 2
    assert "OK500" in str(out.output)
    assert time.monotonic() - t0 >= 0.9  # ~1s exponential backoff, not zero


# ── Non-retriable statuses are NOT retried ────────────────────────────────────
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_non_retriable_statuses_are_not_retried(status):
    transport, state = _sequence_transport(
        [
            lambda: httpx.Response(
                status, json={"type": "error", "error": {"type": "invalid_request_error"}}
            )
        ]
    )
    model, _ = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=None, cfg=_cfg(), _wrapped_transport=transport
    )
    with pytest.raises(Exception):  # noqa: B017 — surfaces as an SDK/model error, not retried
        _run(model)
    assert state["n"] == 1  # a single attempt, no retry


# ── Exhaustion: all attempts fail -> the original exception surfaces ───────────
def test_exhaustion_reraises_after_all_attempts():
    transport, state = _sequence_transport(
        [
            lambda: httpx.Response(
                503,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "overloaded_error"}},
            )
        ]
    )
    model, _ = _build_retrying_anthropic_model(
        "claude-sonnet-4-6",
        base_url=None,
        cfg=_cfg(llm_retry_max_attempts=3),
        _wrapped_transport=transport,
    )
    with pytest.raises(Exception):  # noqa: B017 — the last attempt's error is re-raised (reraise=True)
        _run(model)
    assert state["n"] == 3  # exactly max_attempts attempts, then surfaced


# ── Observability: each retry attempt logs the stable prefix ──────────────────
def test_retry_attempt_is_logged(caplog):
    transport, _ = _sequence_transport(
        [
            lambda: httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "rate_limit_error"}},
            ),
            lambda: httpx.Response(200, json=_ok_body()),
        ]
    )
    model, _ = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=None, cfg=_cfg(), _wrapped_transport=transport
    )
    with caplog.at_level("WARNING", logger="rebar.llm.runner"):
        _run(model)
    assert any("llm transport retry" in r.message for r in caplog.records)


# ── Construction-time guard: fail fast if the SDK is not at max_retries=0 ──────
def test_construction_guard_fails_fast_on_nonzero_sdk_retries(monkeypatch):
    """If injection ever regressed so the SDK client kept its own retries, the guard
    raises LLMConfigError rather than silently downgrading."""
    import anthropic

    class _BadClient:
        """A stub AsyncAnthropic that ignored max_retries=0 (kept its own 2) — the exact
        silent-downgrade the guard must catch."""

        def __init__(self, *args, **kwargs):
            self.max_retries = 2

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _BadClient)
    transport, _ = _sequence_transport([lambda: httpx.Response(200, json=_ok_body())])
    with pytest.raises(LLMConfigError, match="max_retries"):
        _build_retrying_anthropic_model(
            "claude-sonnet-4-6", base_url=None, cfg=_cfg(), _wrapped_transport=transport
        )


# ── Config: the two LLMConfig keys + env override ─────────────────────────────
def test_llm_config_retry_defaults():
    cfg = LLMConfig(repo_path=".")
    assert cfg.llm_retry_max_attempts == DEFAULT_LLM_RETRY_MAX_ATTEMPTS == 4
    assert cfg.llm_retry_max_wait_s == DEFAULT_LLM_RETRY_MAX_WAIT_S == 60


def test_llm_config_retry_env_override(monkeypatch):
    monkeypatch.setenv("REBAR_LLM_RETRY_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("REBAR_LLM_RETRY_MAX_WAIT_S", "120")
    cfg = LLMConfig.from_env(repo_root=".")
    assert cfg.llm_retry_max_attempts == 7
    assert cfg.llm_retry_max_wait_s == 120


def test_attempts_one_disables_retry_failfast():
    """The back-out: llm_retry_max_attempts=1 makes a single attempt (no retry)."""
    transport, state = _sequence_transport(
        [
            lambda: httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "rate_limit_error"}},
            )
        ]
    )
    model, _ = _build_retrying_anthropic_model(
        "claude-sonnet-4-6",
        base_url=None,
        cfg=_cfg(llm_retry_max_attempts=1),
        _wrapped_transport=transport,
    )
    with pytest.raises(Exception):  # noqa: B017
        _run(model)
    assert state["n"] == 1  # no retry


# ── Comment idempotency: a mid-run 429->200 self-heals with NO duplicate comment ──
def _tool_use_body(tool_name: str, tool_id: str = "toolu_1") -> dict:
    """An Anthropic ``tool_use`` turn: the model asks to call ``tool_name`` exactly once.
    ``stop_reason='tool_use'`` drives the agent loop to execute the tool locally and send a
    follow-up turn carrying the ``tool_result``."""
    return {
        "id": "msg_tool",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [
            {
                "type": "tool_use",
                "id": tool_id,
                "name": tool_name,
                "input": {"ticket_id": "t-1", "body": "done"},
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_midrun_429_self_heals_without_duplicate_comment():
    """AC (story morbid-uncultured-arcticduck): a MID-RUN transient — a 429 injected on the
    HTTP request that FOLLOWS a completed comment tool call, then a 200 — self-heals and
    produces NO DUPLICATE comment.

    Mechanism it pins: retry is owned SOLELY by the httpx ``AsyncTenacityTransport``, which
    sits BELOW pydantic-ai's agent tool loop (``_build_retrying_anthropic_model``,
    ``src/rebar/llm/runner.py`` lines 752-753 + 774-792: "a transient blip is re-sent BELOW
    the agent loop, so completed tool calls are never re-executed"). Re-sending ONE HTTP
    request never re-enters the loop, so an already-executed tool (the ``comment_ticket``
    write, ``src/rebar/llm/pai_tools.py`` line 166) cannot fire a second time.

    Observable asserted (not merely restated): the comment tool callback is invoked EXACTLY
    ONCE even though the follow-up turn is put on the wire TWICE (429 then 200) — i.e. the
    transport genuinely retried the HTTP request, yet the side-effect did not duplicate."""
    comment_calls = {"n": 0}
    sends = {"total": 0, "followup": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        sends["total"] += 1
        posted_already = b"tool_result" in request.content
        if not posted_already:
            # Turn 1: the model asks to call the comment tool (side-effect not yet run).
            return httpx.Response(200, json=_tool_use_body("post_comment"))
        # Turn 2 carries the tool_result — the comment has already been posted once by the
        # agent loop. Inject a transient 429 on the FIRST send of this turn, heal on retry.
        sends["followup"] += 1
        if sends["followup"] == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "rate_limit_error"}},
            )
        return httpx.Response(200, json=_ok_body("HEALED"))

    transport = httpx.MockTransport(handler)
    model, _ = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=None, cfg=_cfg(), _wrapped_transport=transport
    )

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
    try:
        agent = Agent(model)

        @agent.tool_plain
        def post_comment(ticket_id: str, body: str) -> str:
            # Stand-in for pai_tools.comment_ticket — the ONLY write the gate agent makes.
            comment_calls["n"] += 1
            return f"Commented on {ticket_id}."

        out = agent.run_sync("go")
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    # The transport REALLY retried: the follow-up turn hit the wire twice (429, then 200).
    assert sends["followup"] == 2
    assert sends["total"] == 3  # turn-1 tool_use + turn-2 (429 then 200)
    # ...yet the comment side-effect fired EXACTLY ONCE — no duplicate comment.
    assert comment_calls["n"] == 1
    assert "HEALED" in str(out.output)
