"""S1 — optional Streamable-HTTP transport for rebar-mcp + transport hardening.

Observable-behavior tests for the HTTP transport: config defaults + flat keys, a
baseline MCP request over the SDK's Streamable-HTTP ASGI entrypoint, the
DNS-rebinding / Origin protection with explicit loopback defaults, the two
fail-closed startup gates (non-loopback bind, unauthenticated HTTP), and the
manifest / doc-generator drift for the new env vars.

The transport is driven in-process via Starlette's ``TestClient`` (which speaks
httpx over the ASGI app and runs the app lifespan / session-manager context) —
a unit test cannot cross the Starlette transport/middleware seam.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rebar._config_schema import Config, ConfigError, McpConfig

# A minimal, spec-valid MCP initialize request.
INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    },
}
# Streamable-HTTP requires both JSON + SSE Accept and a JSON content type on POST.
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
REPO_ROOT = Path(__file__).resolve().parents[3]

NEW_ENV_VARS = [
    "REBAR_MCP_TRANSPORT",
    "REBAR_MCP_HTTP_HOST",
    "REBAR_MCP_HTTP_PORT",
    "REBAR_MCP_HTTP_PATH",
    "REBAR_MCP_HTTP_ALLOWED_HOSTS",
    "REBAR_MCP_HTTP_ALLOWED_ORIGINS",
    "REBAR_MCP_HTTP_TLS_AT_EDGE",
    "REBAR_MCP_ALLOW_UNAUTHENTICATED_HTTP",
]


def _http_config(**overrides) -> Config:
    """A Config whose mcp section selects the HTTP transport (auth off, so the
    unauthenticated-HTTP ack is set to permit the auth-off boot)."""
    mcp_kwargs = dict(transport="http", allow_unauthenticated_http=True)
    mcp_kwargs.update(overrides)
    return Config(mcp=McpConfig(**mcp_kwargs))


# ── config defaults + flat keys ──────────────────────────────────────────────
def test_mcp_config_transport_defaults_to_stdio():
    """Unset transport → stdio, and every new http_* key holds its documented
    default (observable: the dataclass field values)."""
    cfg = McpConfig()
    assert cfg.transport == "stdio"
    assert cfg.http_host == "127.0.0.1"
    assert cfg.http_port == 8000
    assert cfg.http_path == "/mcp"
    assert tuple(cfg.http_allowed_hosts) == ()
    assert tuple(cfg.http_allowed_origins) == ()
    assert cfg.http_tls_at_edge is False
    assert cfg.allow_unauthenticated_http is False


def test_mcp_http_keys_parse_from_toml_mapping():
    """The flat http_* keys coerce from a [tool.rebar.mcp] TOML mapping
    (observable: resolved Config field values, incl. comma-split lists)."""
    raw = {
        "mcp": {
            "transport": "http",
            "http_host": "0.0.0.0",
            "http_port": 9001,
            "http_path": "/rebar-mcp",
            "http_allowed_hosts": "example.com:443, mcp.example.com:443",
            "http_allowed_origins": "https://example.com",
            "http_tls_at_edge": True,
            "allow_unauthenticated_http": True,
        }
    }
    cfg = Config.from_mapping(raw)
    assert cfg.mcp.transport == "http"
    assert cfg.mcp.http_host == "0.0.0.0"
    assert cfg.mcp.http_port == 9001
    assert cfg.mcp.http_path == "/rebar-mcp"
    assert tuple(cfg.mcp.http_allowed_hosts) == ("example.com:443", "mcp.example.com:443")
    assert tuple(cfg.mcp.http_allowed_origins) == ("https://example.com",)
    assert cfg.mcp.http_tls_at_edge is True
    assert cfg.mcp.allow_unauthenticated_http is True


def test_invalid_transport_value_rejected():
    """transport is a closed choice: an unknown value is a config error
    (observable: from_mapping raises)."""
    with pytest.raises(ConfigError):
        Config.from_mapping({"mcp": {"transport": "grpc"}})


# ── the boot happy path (integration over the real ASGI transport) ───────────
def test_http_transport_boots_and_serves_initialize():
    """transport=http (auth off + unauthenticated ack) boots the Streamable-HTTP
    app and a baseline MCP initialize request succeeds — observable oracle:
    HTTP 200 and a JSON-RPC initialize result in the response body."""
    from starlette.testclient import TestClient

    from rebar.mcp_server import build_server

    server = build_server(_http_config())
    app = server.streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        resp = client.post("/mcp", json=INIT_REQUEST, headers=MCP_HEADERS)
    assert resp.status_code == 200
    assert '"result"' in resp.text
    assert '"protocolVersion"' in resp.text


def test_build_server_default_is_stdio_fastmcp():
    """build_server() with no/stdio config returns a FastMCP with the 'rebar'
    name — the stdio path is unchanged (observable: build succeeds)."""
    from rebar.mcp_server import build_server

    server = build_server()  # no cfg → loads config → stdio default
    assert server.name == "rebar"


# ── DNS-rebinding / Origin protection with explicit loopback defaults ─────────
def test_disallowed_host_421_and_origin_403_loopback_ok():
    """With protection ON and the explicit loopback defaults, a disallowed Host
    → 421 and a disallowed Origin → 403, while an allowed loopback request → 200
    (negative controls + the positive control in one test)."""
    from starlette.testclient import TestClient

    from rebar.mcp_server import build_server

    app = build_server(_http_config()).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        ok = client.post("/mcp", json=INIT_REQUEST, headers=MCP_HEADERS)
        bad_host = client.post(
            "/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "Host": "evil.example.com"}
        )
        bad_origin = client.post(
            "/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "Origin": "http://evil.example.com"}
        )
    assert ok.status_code == 200
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403


# ── fail-closed startup gate: non-loopback bind ──────────────────────────────
def test_nonloopback_refuses_without_allowlists():
    """A non-loopback http_host with both allowlists empty refuses to start."""
    from rebar.mcp_server import McpStartupError, build_server

    cfg = _http_config(http_host="0.0.0.0", http_tls_at_edge=True)
    with pytest.raises(McpStartupError):
        build_server(cfg)


def test_nonloopback_refuses_with_only_one_allowlist():
    """BOTH allowlists are required: supplying only http_allowed_hosts (origins
    empty), even with the TLS ack, still refuses — the discriminating case that a
    weakened ``and`` gate would wrongly admit."""
    from rebar.mcp_server import McpStartupError, build_server

    cfg = _http_config(
        http_host="0.0.0.0",
        http_allowed_hosts=("edge.example.com:443",),
        http_allowed_origins=(),
        http_tls_at_edge=True,
    )
    with pytest.raises(McpStartupError):
        build_server(cfg)


def test_nonloopback_refuses_without_tls_ack():
    """A non-loopback bind with both allowlists but no TLS-at-edge ack refuses."""
    from rebar.mcp_server import McpStartupError, build_server

    cfg = _http_config(
        http_host="0.0.0.0",
        http_allowed_hosts=("edge.example.com:443",),
        http_allowed_origins=("https://edge.example.com",),
        http_tls_at_edge=False,
    )
    with pytest.raises(McpStartupError):
        build_server(cfg)


def test_nonloopback_boots_with_allowlists_and_ack():
    """A non-loopback bind boots when both allowlists AND the TLS ack are set."""
    from rebar.mcp_server import build_server

    cfg = _http_config(
        http_host="0.0.0.0",
        http_allowed_hosts=("edge.example.com:443",),
        http_allowed_origins=("https://edge.example.com",),
        http_tls_at_edge=True,
    )
    server = build_server(cfg)  # must not raise
    assert server.name == "rebar"


# ── fail-closed startup gate: unauthenticated HTTP ───────────────────────────
def test_unauthenticated_http_refuses_without_ack():
    """transport=http with auth off and no unauthenticated-HTTP ack refuses."""
    from rebar.mcp_server import McpStartupError, build_server

    cfg = Config(mcp=McpConfig(transport="http", allow_unauthenticated_http=False))
    with pytest.raises(McpStartupError):
        build_server(cfg)


def test_unauthenticated_http_boots_with_ack():
    """transport=http with the ack set boots (auth-off is permitted)."""
    from rebar.mcp_server import build_server

    cfg = Config(mcp=McpConfig(transport="http", allow_unauthenticated_http=True))
    assert build_server(cfg).name == "rebar"


# ── manifest + generator drift ───────────────────────────────────────────────
def test_mcp_env_vars_include_new_http_keys():
    from rebar.mcp_server import MCP_ENV_VARS

    names = {v["name"] for v in MCP_ENV_VARS}
    for var in NEW_ENV_VARS:
        assert var in names, f"{var} missing from MCP_ENV_VARS"


def test_server_json_advertises_new_keys():
    data = json.loads((REPO_ROOT / "server.json").read_text())
    advertised = {e["name"] for e in data["packages"][0]["environmentVariables"]}
    for var in NEW_ENV_VARS:
        assert var in advertised, f"{var} missing from server.json"


def test_check_server_manifest_passes():
    r = subprocess.run(
        [sys.executable, "scripts/check_server_manifest.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_generators_not_stale_and_include_new_keys():
    """Both regenerated docs contain every new key and their drift gates pass."""
    for script in ("scripts/gen_mcp_reference.py", "scripts/gen_env_registry.py"):
        r = subprocess.run(
            [sys.executable, script, "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert r.returncode == 0, f"{script} --check stale:\n{r.stdout}{r.stderr}"
    ref = (REPO_ROOT / "docs" / "mcp-reference.md").read_text()
    env = (REPO_ROOT / "docs" / "env-vars.md").read_text()
    for var in NEW_ENV_VARS:
        assert var in ref, f"{var} missing from docs/mcp-reference.md"
        assert var in env, f"{var} missing from docs/env-vars.md"


# ── /health, the in-flight gauge, and bounded SIGTERM grace (ADR 0104) ────────
def _write_static_tokens(tmp_path) -> str:
    """Write a minimal valid static-tokens file (one sha256 record) and return its
    path, so an auth-enabled build has a working `static` verifier."""
    import hashlib

    digest = hashlib.sha256(b"secret-token").hexdigest()
    tokens = {
        "tokens": [{"name": "probe", "client_id": "probe", "scopes": [], "token_sha256": digest}]
    }
    path = tmp_path / "mcp-static-tokens.json"
    path.write_text(json.dumps(tokens), encoding="utf-8")
    return str(path)


def _auth_http_config(tmp_path, **overrides) -> Config:
    """A Config selecting the HTTP transport with auth ENABLED (static strategy),
    for the /health auth-exemption oracle. Loopback bind so no TLS/allowlist ack is
    needed; a token verifier is built so the unauthenticated-HTTP gate is satisfied."""
    mcp_kwargs = dict(
        transport="http",
        auth_enabled=True,
        auth_strategies=("static",),
        auth_static_tokens_file=_write_static_tokens(tmp_path),
        auth_issuer_url="https://rebar.example",
        auth_resource_server_url="https://rebar.example/mcp",
    )
    mcp_kwargs.update(overrides)
    return Config(mcp=McpConfig(**mcp_kwargs))


def test_health_reports_in_flight_gauge_idle_inflight_and_contrast():
    """/health returns JSON with an integer in_flight that reads 0 at idle,
    increments while a certified op is held in flight (through the real endpoint),
    returns to 0 after, and does NOT move for a trivial (non-certified) op."""
    from starlette.testclient import TestClient

    from rebar._mcp_health import _GAUGE_ATTR
    from rebar.mcp_server import build_server

    server = build_server(_http_config())
    gauge = getattr(server, _GAUGE_ATTR)
    app = server.streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        idle = client.get("/health").json()
        with gauge.track("review_plan"):
            inflight = client.get("/health").json()
        after = client.get("/health").json()
        with gauge.track("list_tickets"):
            trivial = client.get("/health").json()
    assert idle["in_flight"] == 0
    # /health also reports store reachability now, so a container with no ticket
    # store is distinguishable from a healthy one (mobile-groovy-badger).
    assert "store" in idle and "present" in idle["store"]
    assert isinstance(inflight["in_flight"], int) and inflight["in_flight"] == 1
    assert after["in_flight"] == 0
    assert trivial["in_flight"] == 0  # a trivial read never moves the gauge


def test_health_is_unauthenticated_even_with_auth_enabled(tmp_path):
    """/health returns 200 WITHOUT a bearer while auth is enabled (it is a custom
    route outside RequireAuthMiddleware) — while /mcp still challenges (401)."""
    from starlette.testclient import TestClient

    from rebar.mcp_server import build_server

    app = build_server(_auth_http_config(tmp_path)).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        health = client.get("/health")
        mcp = client.post("/mcp", json=INIT_REQUEST, headers=MCP_HEADERS)
    assert health.status_code == 200
    assert health.json()["in_flight"] == 0
    assert "store" in health.json()
    assert mcp.status_code == 401  # the MCP endpoint still enforces the bearer


def test_certified_tool_instrumentation_moves_the_gauge():
    """instrument_certified_tools wraps a certified tool's fn so running it moves
    the gauge; a non-certified tool's fn is left untouched. Uses a blocking stub so
    the increment is observable while the call is in flight."""
    import threading
    from types import SimpleNamespace

    from rebar._mcp_health import InFlightGauge, instrument_certified_tools

    started = threading.Event()
    release = threading.Event()

    def _blocking_review_plan(*_a, **_k):
        started.set()
        release.wait(timeout=5)
        return "done"

    def _trivial(*_a, **_k):
        return "ok"

    certified = SimpleNamespace(fn=_blocking_review_plan, is_async=False)
    trivial = SimpleNamespace(fn=_trivial, is_async=False)
    async_certified_fn = object()
    async_certified = SimpleNamespace(fn=async_certified_fn, is_async=True)
    tools = {
        "review_plan": certified,
        "list_tickets": trivial,
        # verify_completion is a certified name but marked async → must be left alone;
        # review_code / scan_spec are absent (get_tool → None) → skipped, not an error.
        "verify_completion": async_certified,
    }
    manager = SimpleNamespace(get_tool=lambda n: tools.get(n))
    mcp = SimpleNamespace(_tool_manager=manager)

    gauge = InFlightGauge()
    instrument_certified_tools(mcp, gauge)

    assert trivial.fn is _trivial  # non-certified tool untouched
    assert certified.fn is not _blocking_review_plan  # certified tool wrapped
    assert async_certified.fn is async_certified_fn  # async certified tool left untouched

    assert gauge.value == 0
    worker = threading.Thread(target=certified.fn)
    worker.start()
    assert started.wait(timeout=5)
    assert gauge.value == 1  # certified op is in flight
    release.set()
    worker.join(timeout=5)
    assert gauge.value == 0  # drained after the op returns


def test_wrap_tool_fn_passes_args_and_kwargs_through_and_counts():
    """_wrap_tool_fn (installed by instrument_certified_tools) forwards positional +
    keyword arguments to the wrapped tool and returns its result, while the gauge
    tracks the call — the wrapper is *args/**kwargs, so an arg-taking tool must still
    work through FastMCP's call_fn_with_arg_validation path."""
    from types import SimpleNamespace

    from rebar._mcp_health import InFlightGauge, instrument_certified_tools

    seen = {}

    def _review_plan(ticket, *, depth=1):
        seen["args"] = (ticket, depth)
        return f"{ticket}:{depth}"

    tool = SimpleNamespace(fn=_review_plan, is_async=False)
    manager = SimpleNamespace(get_tool=lambda n: {"review_plan": tool}.get(n))
    gauge = InFlightGauge()
    instrument_certified_tools(SimpleNamespace(_tool_manager=manager), gauge)

    result = tool.fn("abcd", depth=3)  # the wrapped fn, called with args
    assert result == "abcd:3"
    assert seen["args"] == ("abcd", 3)
    assert gauge.value == 0  # balanced after the call returns


def test_make_sigterm_handler_stops_intake_then_drains():
    """The SIGTERM handler BODY built by make_sigterm_handler (used by
    run_http_with_grace) is executed directly: it sets should_exit immediately (stop
    accepting new connections) and does NOT return until the gauge drains — proving the
    stop-then-drain ordering, not merely that a handler was installed."""
    import threading

    from rebar._mcp_health import InFlightGauge, make_sigterm_handler

    class _FakeServer:
        should_exit = False

    server = _FakeServer()
    gauge = InFlightGauge()
    gauge._increment()  # one op in flight when the signal arrives
    handler = make_sigterm_handler(server, gauge, grace_seconds=5, poll_interval=0.02)

    order = []

    def _release():
        time.sleep(0.15)
        order.append(("should_exit_before_drain_done", server.should_exit))
        gauge._decrement()

    threading.Thread(target=_release, daemon=True).start()
    handler(15, None)  # execute the real handler body (SIGTERM == 15)

    assert server.should_exit is True  # intake stopped
    assert gauge.value == 0  # handler waited for the in-flight op to finish
    # should_exit was already True WHILE the op was still draining (intake-stop first).
    assert order and order[0] == ("should_exit_before_drain_done", True)


def test_wired_server_moves_gauge_for_a_really_registered_certified_tool():
    """Integration of the PRODUCTION wiring: a really-registered certified tool
    (name review_plan) on a real FastMCP, instrumented by wire_health, moves the
    /health gauge while it runs and returns to 0 after — proving instrument_certified_tools
    + _wrap_tool_fn actually wrap a genuinely-registered tool (only the tool BODY is a
    stub). Read through the real /health route via TestClient."""
    import threading

    from mcp.server.fastmcp import FastMCP
    from starlette.testclient import TestClient

    from rebar._mcp_health import wire_health

    server = FastMCP("rebar-test", host="127.0.0.1", port=8000)
    started = threading.Event()
    release = threading.Event()

    @server.tool(name="review_plan")
    def review_plan() -> str:  # a stubbed certified op
        started.set()
        release.wait(timeout=5)
        return "ok"

    wire_health(server)
    wrapped = server._tool_manager.get_tool("review_plan").fn  # the production wrapper
    app = server.streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/health").json()["in_flight"] == 0
        worker = threading.Thread(target=wrapped)
        worker.start()
        assert started.wait(timeout=5)
        assert client.get("/health").json()["in_flight"] == 1  # via the real endpoint
        release.set()
        worker.join(timeout=5)
        assert client.get("/health").json()["in_flight"] == 0


def test_drain_then_exit_waits_for_gauge_then_exits_within_bound():
    """drain_then_exit blocks (bounded by grace) until the gauge drains, then calls
    exit_fn. With a fake clock: it does not exit while in flight, and does once the
    gauge reaches 0."""
    from rebar._mcp_health import InFlightGauge, drain_then_exit

    gauge = InFlightGauge()
    gauge._increment()  # one op in flight
    now = {"t": 0.0}
    exited = {"v": False}
    ticks = {"n": 0}

    def _sleep(dt):
        now["t"] += dt
        ticks["n"] += 1
        if ticks["n"] == 3:  # the op finishes after a few polls
            gauge._decrement()

    drain_then_exit(
        gauge,
        grace_seconds=1200,
        exit_fn=lambda: exited.__setitem__("v", True),
        poll_interval=0.5,
        sleep=_sleep,
        monotonic=lambda: now["t"],
    )
    assert exited["v"] is True
    assert gauge.value == 0
    assert now["t"] < 1200  # exited well within the bound once drained


def test_drain_then_exit_is_bounded_when_gauge_never_drains():
    """A stuck op does not hang shutdown forever: drain_then_exit exits once the
    grace bound elapses even though the gauge is still > 0."""
    from rebar._mcp_health import InFlightGauge, drain_then_exit

    gauge = InFlightGauge()
    gauge._increment()
    now = {"t": 0.0}
    exited = {"v": False}

    drain_then_exit(
        gauge,
        grace_seconds=10,
        exit_fn=lambda: exited.__setitem__("v", True),
        poll_interval=1.0,
        sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
        monotonic=lambda: now["t"],
    )
    assert exited["v"] is True
    assert gauge.value == 1  # never drained
    assert now["t"] >= 10  # waited the full bound


def test_run_http_with_grace_installs_sigterm_and_bounds_uvicorn(monkeypatch):
    """run_http_with_grace builds a uvicorn server with a bounded graceful-shutdown
    timeout equal to the grace, installs a SIGTERM handler, and joins the serving
    thread. A fake uvicorn Server (run() returns immediately) keeps the test
    port-free; the SIGTERM handler drains the gauge then sets should_exit."""
    import signal

    import rebar._mcp_health as health
    from rebar.mcp_server import build_server

    captured = {}

    class _FakeServer:
        def __init__(self, config):
            captured["config"] = config
            self.should_exit = False

        def run(self):  # returns immediately → serving thread ends at once
            captured["ran"] = True

    class _FakeConfig:
        def __init__(self, app, **kwargs):
            captured["config_kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.Config", _FakeConfig)
    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    prev = signal.getsignal(signal.SIGTERM)
    try:
        server = build_server(_http_config())
        gauge = getattr(server, health._GAUGE_ATTR)
        health.run_http_with_grace(server, gauge, grace_seconds=1200)
        assert captured.get("ran") is True
        assert captured["config_kwargs"]["timeout_graceful_shutdown"] == 1200
        assert signal.getsignal(signal.SIGTERM) not in (prev, signal.SIG_DFL)
    finally:
        signal.signal(signal.SIGTERM, prev)


def test_run_mcp_stdio_delegates_to_fastmcp_run(monkeypatch):
    """run_mcp on a stdio config calls FastMCP.run(transport='stdio') and does not
    touch the HTTP grace path."""
    from types import SimpleNamespace

    from rebar._mcp_health import run_mcp

    calls = {}
    server = SimpleNamespace(run=lambda **kw: calls.update(kw))
    run_mcp(server, SimpleNamespace(transport="stdio"))
    assert calls == {"transport": "stdio"}


def test_shutdown_grace_constant_is_positive_and_bounded():
    """The module grace constant is the documented 1200s budget."""
    from rebar._mcp_health import DEFAULT_SHUTDOWN_GRACE_SECONDS

    assert DEFAULT_SHUTDOWN_GRACE_SECONDS == 1200


def test_sigterm_grace_subprocess_waits_for_inflight_then_exits_zero():
    """End-to-end SIGTERM grace in a real subprocess: with one op in flight, a
    SIGTERM does NOT exit immediately — the process waits until the gauge drains
    (~0.8s here) and then exits 0, all within the grace bound. Drives the real
    _mcp_health.drain_then_exit via an installed SIGTERM handler + a real signal."""
    import time

    script = (
        "import os, sys, time, signal, threading\n"
        "from rebar._mcp_health import InFlightGauge, drain_then_exit\n"
        "g = InFlightGauge(); g._increment()\n"
        "def _release():\n"
        "    time.sleep(0.8); g._decrement()\n"
        "threading.Thread(target=_release, daemon=True).start()\n"
        "def _h(signum, frame):\n"
        "    drain_then_exit(g, grace_seconds=30, poll_interval=0.05,\n"
        "                    exit_fn=lambda: sys.exit(0))\n"
        "signal.signal(signal.SIGTERM, _h)\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
        "time.sleep(60)\n"
    )
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", script], timeout=30, check=False)
    elapsed = time.monotonic() - start
    assert proc.returncode == 0
    # The release thread holds the op ~0.8s, so a correct grace CANNOT have exited before
    # then; this lower bound proves it waited for the in-flight op rather than exiting on the
    # signal. The subprocess timeout=30 above is the hang guard (no upper-bound wall-clock
    # assert — that is the proven CI flake class).
    assert elapsed >= 0.5
