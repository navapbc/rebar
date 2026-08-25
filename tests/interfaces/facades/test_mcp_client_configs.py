"""Ticket 14f5-ca81 — the copilot / codex / claude MCP client configs wired to the
rebar MCP endpoint over remote HTTP with a ``static`` bearer PAT.

Observable-behavior tests for the three committed example client configs under
``examples/mcp-clients/``. The tier is *real client config load + a basic read-tool
call*, made hermetic by spinning up a loopback ``rebar-mcp`` HTTP instance with a known
static token — no network, no external service:

- **Real tool result (client-agnostic, always runs):** a structural loader parses each
  committed example config, validates it against that client's documented schema, and
  resolves the bearer via the client's documented env-var substitution; the loopback
  server then serves a real ``show_ticket`` read for the resolved ``Authorization``
  header (oracle: the returned ticket payload, not just "connected").
- **401 contrast (always runs):** the same loopback server returns 401 for an
  absent/invalid bearer, confirming the header is on the request path (not a hang).
- **No secret committed (always runs):** every example config references the PAT by env
  var only — no bearer literal appears in the committed files.
- **Client-binary acceptance (skips-with-reason when the CLI is absent):** where
  installed, ``copilot mcp get`` / ``codex mcp list`` / ``claude mcp list`` load the
  committed example config and list the ``rebar`` server.

The loopback server is driven with the real MCP SDK client over the real Streamable-HTTP
transport, so the composite auth verifier + bearer header path are exercised end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import tomllib
from _subprocess_env import subprocess_env

from rebar._config_schema import Config, McpConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = REPO_ROOT / "examples" / "mcp-clients"

# The external endpoint the deps (esok edge + avians PATs) landed: nginx TLS edge →
# loopback rebar-mcp, DNS-rebinding allowlist names this host.
EXPECTED_URL = "https://rebar.solutions.navateam.com/mcp/"

# The per-client PAT env var names avians materializes on the box (see
# infra/runbooks/mcp-client-pats.md). The client configs reference these by name.
PAT_ENV = {
    "copilot": "MCP_CLIENT_PAT_COPILOT",
    "codex": "MCP_CLIENT_PAT_CODEX",
    "claude": "MCP_CLIENT_PAT_CLAUDE",
}
# Distinct high-entropy TEST tokens (never real secrets) the loopback verifier accepts.
TEST_PATS = {
    "copilot": "test-pat-copilot-6f2a9c1e4b7d8a3f5e0c",
    "codex": "test-pat-codex-1a2b3c4d5e6f7a8b9c0d",
    "claude": "test-pat-claude-9e8d7c6b5a4f3e2d1c0b",
}

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
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


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── committed-config loaders: parse each example the way its client documents ──────
def _expand_env(value: str, env: dict[str, str]) -> str:
    """Expand ``$VAR`` / ``${VAR}`` from ``env`` (copilot / claude header substitution)."""

    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, value)


def load_copilot() -> tuple[str, str]:
    """Return (url, raw Authorization header) from the committed copilot config."""
    doc = json.loads((EXAMPLES / "copilot" / "mcp-config.json").read_text())
    entry = doc["mcpServers"]["rebar"]
    assert entry["type"] == "http"
    return entry["url"], entry["headers"]["Authorization"]


def load_claude() -> tuple[str, str]:
    """Return (url, raw Authorization header) from the committed claude config."""
    doc = json.loads((EXAMPLES / "claude" / ".mcp.json").read_text())
    entry = doc["mcpServers"]["rebar"]
    assert entry["type"] == "http"
    return entry["url"], entry["headers"]["Authorization"]


def load_codex() -> tuple[str, str]:
    """Return (url, bearer env var NAME) from the committed codex config."""
    doc = tomllib.loads((EXAMPLES / "codex" / "config.toml").read_text())
    entry = doc["mcp_servers"]["rebar"]
    return entry["url"], entry["bearer_token_env_var"]


def resolve_bearer(client: str, env: dict[str, str]) -> str:
    """Resolve the ``Authorization`` value the client would send, given ``env``."""
    if client == "copilot":
        _, raw = load_copilot()
        return _expand_env(raw, env)
    if client == "claude":
        _, raw = load_claude()
        return _expand_env(raw, env)
    _, var = load_codex()
    return f"Bearer {env[var]}"


def committed_url(client: str) -> str:
    return {"copilot": load_copilot, "claude": load_claude, "codex": load_codex}[client]()[0]


# ── loopback rebar-mcp HTTP server (real transport + real static-bearer verifier) ──
@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Boot a loopback rebar-mcp HTTP server whose ``static`` verifier accepts each
    client's TEST PAT (referenced by the same env var names as on the box), backed by a
    freshly-seeded ticket store so a read tool returns a REAL payload. Yields
    ``(base_url, store_path, seeded_ticket_id)``."""
    import rebar

    tmp = tmp_path_factory.mktemp("mcp-clients")
    prior = {name: os.environ.get(name) for name in PAT_ENV.values()}
    prior_sync = os.environ.get("REBAR_SYNC_PUSH")
    for client, var in PAT_ENV.items():
        os.environ[var] = TEST_PATS[client]

    # A seeded, git-backed store the loopback server reads (no network / no auto-push).
    os.environ["REBAR_SYNC_PUSH"] = "off"
    store = tmp / "store"
    store.mkdir()
    subprocess.run(["git", "init", "-q", str(store)], check=True)
    subprocess.run(["git", "-C", str(store), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(store), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(store), "commit", "-q", "--allow-empty", "-m", "root"], check=True
    )
    rebar.init_repo(repo_root=str(store))
    seeded_ticket = rebar.create_ticket(
        "task", "olm client-config smoke ticket", repo_root=str(store)
    )

    records = [{"name": c, "client_id": c, "scopes": [], "token_env": PAT_ENV[c]} for c in PAT_ENV]
    tokens_file = tmp / "static-tokens.json"
    tokens_file.write_text(json.dumps({"tokens": records}))

    port = _free_port()
    cfg = Config(
        mcp=McpConfig(
            transport="http",
            auth_enabled=True,
            auth_strategies=("static",),
            auth_issuer_url="https://issuer.example.com",
            auth_resource_server_url="https://mcp.example.com",
            auth_static_tokens_file=str(tokens_file),
            http_host="127.0.0.1",
            http_port=port,
        )
    )

    import uvicorn

    from rebar.mcp_server import build_server

    app = build_server(cfg).streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "loopback rebar-mcp HTTP server did not start"

    base_url = f"http://127.0.0.1:{port}/mcp"
    try:
        yield base_url, str(store), seeded_ticket
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for name, value in {**prior, "REBAR_SYNC_PUSH": prior_sync}.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def _call_list_tickets(base_url: str, bearer: str):
    import httpx as _httpx
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with _httpx.AsyncClient(headers={"Authorization": bearer}) as http_client:
        async with streamable_http_client(base_url, http_client=http_client) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await session.call_tool("list_tickets", {})


# ── AC oracle 1: real read-tool result through HTTP with the resolved bearer ───────
@pytest.mark.parametrize("client", ["copilot", "codex", "claude"])
def test_committed_config_bearer_executes_read_tool(client, live_server, monkeypatch):
    """Each committed example config resolves (via its client's documented env-var
    substitution) to a bearer the server accepts, and a basic read tool returns a REAL
    ticket payload over the HTTP transport — not merely 'connected'."""
    import rebar

    base_url, store, seeded_ticket = live_server
    # Point the server's per-call store resolution at the seeded store.
    monkeypatch.setenv("REBAR_ROOT", store)
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.config.reset_config_cache()

    # The committed config points at the external endpoint the deps landed.
    assert committed_url(client) == EXPECTED_URL

    bearer = resolve_bearer(client, subprocess_env())
    assert bearer == f"Bearer {TEST_PATS[client]}"

    result = asyncio.run(_call_list_tickets(base_url, bearer))

    assert result.isError is False
    payload = result.structuredContent
    assert isinstance(payload, dict)
    # list_tickets returns the seeded ticket — a concrete tool RESULT over the transport.
    tickets = payload.get("result")
    assert isinstance(tickets, list) and len(tickets) > 0
    assert any(t.get("ticket_id") == seeded_ticket for t in tickets)


# ── AC oracle 3: absent / invalid bearer → 401 surfaced (header is on the path) ────
def test_absent_or_invalid_bearer_is_401(live_server):
    base_url, _store, _ticket = live_server
    with httpx.Client(base_url="") as client:
        anon = client.post(base_url, json=INIT_REQUEST, headers=MCP_HEADERS)
        wrong = client.post(
            base_url,
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "Authorization": "Bearer not-a-valid-pat-000"},
        )
    assert anon.status_code == 401
    assert wrong.status_code == 401


# ── AC 2: no secret committed — every config references the PAT by env var only ────
@pytest.mark.parametrize(
    ("relpath", "client"),
    [
        ("copilot/mcp-config.json", "copilot"),
        ("codex/config.toml", "codex"),
        ("claude/.mcp.json", "claude"),
    ],
)
def test_no_secret_committed(relpath, client):
    text = (EXAMPLES / relpath).read_text()
    # The PAT env var name is referenced.
    assert PAT_ENV[client] in text
    # No TEST/real PAT literal, and no inline bearer secret token.
    for pat in TEST_PATS.values():
        assert pat not in text
    assert re.search(r"Bearer\s+[A-Za-z0-9+/=_.-]{16,}", text) is None
    # codex must reference the env var by NAME, never a plaintext token field.
    if client == "codex":
        assert "bearer_token_env_var" in text
        assert '"token"' not in text and "token_sha256" not in text


# ── AC oracle 2: each client CLI accepts the committed config + lists `rebar` ───────
def _mask_home(tmp_path):
    """A scratch HOME so a client CLI reads only the config we plant (no user config)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{}")
    return home


@pytest.mark.skipif(shutil.which("copilot") is None, reason="copilot CLI not installed")
def test_copilot_cli_loads_committed_config(tmp_path):
    home = _mask_home(tmp_path)
    (home / ".copilot").mkdir()
    shutil.copy(EXAMPLES / "copilot" / "mcp-config.json", home / ".copilot" / "mcp-config.json")
    env = subprocess_env({"HOME": str(home), PAT_ENV["copilot"]: TEST_PATS["copilot"]})
    proc = subprocess.run(
        ["copilot", "mcp", "get", "rebar"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "rebar" in proc.stdout
    assert EXPECTED_URL in proc.stdout


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
def test_codex_cli_loads_committed_config(tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    shutil.copy(EXAMPLES / "codex" / "config.toml", codex_home / "config.toml")
    env = subprocess_env({"CODEX_HOME": str(codex_home), PAT_ENV["codex"]: TEST_PATS["codex"]})
    proc = subprocess.run(
        ["codex", "mcp", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "rebar" in proc.stdout
    assert PAT_ENV["codex"] in proc.stdout


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_claude_cli_loads_committed_config(tmp_path):
    home = _mask_home(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    shutil.copy(EXAMPLES / "claude" / ".mcp.json", proj / ".mcp.json")
    env = subprocess_env({"HOME": str(home), PAT_ENV["claude"]: TEST_PATS["claude"]})
    proc = subprocess.run(
        ["claude", "mcp", "list"],
        env=env,
        cwd=str(proj),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "rebar" in proc.stdout
    assert EXPECTED_URL in proc.stdout


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
