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

from rebar.llm import structured_run as structured_run_mod

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm.anthropic_model import _build_retrying_anthropic_model
from rebar.llm.config import DEFAULT_LLM_TOOL_TIMEOUT_S, LLMConfig

pytestmark = pytest.mark.unit


def _anthropic_expects_httpx2_client() -> bool:
    import inspect

    import anthropic

    http_client = inspect.signature(anthropic.AsyncAnthropic.__init__).parameters["http_client"]
    return "httpx2.AsyncClient" in str(http_client.annotation)


def _transport_http_module():
    if _anthropic_expects_httpx2_client():
        return pytest.importorskip("httpx2")
    return httpx


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
            with pytest.raises(BaseException) as exc_info:
                Agent(model).run_sync("go")
            elapsed = time.monotonic() - t0
        finally:
            pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
            asyncio.run(http_client.aclose())

    # httpx's real read timeout fired (not the 5s connect timeout, not the server's 5s sleep).
    # The raised ReadTimeout is the PRIMARY proof; elapsed is a loose sanity bound that it
    # aborted BEFORE the server's 5s stall (generous headroom for slow/loaded CI runners —
    # connection + SDK overhead, not the read window, dominates wall-time).
    assert any(
        isinstance(e, _transport_http_module().ReadTimeout) for e in _exc_chain(exc_info.value)
    )
    # timing: hang-guard — abort-before-stall proof; the 5s server stall is the failure mode
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
    # timing: hang-guard — stuck-run guard; 20s dwarfs the ~1s happy path
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
    # timing: hang-guard — cancellation proof; the 5s sleep is the failure mode
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
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    captured: dict = {}
    real_import = structured_run_mod._import_pydantic_ai

    def _spy_import():
        RealAgent = real_import()

        class _SpyAgent(RealAgent):  # type: ignore[misc,valid-type]
            def __init__(self, *args, **kwargs):
                captured["tool_timeout"] = kwargs.get("tool_timeout")
                super().__init__(*args, **kwargs)

        return _SpyAgent

    monkeypatch.setattr(structured_run_mod, "_import_pydantic_ai", _spy_import)
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("hi")])

    cfg = _cfg(llm_tool_timeout_s=77)
    req = RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text")
    PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)
    assert captured["tool_timeout"] == 77.0


# ── No total-runtime timer in the gate path (SUPPLEMENTAL structural guard) ────
_WALL_CLOCK_PRIMITIVES = ("signal.alarm", "Timer(")


def _runner_scanned_sources() -> dict[str, str]:
    """The exact source the wall-clock guard below inspects, as ``{module name: source}``.

    Named and separated so the guard's POPULATION is itself assertable.

    NON-VACUITY (bug 8a5e, same rot class as bug 34c2). This guard used to read exactly one
    module — ``inspect.getsource(rebar.llm.runner)``. The run path has since been split, and
    the timeout-bearing code moved into siblings (``structured_run``, ``agent_call``), so the
    guard was left reading a module that names ``timeout`` once and could not have caught a
    new wall-clock primitive landing in the half it no longer saw.

    The repair is the one proven on bug 34c2: derive the population rather than pin it. Walk
    the runner's intra-``rebar.llm`` imports transitively, so every module the run path was
    split into is scanned. A relocation cannot orphan this — the runner must import whatever
    it delegates to.
    """
    import ast
    import pathlib

    import rebar.llm.runner as runner_mod

    pkg = pathlib.Path(runner_mod.__file__).parent
    sources: dict[str, str] = {}
    queue = ["runner"]
    while queue:
        name = queue.pop()
        if name in sources:
            continue
        path = pkg / f"{name}.py"
        if not path.is_file():
            continue
        sources[name] = path.read_text()
        for node in ast.walk(ast.parse(sources[name])):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rebar.llm"):
                tail = (node.module or "").rsplit(".", 1)[-1]
                queue.append(tail)
                queue.extend(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                queue.extend(
                    a.name.rsplit(".", 1)[-1] for a in node.names if a.name.startswith("rebar.llm")
                )
    return sources


def test_no_total_runtime_timer_mechanism():
    """SUPPLEMENTAL structural guard — NOT counted as behavioral liveness coverage. The
    runner's whole run path introduces no total-runtime wall-clock KILL primitive (no
    ``signal.alarm`` SIGALRM, no ``threading.Timer`` deadline). The actual liveness contract
    (per-request read timeout, per-tool timeout, step caps) is exercised behaviorally by the
    read-timeout / tool-timeout probe tests elsewhere in this module; this guard only pins
    the negative-space invariant that no NEW wall-clock timer primitive has crept in."""
    offenders = [
        f"{name}: {primitive}"
        for name, src in sorted(_runner_scanned_sources().items())
        for primitive in _WALL_CLOCK_PRIMITIVES
        if primitive in src
    ]
    assert not offenders, (
        f"a total-runtime wall-clock kill primitive appeared in the run path: {offenders}. "
        f"Liveness is activity-based here (per-request read timeout, per-tool timeout, step "
        f"caps); a wall-clock kill truncates a healthy long call mid-flight"
    )


def test_the_wall_clock_guard_scans_the_modules_that_hold_the_timeout_machinery():
    """ANTI-VACUITY (bug 8a5e). The guard above can only be meaningful if the source it
    scans is where a wall-clock primitive would plausibly LAND — i.e. the modules that
    actually carry the call's timeout handling. Pinning it to ``runner.py`` alone is what
    hollowed it out: the run-path split left that module naming ``timeout`` once while its
    siblings carried the rest.

    Assert the POPULATION, not just the verdict, so the next split fails the build instead
    of silently disarming the guard.
    """
    sources = _runner_scanned_sources()
    timeout_bearing = sorted(name for name, src in sources.items() if "timeout" in src)
    assert len(timeout_bearing) >= 2, (
        f"the wall-clock guard scans {sorted(sources)}, of which only {timeout_bearing} "
        f"mention a timeout at all. The runner delegates its call machinery to siblings, so "
        f"a single-module timeout surface means the import walk broke and a new wall-clock "
        f"primitive could land unscanned. Re-aim _runner_scanned_sources()."
    )


def test_the_wall_clock_guard_fires_on_a_primitive_outside_the_runner_module():
    """TEETH for the widened scan, driven through the REAL modules rather than a synthetic
    string — a synthetic-source teeth test proves the predicate works but cannot detect the
    guard being aimed at the wrong file, which is exactly how this one survived the split.

    Plant each banned primitive in a scanned module OTHER than ``runner`` and require the
    guard to report it against that module.
    """
    sources = _runner_scanned_sources()
    others = sorted(name for name in sources if name != "runner")
    assert others, "precondition: the runner delegates to at least one sibling module"
    victim = others[0]

    for primitive in _WALL_CLOCK_PRIMITIVES:
        mutated = dict(sources)
        mutated[victim] = f"x = {primitive}30)\n" + mutated[victim]
        offenders = [
            f"{name}: {p}"
            for name, src in sorted(mutated.items())
            for p in _WALL_CLOCK_PRIMITIVES
            if p in src
        ]
        assert any(o.startswith(f"{victim}: ") for o in offenders), (
            f"a {primitive!r} planted in {victim}.py went unreported (offenders: "
            f"{offenders!r}) — the guard is still effectively aimed at runner.py alone"
        )
