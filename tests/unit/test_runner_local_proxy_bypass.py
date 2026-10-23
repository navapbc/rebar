"""Internal Anthropic agent calls bypass a loopback ANTHROPIC_BASE_URL because a local
payload optimizer can turn multi-turn tool replies into an empty stream. Non-loopback
gateways remain configured, and REBAR_LLM_ALLOW_LOCAL_PROXY opts back into loopback.
"""

from __future__ import annotations

import pytest

from rebar.llm.anthropic_model import _DIRECT_ANTHROPIC_BASE_URL, _local_proxy_bypass_base_url


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("REBAR_LLM_ALLOW_LOCAL_PROXY", raising=False)


@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1:8787",
        "http://localhost:8787",
        "http://127.0.0.1:8787/v1",
        "http://[::1]:8787",
        "http://0.0.0.0:8787",
    ],
)
def test_loopback_proxy_is_bypassed_to_direct_api(monkeypatch, base):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    assert _local_proxy_bypass_base_url() == _DIRECT_ANTHROPIC_BASE_URL


def test_unset_base_url_is_a_noop(monkeypatch):
    assert _local_proxy_bypass_base_url() is None


@pytest.mark.parametrize(
    "base",
    [
        "https://api.anthropic.com",
        "https://llm-gateway.corp.example.com",
        "https://anthropic.internal:8443",
    ],
)
def test_non_loopback_gateway_is_respected(monkeypatch, base):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    assert _local_proxy_bypass_base_url() is None


@pytest.mark.parametrize("optin", ["1", "true", "YES", " yes "])
def test_opt_in_keeps_the_local_proxy(monkeypatch, optin):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    monkeypatch.setenv("REBAR_LLM_ALLOW_LOCAL_PROXY", optin)
    assert _local_proxy_bypass_base_url() is None
