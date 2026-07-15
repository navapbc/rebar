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
        )
        assert r.returncode == 0, f"{script} --check stale:\n{r.stdout}{r.stderr}"
    ref = (REPO_ROOT / "docs" / "mcp-reference.md").read_text()
    env = (REPO_ROOT / "docs" / "env-vars.md").read_text()
    for var in NEW_ENV_VARS:
        assert var in ref, f"{var} missing from docs/mcp-reference.md"
        assert var in env, f"{var} missing from docs/env-vars.md"
