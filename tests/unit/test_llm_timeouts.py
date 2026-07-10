"""Activity-based liveness: per-request read timeout + per-tool timeout (story
chief-contained-hoopoe, epic jira-reb-687). Offline, no billable call.

The per-request READ timeout reuses ``cfg.timeout_s`` and is set as an ``httpx.Timeout`` on
arcticduck's shared client (authoritative on the anthropic path). The per-TOOL timeout
(``Agent(tool_timeout=cfg.llm_tool_timeout_s)``) bounds an ASYNC/MCP tool — verified here to
cancel one — while a SYNC in-process tool is NOT interrupted (async cancel can't stop a
blocking call); the sync caveat is pinned so the scope is honest. Step caps (arawana) bound
runaway loops. No total-runtime timeout and no new event loop are introduced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm.config import DEFAULT_LLM_TOOL_TIMEOUT_S, LLMConfig
from rebar.llm.runner import _build_retrying_anthropic_model

pytestmark = pytest.mark.unit


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


@pytest.fixture
def _dummy_anthropic_key(monkeypatch):
    """The read-timeout PROBES below drive a REAL ``AsyncAnthropic`` client (its HTTP served by
    a localhost socket, never the public network), but the SDK builds auth headers at request
    time and raises ``TypeError: Could not resolve authentication method`` without *a* key. CI
    has none, so provide a dummy; it is never sent anywhere real (the base_url is a loopback
    stub). Mirrors test_transport_retry.py's fixture."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")


def _exc_chain(exc: BaseException) -> Iterator[BaseException]:
    """Walk an exception's ``__cause__``/``__context__`` chain (cycle-safe)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


# ── Per-request read timeout: wired onto arcticduck's shared client ───────────
def test_helper_uses_the_supplied_http_timeout():
    """hoopoe passes an httpx.Timeout(read=cfg.timeout_s, ...) into arcticduck's helper;
    the constructed client carries exactly that timeout."""
    t = httpx.Timeout(read=123.0, connect=10.0, write=30.0, pool=10.0)
    _model, http_client = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=None, cfg=_cfg(), http_timeout=t
    )
    assert http_client.timeout.read == 123.0
    assert http_client.timeout.connect == 10.0


def test_helper_default_timeout_falls_back_to_cfg_timeout_s():
    """Absent an explicit http_timeout, the client is still bounded (never unbounded) —
    the default derives from cfg.timeout_s."""
    _model, http_client = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=None, cfg=_cfg(timeout_s=321)
    )
    assert http_client.timeout.read == 321.0


# ── Read-timeout PROBES: cross the mechanism against a REAL localhost socket ───
# The value tests above only assert the timeout is STORED on the client. These two probes fire
# the mechanism end-to-end: an ``AnthropicModel`` built by the real helper runs under
# ``agent.run_sync()`` against a REAL loopback socket, so httpx's real transport enforces the
# ``read`` timeout on an actual socket read. A ``MockTransport`` cannot exercise this — it
# bypasses the socket layer, so a sleeping mock handler NEVER trips ``httpx.ReadTimeout`` (it
# just returns late). Only a real (localhost-only) socket genuinely crosses the read path;
# hence ``@pytest.mark.allow_network`` (no public network — the server binds 127.0.0.1).


@contextlib.contextmanager
def _local_server(handle) -> Iterator[int]:
    """Run a raw TCP server on 127.0.0.1 that dispatches each accepted connection to
    ``handle(conn)`` in a daemon thread. Yields the bound port; tears the listener down on exit
    (the socket guard is bypassed via ``allow_network``; nothing leaves the loopback)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def _accept_loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return  # listener closed on teardown
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=_accept_loop, daemon=True).start()
    try:
        yield port
    finally:
        srv.close()


def _anthropic_ok_response(text: str) -> bytes:
    body = json.dumps(
        {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    ).encode()
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
    ) + body


@pytest.mark.allow_network
def test_stalled_server_trips_read_timeout_under_run_sync(_dummy_anthropic_key):
    """PROBE 1 — the mechanism ABORTS a stalled request. A server that accepts the connection
    then never replies leaves the socket read hanging; with ``read=0.2`` the run aborts fast and
    ``httpx.ReadTimeout`` is genuinely raised (surfaced wrapped as an SDK/pydantic-ai error, but
    present in the cause chain). Retries are disabled (attempts=1) so it fires once."""

    def _stall(conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.recv(65536)  # read the request, then hang — never send a response
            time.sleep(5.0)
        conn.close()

    with _local_server(_stall) as port:
        cfg = _cfg(timeout_s=1, llm_retry_max_attempts=1)  # no retry -> one read-timeout fire
        http_timeout = httpx.Timeout(read=0.2, connect=5.0, write=5.0, pool=5.0)
        model, http_client = _build_retrying_anthropic_model(
            "claude-sonnet-4-6",
            base_url=f"http://127.0.0.1:{port}",
            cfg=cfg,
            http_timeout=http_timeout,
        )
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
        t0 = time.monotonic()
        try:
            with pytest.raises(BaseException) as exc_info:  # noqa: PT011,B017 — chain asserted below
                Agent(model).run_sync("go")
            elapsed = time.monotonic() - t0
        finally:
            pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
            asyncio.run(http_client.aclose())

    # httpx's real read timeout fired (not the 5s connect timeout, not the server's 5s sleep).
    # The raised ReadTimeout is the PRIMARY proof; elapsed is a loose sanity bound that it
    # aborted BEFORE the server's 5s stall (generous headroom for slow/loaded CI runners —
    # connection + SDK overhead, not the read window, dominates wall-time).
    assert any(isinstance(e, httpx.ReadTimeout) for e in _exc_chain(exc_info.value))
    assert elapsed < 4.5  # aborted at ~read timeout, well before the 5s stall


@pytest.mark.allow_network
def test_slow_but_alive_server_completes_under_read_timeout(_dummy_anthropic_key):
    """PROBE 2 — a HEALTHY slow run is NOT aborted. A server that replies JUST UNDER the read
    timeout (responds after ~0.15s beneath a 0.6s ``read``) completes normally: the read timeout
    bounds a STALLED request, not a slow-but-alive one. Same real socket + real helper as
    PROBE 1, so it exercises the same mechanism from the passing side."""

    def _slow_ok(conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.recv(65536)
            time.sleep(0.15)  # slow, but < the 0.6s read timeout -> alive, must NOT abort
            conn.sendall(_anthropic_ok_response("ALIVE"))
        conn.close()

    with _local_server(_slow_ok) as port:
        cfg = _cfg(timeout_s=1, llm_retry_max_attempts=1)
        http_timeout = httpx.Timeout(read=0.6, connect=5.0, write=5.0, pool=5.0)
        model, http_client = _build_retrying_anthropic_model(
            "claude-sonnet-4-6",
            base_url=f"http://127.0.0.1:{port}",
            cfg=cfg,
            http_timeout=http_timeout,
        )
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
        t0 = time.monotonic()
        try:
            result = Agent(model).run_sync("go")
            elapsed = time.monotonic() - t0
        finally:
            pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
            asyncio.run(http_client.aclose())

    # The slow-but-alive run COMPLETED (not aborted by the read timeout) — that is the whole
    # point of the probe, and the meaningful assertion. NOT a wall-clock bound: the 0.6s `read`
    # timeout is PER-READ (the server answers each read within 0.15s), it does not bound total
    # wall-time, which is dominated by connection + SDK + agent-loop overhead (~2s on loaded CI).
    assert "ALIVE" in str(result.output)
    assert elapsed < 20  # generous hang-guard only (a stuck run would blow this)


# ── Per-tool timeout: cancels an ASYNC tool; a SYNC tool is NOT interrupted ────
def _tool_calling_model():
    state = {"n": 0}

    def gen(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="slow", args={})])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(gen)


def test_tool_timeout_cancels_an_async_tool():
    """A hung ASYNC tool is cancelled at ~tool_timeout (bounded liveness); the run
    continues (a soft tool error goes back to the model — no exception raised)."""
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
    try:
        agent = Agent(_tool_calling_model(), tool_timeout=0.3)

        @agent.tool_plain
        async def slow() -> str:
            await asyncio.sleep(5.0)
            return "never"

        t0 = time.monotonic()
        result = agent.run_sync("go")
        elapsed = time.monotonic() - t0
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    assert elapsed < 2.0  # cancelled well before the 5s sleep
    assert "done" in str(result.output)  # the run recovered, not aborted


def test_sync_tool_is_not_interrupted_documented_caveat():
    """The honest caveat: async cancellation cannot interrupt a SYNC blocking tool, so
    tool_timeout is a no-op for rebar's sync in-process tools (bounded instead by step
    caps). Pinned with a SHORT sync sleep so the scope claim reflects reality."""
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
    try:
        agent = Agent(_tool_calling_model(), tool_timeout=0.1)

        @agent.tool_plain
        def slow() -> str:
            time.sleep(0.6)  # short, but > tool_timeout — a SYNC blocking call
            return "finished"

        t0 = time.monotonic()
        agent.run_sync("go")
        elapsed = time.monotonic() - t0
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    assert elapsed >= 0.6  # NOT cancelled — waited the full sync sleep (the caveat)


# ── Config ────────────────────────────────────────────────────────────────────
def test_tool_timeout_config_default():
    assert LLMConfig(repo_path=".").llm_tool_timeout_s == DEFAULT_LLM_TOOL_TIMEOUT_S == 120


def test_tool_timeout_config_env_override(monkeypatch):
    monkeypatch.setenv("REBAR_LLM_TOOL_TIMEOUT_S", "45")
    assert LLMConfig.from_env(repo_root=".").llm_tool_timeout_s == 45


# ── The runner wires tool_timeout onto the Agent (via a spy) ──────────────────
def test_runner_sets_tool_timeout_on_the_agent(monkeypatch):
    """A model_override run still builds the Agent with tool_timeout in its kwargs — the
    liveness bound is applied on every agentic construction."""
    import rebar.llm.runner as runner_mod
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    captured: dict = {}
    real_import = runner_mod._import_pydantic_ai

    def _spy_import():
        RealAgent = real_import()

        class _SpyAgent(RealAgent):  # type: ignore[misc,valid-type]
            def __init__(self, *args, **kwargs):
                captured["tool_timeout"] = kwargs.get("tool_timeout")
                super().__init__(*args, **kwargs)

        return _SpyAgent

    monkeypatch.setattr(runner_mod, "_import_pydantic_ai", _spy_import)
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("hi")])

    cfg = _cfg(llm_tool_timeout_s=77)
    req = RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text")
    PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)
    assert captured["tool_timeout"] == 77.0


# ── No total-runtime timeout / no total-runtime timer in the gate path ────────
def test_no_total_runtime_timeout_mechanism():
    """Structural guard: the runner introduces NO total-runtime timer (no signal.alarm,
    no wall-clock deadline thread) — liveness is per-request + per-tool + step caps only.
    The single asyncio.run is the client-teardown aclose, not a run-bounding loop."""
    import inspect

    import rebar.llm.runner as runner_mod

    src = inspect.getsource(runner_mod)
    assert "signal.alarm" not in src  # no SIGALRM wall-clock kill
    assert "Timer(" not in src  # no threading.Timer wall-clock deadline
    # The only asyncio.run CALL is the client-teardown aclose (story arcticduck), not a
    # run-bounding loop.
    assert "asyncio.run(_http_client.aclose())" in src
