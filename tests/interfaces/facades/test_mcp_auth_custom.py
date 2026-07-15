"""S6 — pluggable custom verifier.

Observable-behavior tests: the config key, a valid ``module:factory`` resolving
(via importlib) to a verifier whose token the composite accepts, cwd is NOT
consulted (a cwd-only module fails to load), fail-closed startup (unresolvable
import / missing factory / object lacking verify_token), and the composite
audience re-check still rejecting a custom verifier that returns a wrong/None
resource. Custom-verifier modules are written to a temp dir on sys.path."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from rebar._config_schema import Config, McpConfig

RESOURCE = "https://mcp.example.com"
REPO_ROOT = Path(__file__).resolve().parents[3]

WRONG_RESOURCE_MODULE = textwrap.dedent(
    """
    from mcp.server.auth.provider import AccessToken

    class _Custom:
        async def verify_token(self, token):
            return AccessToken(token=token, client_id="x", scopes=[],
                               resource="https://evil.example.com", expires_at=None)

    def factory():
        return _Custom()
    """
)
NONE_RESOURCE_MODULE = textwrap.dedent(
    """
    from mcp.server.auth.provider import AccessToken

    class _Custom:
        async def verify_token(self, token):
            return AccessToken(token=token, client_id="x", scopes=[],
                               resource=None, expires_at=None)

    def factory():
        return _Custom()
    """
)
NOT_A_VERIFIER_MODULE = textwrap.dedent(
    """
    def factory():
        return object()  # no verify_token method
    """
)


def _write_module(tmp_path, name, source):
    (tmp_path / f"{name}.py").write_text(source)


def _custom_config(import_path, **overrides) -> Config:
    kwargs = dict(
        auth_enabled=True,
        auth_strategies=("custom",),
        auth_issuer_url="https://issuer.example.com",
        auth_resource_server_url=RESOURCE,
        auth_custom_import=import_path,
    )
    kwargs.update(overrides)
    return Config(mcp=McpConfig(**kwargs))


GOOD_MODULE = textwrap.dedent(
    """
    from mcp.server.auth.provider import AccessToken

    class _Custom:
        async def verify_token(self, token):
            return AccessToken(
                token=token, client_id="custom-user", scopes=["rebar.use"],
                resource="https://mcp.example.com", expires_at=None,
            )

    def factory():
        return _Custom()
    """
)


# ── config key ───────────────────────────────────────────────────────────────
def test_custom_config_key_parse_from_toml():
    cfg = Config.from_mapping({"mcp": {"auth_custom_import": "my.module:make_verifier"}})
    assert cfg.mcp.auth_custom_import == "my.module:make_verifier"


def test_custom_config_default():
    assert McpConfig().auth_custom_import == ""


def test_valid_module_factory_accepted_by_composite(tmp_path, monkeypatch):
    from rebar._mcp_auth import build_composite_verifier

    _write_module(tmp_path, "rebar_custom_good", GOOD_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    composite = build_composite_verifier(_custom_config("rebar_custom_good:factory").mcp)
    result = asyncio.run(composite.verify_token("opaque"))
    assert result is not None
    assert result.client_id == "custom-user"
    assert result.resource == RESOURCE


# ── cwd is NOT consulted ─────────────────────────────────────────────────────
def test_cwd_not_consulted(tmp_path, monkeypatch):
    """A module resolvable ONLY from the cwd (not installed, not on sys.path) fails
    to load — proving the loader does not add cwd to sys.path."""
    from rebar._mcp_auth import AuthConfigError, build_composite_verifier

    _write_module(
        tmp_path, "rebar_custom_cwdonly", "def factory():\n    raise AssertionError('imported!')\n"
    )
    monkeypatch.chdir(tmp_path)  # cwd contains the module, but it is NOT on sys.path
    assert "rebar_custom_cwdonly" not in sys.modules
    with pytest.raises(AuthConfigError):
        build_composite_verifier(_custom_config("rebar_custom_cwdonly:factory").mcp)
    # The cwd module was never imported (its factory would have raised on import/call).
    assert "rebar_custom_cwdonly" not in sys.modules


# ── fail-closed load ─────────────────────────────────────────────────────────
def test_unresolvable_import_fails_closed():
    from rebar._mcp_auth import AuthConfigError, build_composite_verifier

    with pytest.raises(AuthConfigError):
        build_composite_verifier(_custom_config("no.such.module:factory").mcp)


def test_missing_factory_attr_fails_closed(tmp_path, monkeypatch):
    from rebar._mcp_auth import AuthConfigError, build_composite_verifier

    _write_module(tmp_path, "rebar_custom_nofactory", "x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(AuthConfigError):
        build_composite_verifier(_custom_config("rebar_custom_nofactory:factory").mcp)


def test_resolved_object_without_verify_token_fails_closed(tmp_path, monkeypatch):
    from rebar._mcp_auth import AuthConfigError, build_composite_verifier

    _write_module(tmp_path, "rebar_custom_bad", NOT_A_VERIFIER_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(AuthConfigError):
        build_composite_verifier(_custom_config("rebar_custom_bad:factory").mcp)


# ── the composite audience re-check still applies to a custom verifier ───────
def test_wrong_resource_custom_verifier_rejected(tmp_path, monkeypatch):
    from rebar._mcp_auth import build_composite_verifier

    _write_module(tmp_path, "rebar_custom_wrong", WRONG_RESOURCE_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    composite = build_composite_verifier(_custom_config("rebar_custom_wrong:factory").mcp)
    assert asyncio.run(composite.verify_token("tok")) is None


def test_none_resource_custom_verifier_rejected(tmp_path, monkeypatch):
    from rebar._mcp_auth import build_composite_verifier

    _write_module(tmp_path, "rebar_custom_none", NONE_RESOURCE_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    composite = build_composite_verifier(_custom_config("rebar_custom_none:factory").mcp)
    assert asyncio.run(composite.verify_token("tok")) is None


# ── manifest + env var ───────────────────────────────────────────────────────
def test_custom_env_var_and_manifest():
    from rebar.mcp_server import MCP_ENV_VARS

    names = {v["name"] for v in MCP_ENV_VARS}
    assert "REBAR_MCP_AUTH_CUSTOM_IMPORT" in names
    r = subprocess.run(
        [sys.executable, "scripts/check_server_manifest.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
