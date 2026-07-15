"""S3 — OIDC / JWKS JWT verifier.

Observable-behavior tests: config keys, a valid JWKS-issued JWT resolving to an
AccessToken, the rejection matrix (iss/aud/exp/nbf/alg:none/HS256-confusion), the
configurable typ check, symmetric-algorithm startup refusal, construction-time
SSRF guards + timeout, JWKS-fetch-failure deny, the concurrency-safe unknown-kid
cooldown (exactly one fetch per burst), and end-to-end over the ASGI transport.
The JWKS HTTP fetch is a stateful fake patched over ``jwt.PyJWKClient`` serving a
locally generated RSA key set."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from rebar._config_schema import Config, McpConfig

RESOURCE = "https://mcp.example.com"
ISSUER = "https://issuer.example.com"
JWKS_URI = "https://issuer.example.com/.well-known/jwks.json"
REPO_ROOT = Path(__file__).resolve().parents[3]

_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUB = _PRIV.public_key()
_ROTATED = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEYS = {"key-1": _PUB}

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
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


def _mint(priv=_PRIV, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "exp": now + 3600,
        "nbf": now - 10,
        "iat": now,
        "scope": "rebar.use",
        "sub": "user-1",
    }
    headers = {"kid": overrides.pop("_kid", "key-1")}
    typ = overrides.pop("_typ", None)
    if typ is not None:
        headers["typ"] = typ
    claims.update(overrides)
    return jwt.encode(claims, priv, algorithm=alg, headers=headers)


class _FakeJWKClient:
    instances: list = []

    def __init__(self, uri, *args, timeout=None, **kwargs):
        self.uri = uri
        self.timeout = timeout
        self.fetch_calls = 0
        _FakeJWKClient.instances.append(self)

    def get_signing_key_from_jwt(self, token):
        self.fetch_calls += 1
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in KEYS:
            raise jwt.exceptions.PyJWKClientError(f"unknown kid {kid!r}")
        return SimpleNamespace(key=KEYS[kid])


class _RaisingJWKClient(_FakeJWKClient):
    def get_signing_key_from_jwt(self, token):
        self.fetch_calls += 1
        raise jwt.exceptions.PyJWKClientConnectionError("JWKS endpoint unreachable")


@pytest.fixture
def patched_jwks(monkeypatch):
    _FakeJWKClient.instances = []
    monkeypatch.setattr("jwt.PyJWKClient", _FakeJWKClient)
    return _FakeJWKClient


def _verifier(**overrides):
    from rebar._mcp_auth import JWKSTokenVerifier

    kwargs = dict(
        jwks_uri=JWKS_URI,
        issuer=ISSUER,
        resource=RESOURCE,
        algorithms=("RS256", "ES256"),
        leeway=60,
        refetch_cooldown=30,
        timeout=10,
        expected_typ="",
        allow_private_jwks_host=False,
    )
    kwargs.update(overrides)
    return JWKSTokenVerifier(**kwargs)


# ── config keys ──────────────────────────────────────────────────────────────
def test_jwt_config_keys_parse_from_toml():
    raw = {
        "mcp": {
            "auth_jwt_jwks_uri": JWKS_URI,
            "auth_jwt_issuer": ISSUER,
            "auth_jwt_algorithms": "RS256, ES384",
            "auth_jwt_leeway": 90,
            "auth_jwt_jwks_refetch_cooldown": 45,
            "auth_jwt_jwks_timeout": 12,
            "auth_jwt_expected_typ": "at+JWT",
            "auth_jwt_allow_private_jwks_host": True,
        }
    }
    cfg = Config.from_mapping(raw)
    assert cfg.mcp.auth_jwt_jwks_uri == JWKS_URI
    assert cfg.mcp.auth_jwt_issuer == ISSUER
    assert tuple(cfg.mcp.auth_jwt_algorithms) == ("RS256", "ES384")
    assert cfg.mcp.auth_jwt_leeway == 90
    assert cfg.mcp.auth_jwt_jwks_refetch_cooldown == 45
    assert cfg.mcp.auth_jwt_jwks_timeout == 12
    assert cfg.mcp.auth_jwt_expected_typ == "at+JWT"
    assert cfg.mcp.auth_jwt_allow_private_jwks_host is True


def test_jwt_config_defaults():
    cfg = McpConfig()
    assert tuple(cfg.auth_jwt_algorithms) == ("RS256", "ES256")
    assert cfg.auth_jwt_leeway == 60
    assert cfg.auth_jwt_jwks_refetch_cooldown == 30
    assert cfg.auth_jwt_jwks_timeout == 10
    assert cfg.auth_jwt_expected_typ == ""
    assert cfg.auth_jwt_allow_private_jwks_host is False


def test_valid_jwt_yields_access_token(patched_jwks):
    exp = int(time.time()) + 3600
    result = asyncio.run(_verifier().verify_token(_mint(exp=exp)))
    assert result is not None
    assert result.resource == RESOURCE
    assert result.client_id == "user-1"  # from sub
    assert set(result.scopes) >= {"rebar.use"}
    assert result.expires_at == exp  # exact exp claim, not merely positive


# ── rejections (each a distinct negative control) ────────────────────────────
@pytest.mark.parametrize(
    "mint_kwargs",
    [
        {"iss": "https://evil.example.com"},  # wrong issuer
        {"aud": "https://other.example.com"},  # wrong audience
        {"exp": int(time.time()) - 3600},  # expired
        {"nbf": int(time.time()) + 3600},  # not yet valid
    ],
)
def test_rejects_bad_claims(patched_jwks, mint_kwargs):
    assert asyncio.run(_verifier().verify_token(_mint(**mint_kwargs))) is None


def test_rejects_alg_none(patched_jwks):
    now = int(time.time())
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": RESOURCE, "exp": now + 3600, "sub": "x"},
        key=None,
        algorithm="none",
        headers={"kid": "key-1"},
    )
    assert asyncio.run(_verifier().verify_token(unsigned)) is None


def test_rejects_hs256_signed_with_public_key(patched_jwks):
    """RS256↔HS256 confusion: an HS256 token whose HMAC 'secret' is the RSA public
    key PEM is rejected (the verifier pins asymmetric algorithms). The token is
    hand-crafted because PyJWT refuses to encode HS256 with a PEM key."""
    import base64
    import hmac

    from cryptography.hazmat.primitives import serialization

    pub_pem = _PUB.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def _b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "key-1"}).encode())
    payload = _b64(
        json.dumps({"iss": ISSUER, "aud": RESOURCE, "exp": now + 3600, "sub": "x"}).encode()
    )
    signing_input = header + b"." + payload
    sig = _b64(hmac.new(pub_pem, signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + sig).decode()
    assert asyncio.run(_verifier().verify_token(forged)) is None


# ── typ check ────────────────────────────────────────────────────────────────
def test_expected_typ_enforced_when_set(patched_jwks):
    v = _verifier(expected_typ="at+JWT")
    assert asyncio.run(v.verify_token(_mint(_typ="at+JWT"))) is not None  # matching
    assert asyncio.run(v.verify_token(_mint(_typ="JWT"))) is None  # mismatched
    assert asyncio.run(v.verify_token(_mint())) is None  # absent


def test_typ_skipped_when_unset(patched_jwks):
    # expected_typ="" → the typ check is skipped, a token without typ is accepted.
    assert asyncio.run(_verifier().verify_token(_mint())) is not None


# ── symmetric-algorithm startup refusal ──────────────────────────────────────
@pytest.mark.parametrize("bad_alg", ["HS256", "HS384", "HS512"])
def test_refuses_symmetric_algorithm_with_jwks(bad_alg):
    from rebar._mcp_auth import AuthConfigError

    with pytest.raises(AuthConfigError):
        _verifier(algorithms=("RS256", bad_alg))


# ── construction-time SSRF guards + timeout ──────────────────────────────────
def test_rejects_http_jwks_uri(patched_jwks):
    from rebar._mcp_auth import AuthConfigError

    with pytest.raises(AuthConfigError):
        _verifier(jwks_uri="http://issuer.example.com/jwks.json")


@pytest.mark.parametrize(
    "host",
    [
        "https://127.0.0.1/jwks.json",
        "https://10.0.0.5/jwks.json",
        "https://169.254.169.254/jwks.json",
    ],
)
def test_rejects_private_jwks_host_by_default(patched_jwks, host):
    from rebar._mcp_auth import AuthConfigError

    with pytest.raises(AuthConfigError):
        _verifier(jwks_uri=host)


def test_allows_private_jwks_host_when_opted_in(patched_jwks):
    v = _verifier(jwks_uri="https://10.0.0.5/jwks.json", allow_private_jwks_host=True)
    assert v is not None


def test_timeout_passed_to_pyjwkclient(patched_jwks):
    _verifier(timeout=10)
    assert patched_jwks.instances, "PyJWKClient was not constructed"
    assert patched_jwks.instances[-1].timeout == 10


# ── fetch failure raises → deny (distinct from invalid token → None) ─────────
def test_jwks_fetch_failure_denies(monkeypatch):
    _RaisingJWKClient.instances = []
    monkeypatch.setattr("jwt.PyJWKClient", _RaisingJWKClient)
    # A transport failure while fetching the JWKS → non-acceptance (None to the
    # composite, which denies). Contrast with the many "definitively invalid token
    # → None" cases above; both surface as None but this one is a fetch failure.
    from rebar._mcp_auth import JWKSTokenVerifier

    v = JWKSTokenVerifier(
        jwks_uri=JWKS_URI,
        issuer=ISSUER,
        resource=RESOURCE,
        algorithms=("RS256",),
        leeway=60,
        refetch_cooldown=30,
        timeout=10,
        expected_typ="",
        allow_private_jwks_host=False,
    )
    assert asyncio.run(v.verify_token(_mint())) is None


# ── unknown-kid cooldown: exactly one fetch for a burst ──────────────────────
def test_unknown_kid_cooldown_exactly_one_fetch(patched_jwks):
    v = _verifier(refetch_cooldown=30)

    async def _burst():
        # A burst of DISTINCT unknown kids within one cooldown window.
        for i in range(5):
            await v.verify_token(_mint(_kid=f"attacker-{i}"))

    asyncio.run(_burst())
    total = sum(inst.fetch_calls for inst in patched_jwks.instances)
    assert total == 1, f"expected exactly 1 JWKS fetch for the unknown-kid burst, got {total}"


def test_rotated_key_validates_after_cooldown(patched_jwks):
    # cooldown=0 → a legitimate rotation (new kid published to the JWKS) still refetches.
    KEYS["key-2"] = _ROTATED.public_key()
    try:
        v = _verifier(refetch_cooldown=0)
        rotated_token = _mint(priv=_ROTATED, _kid="key-2")
        assert asyncio.run(v.verify_token(rotated_token)) is not None
    finally:
        KEYS.pop("key-2", None)


# ── end-to-end over the ASGI transport ───────────────────────────────────────
def _auth_cfg():
    return Config(
        mcp=McpConfig(
            transport="http",
            auth_enabled=True,
            auth_strategies=("jwt",),
            auth_issuer_url=ISSUER,
            auth_resource_server_url=RESOURCE,
            auth_jwt_jwks_uri=JWKS_URI,
            auth_jwt_issuer=ISSUER,
        )
    )


def test_jwt_transport_valid_200_garbage_401(patched_jwks):
    from rebar.mcp_server import build_server

    app = build_server(_auth_cfg()).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        ok = client.post(
            "/mcp",
            json=INIT_REQUEST,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {_mint()}"},
        )
        bad = client.post(
            "/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "Authorization": "Bearer garbage"}
        )
    assert ok.status_code == 200
    assert bad.status_code == 401


# ── manifest + docs drift ────────────────────────────────────────────────────
def test_jwt_env_vars_and_manifest():
    from rebar.mcp_server import MCP_ENV_VARS

    names = {v["name"] for v in MCP_ENV_VARS}
    for k in ("JWKS_URI", "ISSUER", "ALGORITHMS", "LEEWAY", "JWKS_TIMEOUT", "EXPECTED_TYP"):
        assert f"REBAR_MCP_AUTH_JWT_{k}" in names, (
            f"REBAR_MCP_AUTH_JWT_{k} missing from MCP_ENV_VARS"
        )
    r = subprocess.run(
        [sys.executable, "scripts/check_server_manifest.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
