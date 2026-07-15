"""S4 — RFC 7662 token-introspection verifier.

Observable-behavior tests: config keys, a valid active token → AccessToken, the
RFC 7662 response matrix (exp/no-exp, active:false, aud string/array/missing,
scope split), fail-closed transport (non-200/timeout/malformed/no-active →
IntrospectionError), no-caching (re-introspect every call), client_secret_basic +
construction-time SSRF + secret-env guards, and the verifier inside the composite.
The introspection HTTP call is a stateful ``httpx.MockTransport`` fake fed
production-shaped RFC 7662 responses."""

from __future__ import annotations

import asyncio
import base64
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from rebar._config_schema import Config, McpConfig

RESOURCE = "https://mcp.example.com"
ENDPOINT = "https://as.example.com/introspect"
SECRET_ENV = "REBAR_TEST_INTROSPECTION_SECRET"
REPO_ROOT = Path(__file__).resolve().parents[3]


class _FakeAS:
    def __init__(self, response):
        self._response = response
        self.calls = 0
        self.last_auth = None
        self.last_token = None

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        self.calls += 1
        self.last_auth = request.headers.get("authorization")
        body = dict(p.split("=", 1) for p in request.content.decode().split("&") if "=" in p)
        self.last_token = body.get("token")
        if callable(self._response):
            return self._response(request)
        return httpx.Response(200, json=self._response)

    def basic_credentials(self):
        if not self.last_auth or not self.last_auth.startswith("Basic "):
            return None
        return base64.b64decode(self.last_auth.split(" ", 1)[1]).decode()


def _verifier(fake, monkeypatch, **overrides):
    from rebar._mcp_auth import IntrospectionTokenVerifier

    monkeypatch.setenv(SECRET_ENV, overrides.pop("secret", "s3cr3t"))
    kwargs = dict(
        endpoint=ENDPOINT,
        client_id="rebar-rs",
        client_secret_env=SECRET_ENV,
        resource=RESOURCE,
        allow_private_host=False,
        allow_missing_aud=False,
        transport=fake.transport() if fake else None,
    )
    kwargs.update(overrides)
    return IntrospectionTokenVerifier(**kwargs)


# ── config keys ──────────────────────────────────────────────────────────────
def test_introspection_config_keys_parse_from_toml():
    raw = {
        "mcp": {
            "auth_introspection_endpoint": ENDPOINT,
            "auth_introspection_client_id": "rebar-rs",
            "auth_introspection_client_secret_env": SECRET_ENV,
            "auth_introspection_allow_private_host": True,
            "auth_introspection_allow_missing_aud": True,
        }
    }
    cfg = Config.from_mapping(raw)
    assert cfg.mcp.auth_introspection_endpoint == ENDPOINT
    assert cfg.mcp.auth_introspection_client_id == "rebar-rs"
    assert cfg.mcp.auth_introspection_client_secret_env == SECRET_ENV
    assert cfg.mcp.auth_introspection_allow_private_host is True
    assert cfg.mcp.auth_introspection_allow_missing_aud is True


def test_introspection_config_defaults():
    cfg = McpConfig()
    assert cfg.auth_introspection_endpoint == ""
    assert cfg.auth_introspection_allow_private_host is False
    assert cfg.auth_introspection_allow_missing_aud is False


def test_active_token_with_exp_yields_access_token(monkeypatch):
    fake = _FakeAS(_active(exp=9999999999))
    result = asyncio.run(_verifier(fake, monkeypatch).verify_token("opaque-token-1"))
    assert result is not None
    assert result.resource == RESOURCE
    assert set(result.scopes) >= {"rebar.use"}
    assert result.expires_at == 9999999999  # exact exp claim, positive
    assert fake.last_token == "opaque-token-1"
    assert fake.basic_credentials() == "rebar-rs:s3cr3t"  # client_secret_basic carried


def _active(**extra):
    doc = {"active": True, "aud": RESOURCE, "exp": 9999999999, "scope": "rebar.use", "sub": "u1"}
    doc.update(extra)
    return doc


# ── expiry: exp present vs absent ────────────────────────────────────────────
def test_active_without_exp_yields_none_expiry(monkeypatch):
    doc = _active()
    doc.pop("exp")
    result = asyncio.run(_verifier(_FakeAS(doc), monkeypatch).verify_token("t"))
    assert result is not None
    assert result.expires_at is None  # never 0


# ── active:false and audience matrix ─────────────────────────────────────────
def test_inactive_token_returns_none(monkeypatch):
    # active:false but WITH a matching aud/exp, so the active-check is the sole gate
    doc = {"active": False, "aud": RESOURCE, "exp": 9999999999, "scope": "rebar.use"}
    assert asyncio.run(_verifier(_FakeAS(doc), monkeypatch).verify_token("t")) is None


def test_missing_aud_rejected_by_default_and_accepted_when_opted_in(monkeypatch):
    doc = _active()
    doc.pop("aud")
    assert asyncio.run(_verifier(_FakeAS(doc), monkeypatch).verify_token("t")) is None
    # same response, but allow_missing_aud=true → accepted
    ok = asyncio.run(_verifier(_FakeAS(doc), monkeypatch, allow_missing_aud=True).verify_token("t"))
    assert ok is not None and ok.resource == RESOURCE


def test_mismatched_aud_rejected(monkeypatch):
    assert (
        asyncio.run(
            _verifier(_FakeAS(_active(aud="https://other.example.com")), monkeypatch).verify_token(
                "t"
            )
        )
        is None
    )


def test_aud_array_membership(monkeypatch):
    contains = _active(aud=["https://a.example.com", RESOURCE])
    assert asyncio.run(_verifier(_FakeAS(contains), monkeypatch).verify_token("t")) is not None
    missing = _active(aud=["https://a.example.com", "https://b.example.com"])
    assert asyncio.run(_verifier(_FakeAS(missing), monkeypatch).verify_token("t")) is None


def test_scope_string_split_into_list(monkeypatch):
    doc = _active(scope="rebar.read rebar.write rebar.admin")
    result = asyncio.run(_verifier(_FakeAS(doc), monkeypatch).verify_token("t"))
    assert set(result.scopes) == {"rebar.read", "rebar.write", "rebar.admin"}


# ── fail-closed transport ────────────────────────────────────────────────────
def test_non_200_raises(monkeypatch):
    from rebar._mcp_auth import IntrospectionError

    fake = _FakeAS(lambda req: httpx.Response(500, text="oops"))
    with pytest.raises(IntrospectionError):
        asyncio.run(_verifier(fake, monkeypatch).verify_token("t"))


def test_timeout_raises(monkeypatch):
    from rebar._mcp_auth import IntrospectionError

    def _boom(req):
        raise httpx.ReadTimeout("read timed out", request=req)

    with pytest.raises(IntrospectionError):
        asyncio.run(_verifier(_FakeAS(_boom), monkeypatch).verify_token("t"))


def test_malformed_non_json_body_raises(monkeypatch):
    from rebar._mcp_auth import IntrospectionError

    fake = _FakeAS(lambda req: httpx.Response(200, text="not json at all"))
    with pytest.raises(IntrospectionError):
        asyncio.run(_verifier(fake, monkeypatch).verify_token("t"))


def test_json_without_active_field_raises(monkeypatch):
    from rebar._mcp_auth import IntrospectionError

    fake = _FakeAS({"scope": "rebar.use", "aud": RESOURCE})  # no boolean 'active'
    with pytest.raises(IntrospectionError):
        asyncio.run(_verifier(fake, monkeypatch).verify_token("t"))


def test_no_caching_reintrospects_every_call(monkeypatch):
    fake = _FakeAS(_active())
    v = _verifier(fake, monkeypatch)
    asyncio.run(v.verify_token("same-token"))
    asyncio.run(v.verify_token("same-token"))
    assert fake.calls == 2  # two identical requests hit the AS twice (no cache in v1)


# ── construction-time SSRF + secret guards ───────────────────────────────────
def test_http_endpoint_rejected(monkeypatch):
    from rebar._mcp_auth import AuthConfigError

    with pytest.raises(AuthConfigError):
        _verifier(None, monkeypatch, endpoint="http://as.example.com/introspect")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/introspect",
        "https://10.1.2.3/introspect",
        "https://169.254.0.1/introspect",
    ],
)
def test_private_host_rejected_by_default(monkeypatch, endpoint):
    from rebar._mcp_auth import AuthConfigError

    with pytest.raises(AuthConfigError):
        _verifier(None, monkeypatch, endpoint=endpoint)


def test_private_host_allowed_when_opted_in(monkeypatch):
    v = _verifier(
        None, monkeypatch, endpoint="https://10.1.2.3/introspect", allow_private_host=True
    )
    assert v is not None


def test_refuses_start_when_secret_env_absent(monkeypatch):
    from rebar._mcp_auth import AuthConfigError, IntrospectionTokenVerifier

    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(AuthConfigError):
        IntrospectionTokenVerifier(
            endpoint=ENDPOINT,
            client_id="rebar-rs",
            client_secret_env=SECRET_ENV,
            resource=RESOURCE,
            allow_private_host=False,
            allow_missing_aud=False,
        )


def test_refuses_start_when_secret_env_empty(monkeypatch):
    from rebar._mcp_auth import AuthConfigError, IntrospectionTokenVerifier

    monkeypatch.setenv(SECRET_ENV, "")
    with pytest.raises(AuthConfigError):
        IntrospectionTokenVerifier(
            endpoint=ENDPOINT,
            client_id="rebar-rs",
            client_secret_env=SECRET_ENV,
            resource=RESOURCE,
            allow_private_host=False,
            allow_missing_aud=False,
        )


# ── verifier inside the composite (component) ────────────────────────────────
def test_introspection_inside_composite(monkeypatch):
    from rebar._mcp_auth import CompositeTokenVerifier

    active = CompositeTokenVerifier([_verifier(_FakeAS(_active()), monkeypatch)], resource=RESOURCE)
    assert asyncio.run(active.verify_token("t")) is not None

    inactive = CompositeTokenVerifier(
        [_verifier(_FakeAS({"active": False}), monkeypatch)], resource=RESOURCE
    )
    assert asyncio.run(inactive.verify_token("t")) is None


# ── manifest + env vars ──────────────────────────────────────────────────────
def test_introspection_env_vars_and_manifest():
    from rebar.mcp_server import MCP_ENV_VARS

    names = {v["name"] for v in MCP_ENV_VARS}
    for k in (
        "ENDPOINT",
        "CLIENT_ID",
        "CLIENT_SECRET_ENV",
        "ALLOW_PRIVATE_HOST",
        "ALLOW_MISSING_AUD",
    ):
        assert f"REBAR_MCP_AUTH_INTROSPECTION_{k}" in names, (
            f"REBAR_MCP_AUTH_INTROSPECTION_{k} missing"
        )
    r = subprocess.run(
        [sys.executable, "scripts/check_server_manifest.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
