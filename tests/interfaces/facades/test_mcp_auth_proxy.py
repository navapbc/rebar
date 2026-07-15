"""S5 — trusted-proxy passthrough verifier.

Observable-behavior tests: config keys, the request-scoped proxy identity →
AccessToken, authenticated proxy request → 200 while bare/spoofed/wrong-secret →
401, the header-guard middleware stripping the whole X-Forwarded-* family (incl.
case/underscore smuggling variants) without a valid secret, secret-env fail-closed
startup, empty-proxy-scopes → 403, and composition with the static verifier. The
middleware is driven via httpx ASGITransport; the server via Starlette TestClient."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from rebar._config_schema import Config, McpConfig

RESOURCE = "https://mcp.example.com"
ISSUER = "https://issuer.example.com"
SECRET_ENV = "REBAR_TEST_PROXY_SECRET"
SECRET = "proxy-shared-s3cr3t"
INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _proxy_config(**overrides) -> Config:
    kwargs = dict(
        transport="http",
        auth_enabled=True,
        auth_strategies=("proxy",),
        auth_issuer_url=ISSUER,
        auth_resource_server_url=RESOURCE,
        auth_proxy_secret_env=SECRET_ENV,
    )
    kwargs.update(overrides)
    return Config(mcp=McpConfig(**kwargs))


# ── config keys ──────────────────────────────────────────────────────────────
def test_proxy_config_keys_parse_from_toml():
    raw = {
        "mcp": {
            "auth_proxy_secret_env": SECRET_ENV,
            "auth_proxy_secret_header": "x-my-proxy",
            "auth_proxy_identity_header": "x-user",
            "auth_proxy_scopes": "rebar.read, rebar.write",
        }
    }
    cfg = Config.from_mapping(raw)
    assert cfg.mcp.auth_proxy_secret_env == SECRET_ENV
    assert cfg.mcp.auth_proxy_secret_header == "x-my-proxy"
    assert cfg.mcp.auth_proxy_identity_header == "x-user"
    assert tuple(cfg.mcp.auth_proxy_scopes) == ("rebar.read", "rebar.write")


def test_proxy_config_defaults():
    cfg = McpConfig()
    assert cfg.auth_proxy_secret_header == "x-proxy-auth"
    assert cfg.auth_proxy_identity_header == "x-forwarded-user"
    assert tuple(cfg.auth_proxy_scopes) == ()


def test_proxy_verifier_maps_identity_to_access_token():
    from rebar._mcp_auth import PROXY_IDENTITY, ProxyTokenVerifier

    v = ProxyTokenVerifier(resource=RESOURCE, scopes=("rebar.use",))
    token = PROXY_IDENTITY.set("alice")
    try:
        result = asyncio.run(v.verify_token("ignored-marker"))
    finally:
        PROXY_IDENTITY.reset(token)
    assert result is not None
    assert result.client_id == "alice"
    assert set(result.scopes) == {"rebar.use"}
    assert result.resource == RESOURCE


def test_authenticated_proxy_request_200_and_bare_401(monkeypatch):
    from rebar.mcp_server import build_server

    monkeypatch.setenv(SECRET_ENV, SECRET)
    app = build_server(_proxy_config()).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        authed = client.post(
            "/mcp",
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "x-proxy-auth": SECRET, "x-forwarded-user": "alice"},
        )
        bare = client.post("/mcp", json=INIT_REQUEST, headers=MCP_HEADERS)
    assert authed.status_code == 200
    assert bare.status_code == 401


# ── verifier: no identity → None ─────────────────────────────────────────────
def test_proxy_verifier_none_without_identity():
    from rebar._mcp_auth import PROXY_IDENTITY, ProxyTokenVerifier

    token = PROXY_IDENTITY.set(None)
    try:
        assert (
            asyncio.run(ProxyTokenVerifier(resource=RESOURCE, scopes=()).verify_token("x")) is None
        )
    finally:
        PROXY_IDENTITY.reset(token)


# ── middleware strips spoofed forwarded headers when the secret is absent ────
class _Capture:
    """A minimal ASGI app that records the request headers it actually receives."""

    def __init__(self):
        self.headers = None

    async def __call__(self, scope, receive, send):
        self.headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(app, headers):
    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.get("/mcp", headers=headers)

    return asyncio.run(_go())


@pytest.mark.parametrize(
    "identity_header",
    ["x-forwarded-user", "X-Forwarded-User", "X_Forwarded_User", "X-FORWARDED-USER"],
)
def test_spoofed_identity_stripped_without_secret(identity_header):
    from rebar._mcp_auth import ProxyAuthMiddleware

    cap = _Capture()
    mw = ProxyAuthMiddleware(
        cap, secret=SECRET, secret_header="x-proxy-auth", identity_header="x-forwarded-user"
    )
    # No secret header at all — the client tries to smuggle an identity directly.
    _drive(mw, {identity_header: "attacker"})
    # The downstream app must NOT see any x-forwarded-* header (case/underscore-normalized).
    for name in cap.headers:
        normalized = name.replace("_", "-").lower()
        assert not normalized.startswith("x-forwarded"), f"{name} leaked to downstream"


def test_entire_x_forwarded_family_stripped_without_secret():
    from rebar._mcp_auth import ProxyAuthMiddleware

    cap = _Capture()
    mw = ProxyAuthMiddleware(
        cap, secret=SECRET, secret_header="x-proxy-auth", identity_header="x-forwarded-user"
    )
    _drive(
        mw,
        {
            "x-forwarded-user": "attacker",
            "x-forwarded-for": "1.2.3.4",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "evil.example.com",
        },
    )
    for name in cap.headers:
        assert not name.replace("_", "-").lower().startswith("x-forwarded")


def test_valid_secret_passes_identity_to_downstream():
    from rebar._mcp_auth import ProxyAuthMiddleware

    cap = _Capture()
    mw = ProxyAuthMiddleware(
        cap, secret=SECRET, secret_header="x-proxy-auth", identity_header="x-forwarded-user"
    )
    _drive(mw, {"x-proxy-auth": SECRET, "x-forwarded-user": "alice"})
    # With a valid secret the middleware repopulates a synthetic bearer for the verifier.
    assert cap.headers.get("authorization", "").lower().startswith("bearer ")


# ── secret-env fail-closed startup ───────────────────────────────────────────
def test_refuses_start_when_secret_env_absent(monkeypatch):
    from rebar._mcp_auth import AuthConfigError, build_composite_verifier

    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(AuthConfigError):
        build_composite_verifier(_proxy_config().mcp)


def test_refuses_start_when_secret_env_empty(monkeypatch):
    from rebar._mcp_auth import AuthConfigError, build_composite_verifier

    monkeypatch.setenv(SECRET_ENV, "")
    with pytest.raises(AuthConfigError):
        build_composite_verifier(_proxy_config().mcp)


# ── scope 403 with empty proxy scopes ────────────────────────────────────────
def test_spoofed_identity_without_secret_401_at_transport(monkeypatch):
    """End-to-end: a request presenting the identity header but NO valid proxy secret
    is NOT trusted — the spoofed identity yields 401, not 200 (a broken secret check
    that trusted the header would return 200)."""
    from rebar.mcp_server import build_server

    monkeypatch.setenv(SECRET_ENV, SECRET)
    app = build_server(_proxy_config()).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        spoofed = client.post(
            "/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "x-forwarded-user": "attacker"}
        )
        wrong_secret = client.post(
            "/mcp",
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "x-proxy-auth": "WRONG", "x-forwarded-user": "attacker"},
        )
    assert spoofed.status_code == 401
    assert wrong_secret.status_code == 401


def test_empty_proxy_scopes_403_and_granted_scope_200(monkeypatch):
    from rebar.mcp_server import build_server

    monkeypatch.setenv(SECRET_ENV, SECRET)
    hdrs = {**MCP_HEADERS, "x-proxy-auth": SECRET, "x-forwarded-user": "alice"}

    # required scope set, but proxy grants no scopes → 403
    denied_app = build_server(
        _proxy_config(auth_required_scopes=("rebar.use",))
    ).streamable_http_app()
    with TestClient(denied_app, base_url="http://127.0.0.1:8000") as client:
        denied = client.post("/mcp", json=INIT_REQUEST, headers=hdrs)
    assert denied.status_code == 403

    # grant the scope to proxy principals → 200
    ok_app = build_server(
        _proxy_config(auth_required_scopes=("rebar.use",), auth_proxy_scopes=("rebar.use",))
    ).streamable_http_app()
    with TestClient(ok_app, base_url="http://127.0.0.1:8000") as client:
        allowed = client.post("/mcp", json=INIT_REQUEST, headers=hdrs)
    assert allowed.status_code == 200


# ── composition: proxy,static — a static token without proxy identity works ──
def test_composition_static_token_without_proxy_identity(tmp_path, monkeypatch):
    from rebar.mcp_server import build_server

    monkeypatch.setenv(SECRET_ENV, SECRET)
    token = "static-high-entropy-token-proxy-compose-123456"
    tf = tmp_path / "tokens.json"
    tf.write_text(
        json.dumps(
            {
                "tokens": [
                    {
                        "name": "ci",
                        "client_id": "ci",
                        "scopes": [],
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    }
                ]
            }
        )
    )
    cfg = _proxy_config(auth_strategies=("proxy", "static"), auth_static_tokens_file=str(tf))
    app = build_server(cfg).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        # No proxy headers at all — proxy verifier returns None, static accepts.
        resp = client.post(
            "/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 200


# ── manifest + env vars ──────────────────────────────────────────────────────
def test_proxy_env_vars_and_manifest():
    import subprocess
    import sys
    from pathlib import Path

    from rebar.mcp_server import MCP_ENV_VARS

    names = {v["name"] for v in MCP_ENV_VARS}
    for k in ("SECRET_ENV", "SECRET_HEADER", "IDENTITY_HEADER", "SCOPES"):
        assert f"REBAR_MCP_AUTH_PROXY_{k}" in names, f"REBAR_MCP_AUTH_PROXY_{k} missing"
    repo_root = Path(__file__).resolve().parents[3]
    r = subprocess.run(
        [sys.executable, "scripts/check_server_manifest.py"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
