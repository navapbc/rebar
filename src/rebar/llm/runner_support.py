"""Runner support helpers — a behavior-preserving extraction from ``runner.py``.

A cohesive cluster of small, stateless helpers the ``PydanticAIRunner`` consumes but
that carry no runner state: the per-model tool-capability guard
(``_check_tool_capability`` + its ``_TOOL_CAPABILITY_CHECKED`` cache), the fallback-chain
capability intersection (``_intersect_capabilities``), the answering-candidate reader
(``_answering_model``), the read-only ticket gate (``_readonly_gate``), and success-path
run-shape capture (``_merge_success_run_shape``), and the one-shot sampling-parameter retry
(``with_sampling_retry``). Split out of ``runner.py`` so that module
stays under the module-size cap while ``runner`` keeps re-exporting these names (tests import
them from ``rebar.llm.runner``).

Leaf-friendly: heavy libraries (pydantic-ai) import INSIDE the function that needs them,
never at module top, preserving the ``import rebar.llm`` stdlib-only contract.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from rebar.llm import usage_log
from rebar.llm.capabilities import ModelCapabilities
from rebar.llm.errors import LLMConfigError

logger = logging.getLogger(__name__)

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
    at call time. For the fields that a wrong value makes the run 400, reading the primary's
    capabilities would make the run succeed or fail by luck — a chain containing a model MEASURED
    to reject `temperature` would 400 only once that model answered, a failure reproducing solely
    under provider degradation. So `supports_temperature`, `native_structured_output`,
    `supports_thinking`, `native_output_with_thinking`, and `native_web_search` all fail closed
    with `all(...)`.

    `prompt_cache_style` is the ONE exception, and follows the PRIMARY (bug 96f3): it does NOT
    hard-fail cross-provider. The cache directive is a set of provider-scoped `ModelSettings`
    keys (`bedrock_cache_*` vs `anthropic_cache_*`); `pydantic_ai.settings.merge_model_settings`
    is a plain dict union with no key validation, and each provider model reads ONLY its own
    `*_cache_*` keys via `model_settings.get(...)` — so a directive meant for the primary is a
    silent `.get()` miss on a fallback that actually serves, never an error (the same
    ignored-not-errored behavior LiteLLM documents for an inapplicable cache directive). The
    primary serves virtually every call, so following it restores caching on the hot path while a
    rare fallback runs uncached-but-correct; collapsing to "none" on any disagreement instead
    disabled caching on the primary too. This reverses the earlier conservative collapse — see
    ADR 0059 §7 and the `rebar.toml` [llm.model_classes] note."""
    return ModelCapabilities(
        native_structured_output=all(c.native_structured_output for c in per_candidate),
        prompt_cache_style=per_candidate[0].prompt_cache_style,
        # Follows the primary with `prompt_cache_style`: the two together decide whether the
        # cache directive is sent (bug tardy-coltish-swellfish).
        prompt_caching=per_candidate[0].prompt_caching,
        supports_thinking=all(c.supports_thinking for c in per_candidate),
        # `all` like every sibling: a chain may claim native output UNDER THINKING only when EVERY
        # candidate is MEASURED to support it (story 18ae) — any candidate could answer, so a
        # mixed chain must fail closed to PromptedOutput rather than route native by luck.
        native_output_with_thinking=all(c.native_output_with_thinking for c in per_candidate),
        # `effort` follows the PRIMARY, like `prompt_cache_style`: the settings builder emits it
        # under BOTH provider-scoped keys, and each provider model reads only its own, so the
        # serving candidate receives it whichever leg answers. Fallbacks are same-family rows.
        effort=per_candidate[0].effort,
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


def _merge_success_run_shape(
    usage: dict,
    run_messages: list[Any],
    *,
    request_limit: int,
    tool_calls_limit: int,
    call_label: str,
) -> None:
    """Merge the SUCCESS-path run shape into ``usage`` in place (bug aec1).

    Records how the agent loop actually went on a clean run (the distinct-vs-total tool-call
    ratio that separates a LOOP from genuine BREADTH), reusing the SAME reducer the failure
    path uses. Only ``shape_only`` keys are merged — the reducer's token totals are a
    message-derived APPROXIMATION and would corrupt the authoritative billable figures
    ``_extract_usage`` already placed in ``usage``. Telemetry must never break the call path,
    so any reducer failure is swallowed with a warning.
    """
    try:
        shape = usage_log.run_shape(
            run_messages, request_limit=request_limit, tool_calls_limit=tool_calls_limit
        )
        usage.update(usage_log.shape_only(shape))
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the call path
        logger.warning("usage-log: run-shape capture failed for op=%s: %s", call_label, exc)


# ── Sampling-parameter retry (bug tapestried-oversize-blackfly) ─────────────────────────────
# Settings keys pydantic-ai maps onto a provider's sampling fields (Bedrock Converse
# `inferenceConfig.temperature`/`topP`, Anthropic `temperature`/`top_p`/`top_k`).
_SAMPLING_SETTINGS: tuple[str, ...] = ("temperature", "top_p", "top_k")


def _without_sampling(model_settings: Any) -> Any:
    if not model_settings or not any(k in model_settings for k in _SAMPLING_SETTINGS):
        return model_settings
    return {k: v for k, v in model_settings.items() if k not in _SAMPLING_SETTINGS}


def with_sampling_retry(model: Any) -> Any:
    """Wrap a pydantic-ai ``Model`` so a 400 naming a sampling parameter is retried ONCE.

    Sampling is opt-in (``capabilities.supports_sampling``), so only a model measured to accept
    it is ever sent one. If such a model starts rejecting it, the wrapper records the model for
    the process lifetime (``capabilities.record_sampling_rejected``), logs one WARNING, and
    re-sends the same request without ``temperature``/``top_p``/``top_k``; every later request
    to that model — in this run and later runs — goes without them. A second rejection, or any
    other failure, propagates unchanged to the runner's failure translation. Only a model that
    may be sent sampling is wrapped: every other model never receives a sampling parameter, so
    it — and a non-``Model`` (a lazy provider string) — is returned as-is."""
    from rebar.llm.capabilities import supports_sampling

    if not callable(getattr(model, "request", None)):
        return model
    if not supports_sampling(getattr(model, "model_name", None)):
        return model
    return _sampling_retry_class()(model)


@functools.cache
def _sampling_retry_class() -> Any:
    from pydantic_ai.models.wrapper import WrapperModel

    from rebar.llm.capabilities import record_sampling_rejected, supports_sampling
    from rebar.llm.failure import sampling_parameter_rejected

    class SamplingRetryModel(WrapperModel):
        # ``WrapperModel`` leaves these base ``Model`` defaults in place; forward them so the
        # wrapper stays transparent (same as ``tracing._TracedCandidateModel``).
        @property
        def base_url(self) -> Any:
            return self.wrapped.base_url

        @property
        def label(self) -> Any:
            return self.wrapped.label

        @property
        def model_id(self) -> Any:
            return self.wrapped.model_id

        async def request(
            self, messages: Any, model_settings: Any, model_request_parameters: Any
        ) -> Any:
            model_id = self.wrapped.model_name
            if not supports_sampling(model_id):
                model_settings = _without_sampling(model_settings)
            try:
                return await super().request(messages, model_settings, model_request_parameters)
            except Exception as exc:
                stripped = _without_sampling(model_settings)
                param = sampling_parameter_rejected(exc)
                if stripped is model_settings or param is None:
                    raise
                record_sampling_rejected(model_id)
                logger.warning(
                    "llm: model=%s rejected the %r sampling parameter; retrying once without "
                    "sampling and sending none to this model for the rest of this process",
                    model_id,
                    param,
                )
                return await super().request(messages, stripped, model_request_parameters)

    return SamplingRetryModel
