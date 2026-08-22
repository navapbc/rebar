"""Runner support helpers — a behavior-preserving extraction from ``runner.py``.

A cohesive cluster of small, stateless helpers the ``PydanticAIRunner`` consumes but
that carry no runner state: the per-model tool-capability guard
(``_check_tool_capability`` + its ``_TOOL_CAPABILITY_CHECKED`` cache), the fallback-chain
capability intersection (``_intersect_capabilities``), the answering-candidate reader
(``_answering_model``), and the read-only ticket gate (``_readonly_gate``). Split out of
``runner.py`` so that module stays under the module-size cap while ``runner`` keeps
re-exporting these names (tests import them from ``rebar.llm.runner``).

Leaf-friendly: heavy libraries (pydantic-ai) import INSIDE the function that needs them,
never at module top, preserving the ``import rebar.llm`` stdlib-only contract.
"""

from __future__ import annotations

from typing import Any

from rebar.llm.capabilities import ModelCapabilities
from rebar.llm.errors import LLMConfigError

# Agent-build invariants (story sorry-clay-anole) — static guards, checked ONCE per model,
# never per call.
_TOOL_CAPABILITY_CHECKED: set[str] = set()


def _check_tool_capability(model, resolved: str) -> None:
    """Fail fast if a tool-using op is about to run on a model that does NOT support tool
    calling (pydantic-ai #6186 silently drops the tools). Reads the CONCRETE, verified
    signal ``model.profile.supports_tools`` (a bool on the AnthropicModel object). Cached per
    resolved model string — a hot loop pays nothing. Defensive: a missing/None profile is a
    safe skip (True/None passes; only an explicit False raises)."""
    if resolved in _TOOL_CAPABILITY_CHECKED:
        return
    _TOOL_CAPABILITY_CHECKED.add(resolved)
    supports = getattr(getattr(model, "profile", None), "supports_tools", None)
    if supports is False:
        raise LLMConfigError(
            f"model {resolved!r} does not support tool calling, but a tool-using gate op "
            "would silently drop its tools — choose a tool-calling model/provider"
        )


def _intersect_capabilities(per_candidate: list[ModelCapabilities]) -> ModelCapabilities:
    """The CONSERVATIVE capability record for a fallback chain: a capability holds only if EVERY
    candidate has it (task cc33).

    Any candidate may be the one that answers, and which one that is depends on provider health
    at call time. Reading the primary's capabilities would make the run succeed or fail by luck —
    a chain containing a model MEASURED to reject `temperature` would 400 only once that model
    answered, a failure reproducing solely under provider degradation. Disagreeing prompt-cache
    styles collapse to "none" for the same reason: each style's keys are provider-specific and
    would error on the candidate that does not share it."""
    styles = {caps.prompt_cache_style for caps in per_candidate}
    return ModelCapabilities(
        native_structured_output=all(c.native_structured_output for c in per_candidate),
        prompt_cache_style=per_candidate[0].prompt_cache_style if len(styles) == 1 else "none",
        supports_thinking=all(c.supports_thinking for c in per_candidate),
        # `all` like every sibling: a chain may claim native output UNDER THINKING only when EVERY
        # candidate is MEASURED to support it (story 18ae) — any candidate could answer, so a
        # mixed chain must fail closed to PromptedOutput rather than route native by luck.
        native_output_with_thinking=all(c.native_output_with_thinking for c in per_candidate),
        supports_temperature=all(c.supports_temperature for c in per_candidate),
        # `all`, like every other field: web access is attached to the chain as a WHOLE, so the
        # record may only claim the provider-side route when EVERY candidate can serve it —
        # otherwise the answering model decides the route and provenance would attest by luck.
        native_web_search=all(c.native_web_search for c in per_candidate),
    )


def _answering_model(messages: list[Any], candidates: list[str]) -> str | None:
    """The candidate that actually produced the run's final response, or ``None``.

    Read from ``ModelResponse.model_name`` — the model that answered — never from
    ``FallbackModel.model_name``, which is the synthetic combined ``fallback:a,b`` string and
    would attest a model that never ran. The bare name is mapped back onto the provider-qualified
    candidate so provenance keeps naming a provider, and an unrecognized name is returned as-is
    rather than dropped (an unattributable answer is still better evidence than the primary)."""
    from pydantic_ai.messages import ModelResponse

    for message in reversed(messages):
        name = getattr(message, "model_name", None) if isinstance(message, ModelResponse) else None
        if name:
            return next((c for c in candidates if c.rpartition(":")[2] == name), name)
    return None


def _readonly_gate() -> bool:
    """True if the READONLY gate is set — reused to withhold the comment tool, so a
    read-only deployment grants the agent read-only ticket access.

    Resolves the SAME config-aware way as the MCP server's write-tool gate: env
    ``REBAR_MCP_READONLY`` wins over the ``[tool.rebar.mcp] readonly`` file key, and a
    malformed config raises ``ConfigError`` out of the shared resolver (operator ruling
    39f8-ae7c: an unreadable config is an error, never a silent policy value — the
    pre-ruling posture failed CLOSED here, which safely withheld the write but also let
    the fault masquerade as configured read-only). Previously this read ONLY the env var
    (its own truthy parser) and ignored the file key, so a server set read-only via the
    config FILE alone still handed the review agent a live ``comment_ticket`` write in
    ``source=local`` mode — half-enforced read-only. Both this and ``mcp_server._readonly``
    now route through the one ``rebar.config.mcp_readonly`` resolver so they can't drift.

    Import edge: we call the resolver in ``rebar.config`` (a core LEAF), NOT
    ``mcp_server``. Importing ``mcp_server`` from ``rebar.llm`` would invert the layering
    AND pull the ``mcp`` extra's module-top imports into the LLM runtime, breaking the
    ``import rebar.llm`` optionality contract. The import is kept lazy (inside the
    function) to leave the hot-path module-import graph unchanged."""
    import rebar.config

    return rebar.config.mcp_readonly()
