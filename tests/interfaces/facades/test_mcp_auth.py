"""S2 — auth framework + Resource-Server wiring + static-bearer verifier.

Observable-behavior tests for the composite audience choke point (the single
RFC 8707 audience / fail-closed enforcement point), the static bearer verifier,
the Resource-Server wiring over the real ASGI transport (PRM, 401, scope 403),
fail-closed startup, digest-only + constant-time secret storage, redaction, and
session-id replay resistance.

Async verifiers are driven via ``asyncio.run``; the transport is driven
in-process via Starlette's ``TestClient`` (httpx over the ASGI app, running the
app lifespan / session-manager context).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import pytest
from starlette.testclient import TestClient

from rebar._config_schema import Config, McpConfig

RESOURCE = "https://mcp.example.com"
ISSUER = "https://issuer.example.com"

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
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tokens_file(tmp_path, *records) -> str:
    """Write a JSON static-tokens secrets file and return its path."""
    p = tmp_path / "tokens.json"
    p.write_text(json.dumps({"tokens": list(records)}))
    return str(p)


def _record(client_id, digest_token=None, *, scopes=None, token_env=None, name="rec"):
    """Build one static-token record (digest by default, or an env reference)."""
    rec = {"name": name, "client_id": client_id, "scopes": scopes or []}
    if token_env is not None:
        rec["token_env"] = token_env
    else:
        rec["token_sha256"] = _sha256(digest_token)
    return rec


def _auth_config(tokens_file, **overrides) -> Config:
    """A Config with auth enabled + the static strategy over the HTTP transport."""
    kwargs = dict(
        transport="http",
        auth_enabled=True,
        auth_strategies=("static",),
        auth_issuer_url=ISSUER,
        auth_resource_server_url=RESOURCE,
        auth_static_tokens_file=tokens_file,
    )
    kwargs.update(overrides)
    return Config(mcp=McpConfig(**kwargs))


class _StubVerifier:
    """A TokenVerifier-shaped stub returning a preset AccessToken (or None/raise)."""

    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises
        self.called = False

    async def verify_token(self, token):
        self.called = True
        if self._raises:
            raise RuntimeError("stub verifier boom: token=" + token)
        return self._result


def _access_token(**overrides):
    from mcp.server.auth.provider import AccessToken

    kwargs = dict(
        token="t", client_id="c", scopes=["rebar.use"], resource=RESOURCE, expires_at=None
    )
    kwargs.update(overrides)
    return AccessToken(**kwargs)


# ── config keys ──────────────────────────────────────────────────────────────
def test_auth_config_keys_parse_from_toml():
    raw = {
        "mcp": {
            "auth_enabled": True,
            "auth_strategies": "static, jwt",
            "auth_issuer_url": ISSUER,
            "auth_resource_server_url": RESOURCE,
            "auth_required_scopes": "rebar.read, rebar.write",
            "auth_static_tokens_file": "/etc/rebar/tokens.json",
        }
    }
    cfg = Config.from_mapping(raw)
    assert cfg.mcp.auth_enabled is True
    assert tuple(cfg.mcp.auth_strategies) == ("static", "jwt")
    assert cfg.mcp.auth_issuer_url == ISSUER
    assert cfg.mcp.auth_resource_server_url == RESOURCE
    assert tuple(cfg.mcp.auth_required_scopes) == ("rebar.read", "rebar.write")
    assert cfg.mcp.auth_static_tokens_file == "/etc/rebar/tokens.json"


def test_auth_config_keys_resolve_from_env(monkeypatch):
    """Each auth key also resolves from its auto-derived REBAR_MCP_AUTH_* env var
    into the typed McpConfig (observable: resolved Config field values)."""
    from rebar import config as rc

    monkeypatch.setenv("REBAR_MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("REBAR_MCP_AUTH_STRATEGIES", "static,jwt")
    monkeypatch.setenv("REBAR_MCP_AUTH_ISSUER_URL", ISSUER)
    monkeypatch.setenv("REBAR_MCP_AUTH_RESOURCE_SERVER_URL", RESOURCE)
    monkeypatch.setenv("REBAR_MCP_AUTH_REQUIRED_SCOPES", "rebar.read,rebar.write")
    monkeypatch.setenv("REBAR_MCP_AUTH_STATIC_TOKENS_FILE", "/etc/rebar/tokens.json")
    cfg = Config.from_mapping(rc.env_overrides())
    assert cfg.mcp.auth_enabled is True
    assert tuple(cfg.mcp.auth_strategies) == ("static", "jwt")
    assert cfg.mcp.auth_issuer_url == ISSUER
    assert cfg.mcp.auth_resource_server_url == RESOURCE
    assert tuple(cfg.mcp.auth_required_scopes) == ("rebar.read", "rebar.write")
    assert cfg.mcp.auth_static_tokens_file == "/etc/rebar/tokens.json"


# ── static bearer verifier (unit) ────────────────────────────────────────────
def test_static_bearer_accepts_valid_and_rejects_unknown(tmp_path):
    from rebar._mcp_auth import StaticBearerVerifier

    token = "s3cr3t-high-entropy-token-abc123456789"
    tf = _tokens_file(tmp_path, _record("ci-bot", token, scopes=["rebar.use"], name="ci"))
    v = StaticBearerVerifier(tokens_file=tf, resource=RESOURCE)

    good = asyncio.run(v.verify_token(token))
    assert good is not None
    assert good.client_id == "ci-bot"
    assert "rebar.use" in good.scopes
    assert good.resource == RESOURCE
    assert good.expires_at is None  # static tokens are non-expiring, never 0

    assert asyncio.run(v.verify_token("not-a-real-token")) is None


def test_static_token_env_reference(tmp_path, monkeypatch):
    """A record may reference an env var by NAME (token_env) instead of a digest."""
    from rebar._mcp_auth import StaticBearerVerifier

    token = "env-sourced-high-entropy-token-7777gggg8888"
    monkeypatch.setenv("REBAR_TEST_STATIC_TOKEN", token)
    tf = _tokens_file(
        tmp_path, _record("ci", scopes=["rebar.use"], token_env="REBAR_TEST_STATIC_TOKEN")
    )
    v = StaticBearerVerifier(tokens_file=tf, resource=RESOURCE)
    good = asyncio.run(v.verify_token(token))
    assert good is not None and good.client_id == "ci"


def test_static_stores_only_digests_not_plaintext(tmp_path):
    from rebar._mcp_auth import StaticBearerVerifier

    token = "digest-only-high-entropy-token-eeee5555ffff6666"
    v = StaticBearerVerifier(
        tokens_file=_tokens_file(tmp_path, _record("ci", token)), resource=RESOURCE
    )
    assert token not in repr(v.__dict__)


def test_static_rejects_plaintext_literal_record(tmp_path):
    """A record carrying a plaintext token literal (not a digest/env ref) is rejected."""
    from rebar._mcp_auth import AuthConfigError, StaticBearerVerifier

    tf = tmp_path / "t.json"
    tf.write_text(
        json.dumps(
            {"tokens": [{"name": "bad", "client_id": "bad", "scopes": [], "token": "plaintext"}]}
        )
    )
    with pytest.raises(AuthConfigError):
        StaticBearerVerifier(tokens_file=str(tf), resource=RESOURCE)


def test_static_rejects_record_missing_both_token_fields(tmp_path):
    from rebar._mcp_auth import AuthConfigError, StaticBearerVerifier

    tf = tmp_path / "t.json"
    tf.write_text(json.dumps({"tokens": [{"name": "bad", "client_id": "bad", "scopes": []}]}))
    with pytest.raises(AuthConfigError):
        StaticBearerVerifier(tokens_file=str(tf), resource=RESOURCE)


# ── composite (unit) ─────────────────────────────────────────────────────────
def test_composite_accepts_token_with_matching_resource(tmp_path):
    from rebar._mcp_auth import CompositeTokenVerifier, StaticBearerVerifier

    token = "another-high-entropy-token-xyz987654321"
    tf = _tokens_file(tmp_path, _record("svc-1", token, name="svc"))
    composite = CompositeTokenVerifier(
        [StaticBearerVerifier(tokens_file=tf, resource=RESOURCE)], resource=RESOURCE
    )
    result = asyncio.run(composite.verify_token(token))
    assert result is not None
    assert result.client_id == "svc-1"
    assert result.resource == RESOURCE


def test_composite_rejects_wrong_resource_token():
    """A sub-verifier returning an AccessToken whose resource != the configured
    resource server is rejected by the composite (audience re-check, RFC 8707)."""
    from rebar._mcp_auth import CompositeTokenVerifier

    wrong = _StubVerifier(result=_access_token(resource="https://evil.example.com"))
    assert asyncio.run(CompositeTokenVerifier([wrong], resource=RESOURCE).verify_token("x")) is None

    none_resource = _StubVerifier(result=_access_token(resource=None))
    composite = CompositeTokenVerifier([none_resource], resource=RESOURCE)
    assert asyncio.run(composite.verify_token("x")) is None


def test_composite_contract_none_accept_raise_and_shortcircuit():
    from rebar._mcp_auth import CompositeTokenVerifier

    # None → try next; matching AccessToken → accept; a later verifier is never
    # consulted. The post-accept verifier is a RAISING stub: if the short-circuit
    # ever regressed, consulting it would blow up — proving it is truly skipped.
    yielder = _StubVerifier(result=None)
    accepter = _StubVerifier(result=_access_token(client_id="winner"))
    never = _StubVerifier(raises=True)  # a landmine ordered after the acceptor
    composite = CompositeTokenVerifier([yielder, accepter, never], resource=RESOURCE)
    result = asyncio.run(composite.verify_token("tok"))
    assert result is not None and result.client_id == "winner"
    assert yielder.called and accepter.called
    assert never.called is False  # short-circuited after acceptance (raiser never run)

    # A raising verifier is non-acceptance (no swallow-to-accept); the next accepts.
    boom = _StubVerifier(raises=True)
    good = _StubVerifier(result=_access_token(client_id="ok"))
    composite2 = CompositeTokenVerifier([boom, good], resource=RESOURCE)
    result2 = asyncio.run(composite2.verify_token("tok"))
    assert result2 is not None and result2.client_id == "ok"

    # A composite of only a raising verifier denies (returns None).
    composite3 = CompositeTokenVerifier([_StubVerifier(raises=True)], resource=RESOURCE)
    assert asyncio.run(composite3.verify_token("tok")) is None


def test_secret_never_logged_on_verifier_raise(caplog):
    from rebar._mcp_auth import CompositeTokenVerifier

    secret = "super-secret-token-should-never-appear-in-logs-9999"
    composite = CompositeTokenVerifier([_StubVerifier(raises=True)], resource=RESOURCE)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(composite.verify_token(secret)) is None
    assert secret not in caplog.text


# ── fail-closed startup ──────────────────────────────────────────────────────
def test_failclosed_unknown_strategy(tmp_path):
    from rebar._mcp_auth import AuthConfigError, build_composite_verifier

    cfg = _auth_config(_tokens_file(tmp_path), auth_strategies=("static", "bogus"))
    with pytest.raises(AuthConfigError):
        build_composite_verifier(cfg.mcp)


def test_failclosed_empty_composite_when_auth_enabled(tmp_path):
    from rebar._mcp_auth import AuthConfigError, build_composite_verifier

    cfg = _auth_config(_tokens_file(tmp_path), auth_strategies=())
    with pytest.raises(AuthConfigError):
        build_composite_verifier(cfg.mcp)


def test_failclosed_auth_enabled_http_no_verifier(tmp_path):
    """auth_enabled + transport=http with no usable verifier refuses to boot."""
    from rebar.mcp_server import build_server

    cfg = _auth_config(_tokens_file(tmp_path), auth_strategies=())
    with pytest.raises(RuntimeError):  # a hard refusal (AuthConfigError / McpStartupError)
        build_server(cfg)


# ── Resource-Server wiring over the real ASGI transport ──────────────────────
def test_http_auth_valid_token_200_and_missing_token_401(tmp_path):
    from rebar.mcp_server import build_server

    token = "integration-high-entropy-token-qwerty00011122"
    tf = _tokens_file(tmp_path, _record("ci-bot", token, scopes=["rebar.use"], name="ci"))
    app = build_server(_auth_config(tf)).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        authed = client.post(
            "/mcp",
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
        )
        anon = client.post("/mcp", json=INIT_REQUEST, headers=MCP_HEADERS)
    assert authed.status_code == 200
    assert '"result"' in authed.text
    assert anon.status_code == 401


def test_prm_served_with_single_authorization_server(tmp_path):
    from rebar.mcp_server import build_server

    token = "prm-high-entropy-token-aaaa1111bbbb2222"
    cfg = _auth_config(_tokens_file(tmp_path, _record("ci", token)))
    app = build_server(cfg).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        anon = client.post("/mcp", json=INIT_REQUEST, headers=MCP_HEADERS)
        assert anon.status_code == 401
        assert "resource_metadata" in anon.headers.get("www-authenticate", "")
        prm = client.get("/.well-known/oauth-protected-resource")
        assert prm.status_code == 200
        doc = prm.json()
        assert isinstance(doc.get("authorization_servers"), list)
        assert len(doc["authorization_servers"]) == 1


def test_missing_required_scope_403_and_present_200(tmp_path):
    from rebar.mcp_server import build_server

    ok_token = "scope-ok-high-entropy-token-cccc3333"
    no_token = "scope-missing-high-entropy-token-dddd4444"
    tf = _tokens_file(
        tmp_path,
        _record("full", ok_token, scopes=["rebar.use"], name="full"),
        _record("lacks", no_token, scopes=["other"], name="lacks"),
    )
    app = build_server(_auth_config(tf, auth_required_scopes=("rebar.use",))).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        ok = client.post(
            "/mcp",
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {ok_token}"},
        )
        lacking = client.post(
            "/mcp",
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {no_token}"},
        )
        unknown = client.post(
            "/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "Authorization": "Bearer nope"}
        )
    assert ok.status_code == 200
    assert lacking.status_code == 403
    assert unknown.status_code == 401


def test_session_id_replay_cannot_hijack_principal(tmp_path):
    from rebar.mcp_server import build_server

    token = "session-high-entropy-token-hhhh0000iiii1111"
    tf = _tokens_file(tmp_path, _record("ci", token))
    app = build_server(_auth_config(tf)).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        authed = client.post(
            "/mcp",
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
        )
        assert authed.status_code == 200
        session_id = authed.headers.get("mcp-session-id")
        assert session_id  # the server minted a session for the authenticated client
        replay = client.post(
            "/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "Mcp-Session-Id": session_id}
        )
    # The replay (no token of its own) is never served in the first principal's context.
    assert replay.status_code == 401
