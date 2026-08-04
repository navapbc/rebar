"""The shared-prefix cache breakpoint + the write-every-call-never-read diagnostic (bug 1dbe).

Two remedies, both pinned OFFLINE (no live model):

* **Remedy 1 (telemetry)** — :func:`cache_write_never_read` /
  :func:`warn_if_cache_write_never_read` flag the run shape the ticket measured: every caching
  call WROTE the cache and NONE ever READ it, which the per-call ``warn_if_cache_ineffective``
  (write==0 AND read==0) could never see. This is the ticket's AC 4.

* **Remedy 2 (breakpoint relocation)** — a ``RunRequest.cache_prefix`` runner-seam knob moves
  the provider cache breakpoint to the END of the byte-identical ``shared_plan_prefix`` so the
  Pass-1 finder's WRITE is READ by the Pass-2 verifier within one run. The breakpoint boundary
  is verified against REAL pydantic-ai ``_map_message`` for BOTH the anthropic and bedrock
  cache styles — so if a pydantic-ai upgrade ever moved the mark off the prefix, this goes RED
  (the loud regression the ticket asks for). This replaces the unsatisfiable "byte-identical
  across TICKETS" AC (the marked prefix embeds the plan by design — see the ticket's Phase-1
  investigation (b).2/(c).3).
"""

from __future__ import annotations

import asyncio

import pytest

from rebar.llm.structured_run import (
    cache_write_never_read,
    warn_if_cache_write_never_read,
)

pytestmark = pytest.mark.unit


def _row(write: int, read: int, op: str = "plan-reviewer") -> dict:
    return {"op": op, "cache_write_tokens": write, "cache_read_tokens": read, "input_tokens": 9000}


# ── Remedy 1: the write-every-call-never-read aggregate diagnostic ────────────────
def test_write_every_call_never_read_is_flagged() -> None:
    # The exact bug-1dbe shape: three passes each WROTE thousands, every read 0.
    rows = [_row(2800, 0), _row(1545, 0), _row(4871, 0)]
    assert cache_write_never_read(rows) is True


def test_a_single_write_is_not_flagged() -> None:
    # One caching call is the legitimate first write of a warm-then-reuse sequence.
    assert cache_write_never_read([_row(2800, 0)]) is False


def test_any_read_clears_the_flag() -> None:
    # If even one call READ the cache, the run is reusing a prefix — not the pathology.
    rows = [_row(2800, 0), _row(0, 2800), _row(1545, 0)]
    assert cache_write_never_read(rows) is False


def test_non_caching_rows_are_out_of_scope() -> None:
    # Rows that neither wrote nor read (sub-floor passes) are ignored; with no caching calls
    # the write==0/read==0 predicate owns the case, not this one.
    assert cache_write_never_read([_row(0, 0), _row(0, 0)]) is False


def test_warn_fires_and_names_the_totals(caplog) -> None:
    rows = [_row(2800, 0), _row(4871, 0)]
    with caplog.at_level("WARNING"):
        warn_if_cache_write_never_read(rows, model="bedrock/claude")
    msg = caplog.text
    assert "WROTE on every" in msg
    assert "7671" in msg  # 2800 + 4871 premium-rate write tokens, all unread


def test_warn_silent_on_healthy_run(caplog) -> None:
    with caplog.at_level("WARNING"):
        warn_if_cache_write_never_read([_row(2800, 0), _row(0, 2800)], model="m")
    assert "WROTE on every" not in caplog.text


# ── Remedy 2: the breakpoint lands at the shared-prefix boundary ──────────────────
_PREFIX = "SHARED PLAN PREFIX bytes (identical across passes)\n\n"
_SUFFIX = "PASS-SPECIFIC stance suffix"


def _build_agent_kwargs(cache_prefix: str | None):
    """Drive the real ``agent_call.build_agent_kwargs`` with a minimal RunRequest."""
    from types import SimpleNamespace

    from rebar.llm.agent_call import build_agent_kwargs
    from rebar.llm.runner import RunRequest

    req = RunRequest(
        system_prompt=_PREFIX + _SUFFIX,
        instructions="user rubric",
        config=SimpleNamespace(llm_tool_timeout_s=30),  # only this field is read here
        cache_prefix=cache_prefix,
    )
    cfg = SimpleNamespace(llm_tool_timeout_s=30)
    return build_agent_kwargs(cfg, req, [], [], model_settings=None, web_caps=None)


def _mapped_system(kwargs, settings):
    """Feed ``kwargs`` through a REAL pydantic-ai Agent and capture the mapped system blocks.

    Building the Agent with the same ``system_prompt`` + (function) ``instructions``
    build_agent_kwargs produced, then inspecting what the model actually sends, is the only
    faithful check that the cache_control lands where we claim."""
    from pydantic_ai import Agent, models

    captured: dict = {}

    model = _make_model(settings)
    # anthropic exposes ``_map_message``; bedrock ``_map_messages`` — spy whichever exists.
    method_name = "_map_message" if hasattr(model, "_map_message") else "_map_messages"
    orig = getattr(model, method_name)

    async def _spy(messages, mrp, ms):
        captured["sp"] = await orig(messages, mrp, ms)
        return captured["sp"]

    setattr(model, method_name, _spy)
    agent = Agent(
        model,
        system_prompt=kwargs["system_prompt"],
        instructions=kwargs.get("instructions"),
        model_settings=settings,
    )
    # A fresh event loop: ``run_sync`` acquires ``get_event_loop()``, which a prior test may
    # have left closed (the async anthropic client path needs a live loop to even reach the
    # message mapping). We only need the mapped request, not the (401) HTTP call.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    prev_allow = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = True  # the mapping runs before the (blocked) HTTP call
    try:
        agent.run_sync("user rubric")
    except Exception:  # noqa: BLE001 — no real API key; the mapped request is already captured
        pass
    finally:
        models.ALLOW_MODEL_REQUESTS = prev_allow
        loop.close()
        asyncio.set_event_loop(None)
    sp = captured.get("sp")
    # ``_map_message(s)`` returns ``(system_blocks, messages)`` — we want the system blocks.
    return sp[0] if isinstance(sp, tuple) else sp


def _make_model(settings):
    style = "bedrock" if "bedrock_cache_instructions" in settings else "anthropic"
    if style == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel("claude-3-5-sonnet-latest", provider=AnthropicProvider(api_key="x"))
    from pydantic_ai.models.bedrock import BedrockConverseModel
    from pydantic_ai.providers.bedrock import BedrockProvider

    return BedrockConverseModel(
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        provider=BedrockProvider(
            region_name="us-east-1",
            aws_access_key_id="x",
            aws_secret_access_key="y",
        ),
    )


def _anthropic_settings():
    from rebar.llm.capabilities import ModelCapabilities, cache_settings_for

    caps = ModelCapabilities(
        native_structured_output=True, prompt_cache_style="anthropic", supports_thinking=False
    )
    return cache_settings_for(caps, execution_mode="single_turn")


def _bedrock_settings():
    from rebar.llm.capabilities import ModelCapabilities, cache_settings_for

    caps = ModelCapabilities(
        native_structured_output=True, prompt_cache_style="bedrock", supports_thinking=False
    )
    return cache_settings_for(caps, execution_mode="single_turn")


def _cache_block_text(system_blocks, *, key: str) -> str | None:
    """The text of the block carrying the cache marker (``cache_control``/``cachePoint``)."""
    if not isinstance(system_blocks, list):
        return None
    marked = None
    for block in system_blocks:
        if key == "cache_control" and block.get("cache_control"):
            marked = block.get("text")
        if key == "cachePoint" and block.get("cachePoint"):
            # bedrock emits a standalone cachePoint block; the marked span is everything before.
            break
        if key == "cachePoint":
            marked = block.get("text")
    return marked


def test_anthropic_breakpoint_at_prefix_boundary() -> None:
    kwargs = _build_agent_kwargs(_PREFIX)
    assert kwargs["system_prompt"] == _PREFIX
    assert callable(kwargs["instructions"])
    settings = _anthropic_settings()
    system = _mapped_system(kwargs, settings)
    # The MARKED (cached) block is exactly the shared prefix — NOT the whole system.
    assert _cache_block_text(system, key="cache_control") == _PREFIX
    # And the model still receives prefix + suffix, byte-identical to the un-split system.
    assert "".join(b["text"] for b in system) == _PREFIX + _SUFFIX


def test_bedrock_breakpoint_at_prefix_boundary() -> None:
    kwargs = _build_agent_kwargs(_PREFIX)
    settings = _bedrock_settings()
    system = _mapped_system(kwargs, settings)
    assert isinstance(system, list)
    # bedrock: a standalone cachePoint block sits AFTER the prefix text block.
    texts_before_cachepoint = []
    for block in system:
        if isinstance(block, dict) and "cachePoint" in block:
            break
        texts_before_cachepoint.append(block.get("text", ""))
    assert "".join(texts_before_cachepoint) == _PREFIX
    # Full content preserved (prefix + suffix), only the cache boundary moved.
    all_text = "".join(b.get("text", "") for b in system if isinstance(b, dict) and "text" in b)
    assert all_text == _PREFIX + _SUFFIX


def test_no_cache_prefix_leaves_single_system_block() -> None:
    # The DEFAULT (no cache_prefix): one system block, marked at its END — unchanged behavior.
    kwargs = _build_agent_kwargs(None)
    assert kwargs["system_prompt"] == _PREFIX + _SUFFIX
    assert "instructions" not in kwargs


def test_cache_prefix_ignored_when_not_a_proper_prefix() -> None:
    # A cache_prefix that is not actually the leading bytes is a no-op (fail safe).
    kwargs = _build_agent_kwargs("SOMETHING ELSE ENTIRELY")
    assert kwargs["system_prompt"] == _PREFIX + _SUFFIX
    assert "instructions" not in kwargs


def _req(cache_prefix, *, system=_PREFIX + _SUFFIX):
    """A minimal RunRequest exercising only ``effective_cache_prefix``."""
    from types import SimpleNamespace

    from rebar.llm.runner import RunRequest

    return RunRequest(
        system_prompt=system,
        instructions="user rubric",
        config=SimpleNamespace(llm_tool_timeout_s=30),
        cache_prefix=cache_prefix,
    )


def test_effective_cache_prefix_is_the_single_validity_predicate() -> None:
    # The one shared guard (bug 1dbe): agent_call, the marked-prefix estimate, and the
    # workflow request builder all defer to this method, so it must accept exactly a
    # non-empty PROPER prefix and reject every no-op shape.
    assert _req(_PREFIX).effective_cache_prefix() == _PREFIX  # proper prefix -> relocate
    assert _req(None).effective_cache_prefix() is None  # unset
    assert _req("").effective_cache_prefix() is None  # empty
    assert _req("SOMETHING ELSE").effective_cache_prefix() is None  # not a leading segment
    # Full-length (== whole system prompt) is NOT a proper prefix: nothing to split off.
    assert _req(_PREFIX + _SUFFIX).effective_cache_prefix() is None
