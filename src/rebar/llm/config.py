"""Configuration + backend detection for the rebar LLM agent-operations framework.

``LLMConfig`` is a plain dataclass resolved from the environment (and explicit
overrides). It is **stdlib-only** — importing it never pulls langchain/langfuse/
anthropic — so ``import rebar.llm`` stays dependency-free; the heavy libraries are
imported lazily by the runner only when an operation actually runs.

Environment variables (all optional; sensible defaults):

  REBAR_LLM_RUNNER        in-process backend: ``langgraph`` (default) | ``langflow``
  REBAR_LLM_MODEL         model id (default ``claude-opus-4-8``)
  REBAR_LLM_MAX_TOKENS    per-response token ceiling (default 8000)
  REBAR_LLM_MAX_ITERS     agent-loop recursion/iteration cap (default 25)
  REBAR_LLM_TIMEOUT       per-operation wall-clock seconds (default 600)
  REBAR_LLM_REPO_PATH     repo root the agent's read-only file tools see (default: repo root)
  REBAR_LLM_MCP_SERVERS   JSON object of MCP servers (langchain-mcp-adapters shape)
  ANTHROPIC_API_KEY       model credentials (required to actually run langgraph)
  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST   tracing + prompts (optional)
  LANGFLOW_URL / LANGFLOW_API_KEY   Langflow deployment (only for the langflow runner)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib.util import find_spec

from rebar import config as _root_config

DEFAULT_MODEL = "claude-opus-4-8"
RUNNERS = ("langgraph", "langflow", "fake")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class LangfuseConfig:
    """Langfuse credentials/host, plus whether tracing+prompts are *enabled*.

    Enabled is derived purely from key-presence (both keys set) — the runner gates
    on this BEFORE constructing any handler, the documented no-op pattern (a stale
    handler that tries to flush with no keys is the common footgun)."""

    public_key: str | None = None
    secret_key: str | None = None
    host: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self.secret_key)

    @classmethod
    def from_env(cls) -> LangfuseConfig:
        return cls(
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY") or None,
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY") or None,
            host=os.environ.get("LANGFUSE_HOST") or None,
        )


@dataclass
class LLMConfig:
    runner: str = "langgraph"
    model: str = DEFAULT_MODEL
    # Provider is OPTIONAL: LangChain's init_chat_model infers it from the model
    # name (claude-*→anthropic, gpt-*→openai, gemini-*→google_genai). Set it
    # explicitly for ambiguous names or OpenAI-compatible local servers
    # (LMStudio/Ollama/vLLM: model_provider="openai" + base_url).
    model_provider: str | None = None
    base_url: str | None = None  # OpenAI-compatible endpoint (local models)
    api_key: str | None = None  # explicit key (e.g. a dummy key for local servers)
    max_tokens: int = 8000
    max_iterations: int = 25
    timeout_s: int = 600
    repo_path: str | None = None
    mcp_servers: dict = field(default_factory=dict)
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)
    langflow_url: str | None = None
    langflow_api_key: str | None = None

    @classmethod
    def from_env(cls, *, repo_root=None) -> LLMConfig:
        runner = (os.environ.get("REBAR_LLM_RUNNER") or "langgraph").strip().lower()
        if runner not in RUNNERS:
            runner = "langgraph"
        mcp_raw = os.environ.get("REBAR_LLM_MCP_SERVERS")
        mcp_servers: dict = {}
        if mcp_raw:
            try:
                parsed = json.loads(mcp_raw)
                if isinstance(parsed, dict):
                    mcp_servers = parsed
            except json.JSONDecodeError:
                mcp_servers = {}
        repo_path = os.environ.get("REBAR_LLM_REPO_PATH") or str(
            _root_config.repo_root(repo_root)
        )
        return cls(
            runner=runner,
            model=(os.environ.get("REBAR_LLM_MODEL") or DEFAULT_MODEL).strip(),
            model_provider=(os.environ.get("REBAR_LLM_MODEL_PROVIDER") or "").strip() or None,
            base_url=os.environ.get("REBAR_LLM_BASE_URL") or None,
            api_key=os.environ.get("REBAR_LLM_API_KEY") or None,
            max_tokens=_env_int("REBAR_LLM_MAX_TOKENS", 8000),
            max_iterations=_env_int("REBAR_LLM_MAX_ITERS", 25),
            timeout_s=_env_int("REBAR_LLM_TIMEOUT", 600),
            repo_path=repo_path,
            mcp_servers=mcp_servers,
            langfuse=LangfuseConfig.from_env(),
            langflow_url=os.environ.get("LANGFLOW_URL") or None,
            langflow_api_key=os.environ.get("LANGFLOW_API_KEY") or None,
        )


def _module_available(name: str) -> bool:
    """True if an import-able module is installed, without importing it."""
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def available_backends() -> dict:
    """Diagnostic snapshot of what's installed/configured — drives clear errors
    and the ``rebar review --check`` surface. Pure detection (no heavy imports).
    """
    return {
        "langchain": _module_available("langchain") and _module_available("langgraph"),
        # model-provider integrations (langchain-anthropic ships with the extra;
        # the others are opt-in installs for OpenAI / Gemini).
        "provider_anthropic": _module_available("langchain_anthropic"),
        "provider_openai": _module_available("langchain_openai"),
        "provider_google": _module_available("langchain_google_genai"),
        "langchain_mcp_adapters": _module_available("langchain_mcp_adapters"),
        "langfuse": _module_available("langfuse"),
        "anthropic_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_api_key": bool(os.environ.get("OPENAI_API_KEY")),
        "langfuse_configured": LangfuseConfig.from_env().enabled,
        "langflow_url": bool(os.environ.get("LANGFLOW_URL")),
    }


def agents_extra_installed() -> bool:
    """True when the ``nava-rebar[agents]`` extra is importable (the langgraph path,
    default Anthropic provider). Other providers (OpenAI/Gemini) are opt-in extras."""
    b = available_backends()
    return b["langchain"] and b["provider_anthropic"] and b["langchain_mcp_adapters"]
