"""rebar's internal agent must bypass a LOCAL Claude-Code payload optimizer (bug
sue-skimp-tear).

A loopback ``ANTHROPIC_BASE_URL`` (e.g. headroom on ``127.0.0.1:8787``) is inherited by
the Anthropic SDK and corrupts rebar's own multi-turn AGENTIC tool-loop requests into an
empty provider stream, collapsing the plan-review / completion verifiers to INDETERMINATE.
The runner therefore pins an Anthropic model to the DIRECT public API when
``ANTHROPIC_BASE_URL`` is a loopback host, respects real (non-loopback) gateways, and
honours the ``REBAR_LLM_ALLOW_LOCAL_PROXY`` opt-in.
"""

from __future__ import annotations

import pytest

from rebar.llm.runner import _DIRECT_ANTHROPIC_BASE_URL, _local_proxy_bypass_base_url


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
