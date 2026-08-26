"""26ae (radium-condemned-smelts): resolved headers reach the wire.

`ee8a` gave operators a way to express headers and `6cd0` gave the run a stable identity;
neither sends anything. This puts the finished headers on the call via pydantic-ai's
`ModelSettings.extra_headers`, gated on a configured `base_url`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm import config as llm_config
from rebar.llm.capabilities import ModelCapabilities
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError
from rebar.llm.runner import RunRequest
from rebar.llm.structured_run import build_model_settings

pytestmark = pytest.mark.unit

_MODEL = "bedrock:us.anthropic.claude-sonnet-4-6"


def _caps() -> ModelCapabilities:
    return ModelCapabilities(
        native_structured_output=True,
        prompt_cache_style="none",
        supports_thinking=False,
        supports_temperature=True,
    )


def _req(cfg: LLMConfig, **kw) -> RunRequest:
    return RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], **kw)


def _cfg(**kw) -> LLMConfig:
    return LLMConfig(repo_path=".", model=_MODEL, **kw)


def _settings(cfg: LLMConfig) -> dict:
    return build_model_settings(cfg, _req(cfg), _caps(), _MODEL, None, model_override=None)


def _run(cfg: LLMConfig, **ident):
    """A gate-run scope carrying an identity, as a boundary would build it."""
    return llm_config.gate_config(
        LLMConfig(
            **{
                **{f.name: getattr(cfg, f.name) for f in cfg.__dataclass_fields__.values()},
                **ident,
            }
        )
    )


# ══════════════════════════ HAPPY PATH ══════════════════════════


def test_headers_reach_extra_headers_when_base_url_set() -> None:
    """AC1: asserted on the dict the REAL assembly seam returns, not recomposed helpers."""
    cfg = _cfg(
        base_url="http://gw.invalid/v1",
        headers={"x-corp-tag": "rebar", "x-trace": "${run:trace_id}"},
        trace_id="a" * 32,
        ticket_id="t",
        operation="review-plan",
    )
    out = _settings(cfg)
    assert out["extra_headers"] == {"x-corp-tag": "rebar", "x-trace": "a" * 32}


def test_no_extra_headers_without_base_url() -> None:
    """AC3: no configured intermediary, nothing attached."""
    cfg = _cfg(headers={"x-corp-tag": "rebar"}, trace_id="a" * 32)
    assert "extra_headers" not in _settings(cfg)


def test_unconfigured_settings_are_unchanged() -> None:
    """AC4: an operator who configures nothing sees byte-identical behaviour."""
    cfg = _cfg(base_url="http://gw.invalid/v1")
    assert "extra_headers" not in _settings(cfg)


# ══════════════════════════ HELD-OUT ORACLE ══════════════════════════


def test_run_value_is_stable_within_a_run_and_differs_between_runs() -> None:
    """AC2: the whole point — one gate run is one correlation value."""
    base = _cfg(base_url="http://gw.invalid/v1", headers={"x-trace": "${run:trace_id}"})
    a1 = _settings(
        _cfg(
            base_url=base.base_url,
            headers=base.headers,
            trace_id="b" * 32,
            ticket_id="t",
            operation="review-plan",
        )
    )["extra_headers"]["x-trace"]
    a2 = _settings(
        _cfg(
            base_url=base.base_url,
            headers=base.headers,
            trace_id="b" * 32,
            ticket_id="t",
            operation="review-plan",
        )
    )["extra_headers"]["x-trace"]
    b1 = _settings(
        _cfg(
            base_url=base.base_url,
            headers=base.headers,
            trace_id="c" * 32,
            ticket_id="t",
            operation="review-plan",
        )
    )["extra_headers"]["x-trace"]
    assert a1 == a2 == "b" * 32
    assert b1 != a1


def test_absent_run_value_omits_the_whole_header() -> None:
    """AC5: `review-code` and every standalone op — no gate scope, no ticket.

    Dropped rather than sent empty: an empty correlation header pollutes the gateway, and
    raising would break every standalone op merely because headers are configured globally.
    A sibling literal header must survive, proving the omission is per-header.
    """
    cfg = _cfg(
        base_url="http://gw.invalid/v1",
        headers={"x-trace": "${run:trace_id}", "x-corp-tag": "rebar"},
    )
    out = _settings(cfg)
    assert out["extra_headers"] == {"x-corp-tag": "rebar"}


def test_unknown_run_key_raises_naming_it() -> None:
    """AC6: the closed vocabulary is enforced at the delivery seam too."""
    cfg = _cfg(base_url="http://gw.invalid/v1", headers={"x-t": "${run:bogus}"})
    with pytest.raises(LLMConfigError, match="bogus"):
        _settings(cfg)


@pytest.mark.parametrize("ctrl", ["\r", "\n", "\x00"])
def test_value_unsafe_only_after_substitution_is_rejected(ctrl: str) -> None:
    """AC7: re-validation after `${run:...}` substitution.

    Config-time validation saw a clean placeholder; the run value is what makes it unsafe,
    so the same checks must run again on the finished header — the identical
    validate-before-substitute gap the config ticket closed on its own seam.
    """
    cfg = _cfg(
        base_url="http://gw.invalid/v1",
        headers={"x-t": "${run:ticket_id}"},
        trace_id="d" * 32,
        ticket_id=f"tick{ctrl}Injected: bar",
        operation="review-plan",
    )
    with pytest.raises(LLMConfigError):
        _settings(cfg)


def test_configured_mapping_is_not_mutated_by_a_model_call() -> None:
    """AC8: the pinned pydantic-ai Chat path mutates the caller's dict.

    models/openai.py:1001 reads `extra_headers` with NO copy, then :1002 setdefaults
    User-Agent into it — unlike the Responses path (:2300) and anthropic.py:772, which copy.
    Upstream issue 6866, fixed by PR 6868 AFTER this pin. Without a fresh dict per call the
    config's own mapping is polluted and a pydantic-ai version string leaks into the recorded
    header-name set, making a signed verdict's provenance non-deterministic.
    """
    import asyncio

    import httpx

    from rebar.llm.providers import ProviderSession

    configured = {"x-corp-tag": "rebar"}
    cfg = _cfg(base_url="http://gw.invalid/v1", api_key="k", headers=dict(configured))
    resp = {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    class _FT(httpx.AsyncHTTPTransport):
        def __init__(self, *a, **k) -> None:
            pass

        async def handle_async_request(self, request):
            return httpx.Response(200, json=resp)

    import pydantic_ai.models as _pai_models

    orig = httpx.AsyncHTTPTransport
    orig_allow = _pai_models.ALLOW_MODEL_REQUESTS
    httpx.AsyncHTTPTransport = _FT
    _pai_models.ALLOW_MODEL_REQUESTS = True
    try:
        from pydantic_ai import Agent

        with ProviderSession(cfg) as sess:
            agent = Agent(sess.model_for("openai:gpt-4o"))
            # Drive the PRODUCTION assembly seam. Passing a hand-made copy here would
            # test the test's own dict and pass even if the implementation handed over
            # `cfg.headers` itself — the exact tautology this criterion exists to catch.
            asyncio.run(agent.run("hi", model_settings=_settings(cfg)))
    finally:
        httpx.AsyncHTTPTransport = orig
        _pai_models.ALLOW_MODEL_REQUESTS = orig_allow

    assert cfg.headers == configured, "the config's mapping must not gain User-Agent"


def test_configured_header_wins_over_a_client_default() -> None:
    """AC9: merge precedence — the most-regressed behaviour in comparable projects."""
    import asyncio

    import httpx

    from rebar.llm.providers import ProviderSession

    seen: dict[str, str] = {}
    resp = {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    class _FT(httpx.AsyncHTTPTransport):
        def __init__(self, *a, **k) -> None:
            pass

        async def handle_async_request(self, request):
            seen.update(dict(request.headers))
            return httpx.Response(200, json=resp)

    cfg = _cfg(base_url="http://gw.invalid/v1", api_key="k")
    import pydantic_ai.models as _pai_models

    orig = httpx.AsyncHTTPTransport
    orig_allow = _pai_models.ALLOW_MODEL_REQUESTS
    httpx.AsyncHTTPTransport = _FT
    _pai_models.ALLOW_MODEL_REQUESTS = True
    try:
        from pydantic_ai import Agent

        with ProviderSession(cfg) as sess:
            agent = Agent(sess.model_for("openai:gpt-4o"))
            asyncio.run(
                agent.run("hi", model_settings={"extra_headers": {"User-Agent": "rebar-wins"}})
            )
    finally:
        httpx.AsyncHTTPTransport = orig
        _pai_models.ALLOW_MODEL_REQUESTS = orig_allow

    assert seen.get("user-agent") == "rebar-wins"


def test_header_values_never_reach_provenance_or_logs(caplog) -> None:
    """AC10: names are recorded, values never — this lands in SIGNED artifacts."""
    import json as _json

    from rebar.llm.capabilities import provenance_for

    cfg = _cfg(
        base_url="http://gw.invalid/v1",
        headers={"x-corp-secret": "SENTINEL-VALUE-8f2a"},
        trace_id="e" * 32,
        ticket_id="t",
        operation="review-plan",
    )
    with caplog.at_level("DEBUG"):
        _settings(cfg)
    assert "SENTINEL-VALUE-8f2a" not in caplog.text

    rec = provenance_for(
        provider="openai",
        model=_MODEL,
        base_url=cfg.base_url,
        caps=_caps(),
        header_names=sorted(cfg.headers),
    )
    blob = _json.dumps(rec)
    assert "SENTINEL-VALUE-8f2a" not in blob
    assert "x-corp-secret" in blob


def test_runner_stays_within_the_module_size_cap() -> None:
    """AC11: runner.py sits AT the 800-line cap; the provenance_for argument must be paid for."""
    from pathlib import Path

    runner = Path(__file__).resolve().parents[2] / "src" / "rebar" / "llm" / "runner.py"
    assert len(runner.read_text().splitlines()) <= 800
