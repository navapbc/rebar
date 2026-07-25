"""OpenAI-compatible model construction (leaf).

Wires ``REBAR_LLM_BASE_URL`` / ``REBAR_LLM_API_KEY`` through a Pydantic AI
``OpenAIProvider`` so a gate can run against any OpenAI-API-compatible endpoint (a
LiteLLM gateway, vLLM, Ollama, LM Studio, a self-hosted proxy). The runner funnels
through here whenever a ``base_url`` is set; the gateway backend is opaque (LiteLLM
commonly fronts Bedrock/Anthropic), so the runner also forces the tolerant
PromptedOutput path for this model rather than assuming native Structured Outputs.

Pydantic AI's OpenAI provider/model (the opt-in ``openai`` extra) is imported inside the
function that needs it, never at module top, so this module keeps the stdlib-only
``import rebar.llm`` contract. This is a leaf: it imports nothing back from ``runner``.
"""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

_NO_AUTH_PLACEHOLDER = "EMPTY"


def _import_openai_model():
    """Import the Pydantic AI OpenAI model + provider, or raise a clear extra-missing error
    (mirrors ``runner._import_pydantic_ai``): the OpenAI provider is an opt-in extra
    (``pydantic-ai-slim[openai]``) kept out of the lean default ``agents`` extra."""
    try:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError as exc:
        raise LLMConfigError(
            "an OpenAI-compatible endpoint (REBAR_LLM_BASE_URL) needs the OpenAI provider "
            "extra. Install it with: pip install 'pydantic-ai-slim[openai]'"
        ) from exc
    return OpenAIChatModel, OpenAIProvider


def build_openai_compatible_model(name: str, *, cfg: LLMConfig):
    """Build an ``OpenAIChatModel`` bound to ``cfg.base_url`` / ``cfg.api_key``. ``name`` is
    the bare model id the endpoint expects (the runner strips any ``provider:`` prefix). An
    explicit ``api_key`` is honored; when unset a placeholder is sent so no-auth local
    servers construct cleanly (a hosted gateway answers with its own 401 instead)."""
    OpenAIChatModel, OpenAIProvider = _import_openai_model()
    provider = OpenAIProvider(base_url=cfg.base_url, api_key=cfg.api_key or _NO_AUTH_PLACEHOLDER)
    return OpenAIChatModel(name, provider=provider)
