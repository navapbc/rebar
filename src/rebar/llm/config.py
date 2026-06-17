"""Configuration + backend detection for the rebar LLM agent-operations framework.

``LLMConfig`` is a plain dataclass resolved from the environment (and explicit
overrides). It is **stdlib-only** — importing it never pulls langchain/langfuse/
anthropic — so ``import rebar.llm`` stays dependency-free; the heavy libraries are
imported lazily by the runner only when an operation actually runs.

Environment variables (all optional; sensible defaults):

  REBAR_LLM_EXPERIMENTAL_HARNESS  set to ``deepagents`` to opt into the experimental
                          deepagents harness. Otherwise the runner is DERIVED: langflow
                          iff LANGFLOW_URL+LANGFLOW_FLOW_ID are set, else in-process
                          langgraph (the default). ``fake`` is test-only (library arg).
  REBAR_LLM_MODEL         model id (default ``claude-opus-4-8``)
  REBAR_LLM_MAX_TOKENS    per-response token ceiling (default 8000)
  REBAR_LLM_MAX_STEPS     Max agent loop steps before abort (~2 per tool call; default
                          25 ~= 12 tool calls). Deprecated alias: REBAR_LLM_MAX_ITERS.
  REBAR_LLM_TIMEOUT       per-operation wall-clock seconds (default 600)
  REBAR_LLM_REPO_PATH     repo root the agent's read-only file tools see (default: repo root)
  REBAR_LLM_MCP_SERVERS   JSON object of MCP servers (langchain-mcp-adapters shape)
  ANTHROPIC_API_KEY       model credentials (required to actually run langgraph)
  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST   tracing + prompts (optional)
  LANGFLOW_URL / LANGFLOW_FLOW_ID / LANGFLOW_API_KEY   Langflow deployment (langflow runner)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib.util import find_spec

from rebar import config as _root_config

DEFAULT_MODEL = "claude-opus-4-8"
# Execution backends. `langgraph` is the default for review; `deepagents` is an
# opt-in harness (planning/subagents/eviction) intended mainly for future
# task types — review stays on langgraph. `langflow` is a REST stub; `fake` is
# the offline test seam.
RUNNERS = ("langgraph", "deepagents", "langflow", "fake")

# Model-name prefix → provider, mirroring LangChain init_chat_model inference (used
# for diagnostics + clear errors; init_chat_model does the authoritative dispatch).
_PROVIDER_PREFIXES = (
    ("claude", "anthropic"),
    ("gpt-", "openai"),
    ("gpt4", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("chatgpt", "openai"),
    ("gemini", "google_genai"),
)
# provider → the LangChain integration package a client project must install.
PROVIDER_PACKAGES = {
    "anthropic": "langchain-anthropic",
    "openai": "langchain-openai",
    "google_genai": "langchain-google-genai",
}


def infer_provider(model: str, explicit: str | None = None) -> str | None:
    """Resolve the provider for a model: an explicit setting, a ``provider:model``
    prefix, or inference from the model name. Returns None if undeterminable."""
    if explicit:
        return explicit
    if ":" in model:
        return model.split(":", 1)[0]
    low = model.lower()
    for prefix, provider in _PROVIDER_PREFIXES:
        if low.startswith(prefix):
            return provider
    return None


def denied_paths(root: str) -> tuple[str, ...]:
    """Realpaths the agent must never read OR cite: git internals, reconciler
    state, and the live event store — resolved from rebar.config.tracker_dir(root)
    so the REBAR_TRACKER_DIR override (deprecated alias TICKETS_TRACKER_DIR; a
    relocated/renamed store) is covered too.
    Shared by the file tools (read) and citation resolution (output) so neither can
    leak internal state."""
    candidates = [
        os.path.join(root, ".git"),
        os.path.join(root, ".bridge_state"),
        os.path.join(root, ".tickets-tracker"),
    ]
    try:
        candidates.append(str(_root_config.tracker_dir(root)))
    except Exception:
        pass
    return tuple(dict.fromkeys(os.path.realpath(p) for p in candidates))


def is_denied(abs_path: str, denied: tuple[str, ...]) -> bool:
    return any(abs_path == d or abs_path.startswith(d + os.sep) for d in denied)


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


def _env_int_aliased(name: str, legacy: str, default: int) -> int:
    """:func:`_env_int` for ``name``, honoring a deprecated ``legacy`` env var (with
    a one-time-ish warning) when the canonical name is unset."""
    if name not in os.environ and legacy in os.environ:
        import logging

        logging.getLogger("rebar.llm.config").warning("%s is deprecated; use %s", legacy, name)
        name = legacy
    return _env_int(name, default)


# ── [tool.rebar.llm] config-file layer (0ac6 slice 4) ─────────────────────────
#
# llm.* is resolved HERE (not the stdlib-core typed Config) so importing rebar.llm
# never pulls the agents stack into core. The non-secret, non-runtime, non-derived
# knobs are settable in a ``[tool.rebar.llm]`` table (pyproject / rebar.toml [llm] /
# legacy .rebar/config.conf llm.* / XDG user config), read via the core loader's
# discovery so file LOCATIONS + precedence match the rest of rebar. Resolution per
# key: ``rebar -c llm.KEY=VALUE`` (CLI) > ``REBAR_LLM_<KEY>`` env > config file >
# default. Secrets (REBAR_LLM_API_KEY / ANTHROPIC/OPENAI keys / LANGFUSE_* /
# LANGFLOW_API_KEY), the runtime-only REBAR_LLM_REPO_PATH, and the DERIVED runner
# stay env-only and are NOT config-file keys.


def _read_llm_file_table(repo_root=None) -> dict:
    """The merged ``[tool.rebar.llm]`` table (user < project), or ``{}``. A malformed
    core config degrades to env-only — a broken pyproject must never break an LLM op."""
    try:
        return _root_config.read_reserved_section("llm", repo_root)
    except _root_config.ConfigError:
        return {}


def _llm_str(table: dict, cli: dict, env_name: str, file_key: str, default):
    """Resolve a string setting: CLI > env > file > default (blank → fall through)."""
    if file_key in cli and str(cli[file_key]).strip():
        return str(cli[file_key]).strip()
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip()
    fv = table.get(file_key)
    if fv is not None and str(fv).strip():
        return str(fv).strip()
    return default


def _llm_int(table: dict, cli: dict, env_name: str, file_key: str, default: int, *, legacy=None):
    """Resolve an int setting: CLI > env (canonical, then deprecated ``legacy``) > file
    > default. An unparseable higher layer falls through to the next."""
    candidates: list = []
    if file_key in cli:
        candidates.append(cli[file_key])
    env_raw = os.environ.get(env_name)
    if (env_raw is None or not env_raw.strip()) and legacy and os.environ.get(legacy, "").strip():
        import logging

        logging.getLogger("rebar.llm.config").warning("%s is deprecated; use %s", legacy, env_name)
        env_raw = os.environ.get(legacy)
    if env_raw is not None and env_raw.strip():
        candidates.append(env_raw)
    fv = table.get(file_key)
    if fv is not None and not isinstance(fv, bool):
        candidates.append(fv)
    for c in candidates:
        try:
            return int(str(c).strip())
        except (TypeError, ValueError):
            continue
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
    langflow_flow_id: str | None = None

    @classmethod
    def from_env(cls, *, repo_root=None) -> LLMConfig:
        # The runner is DERIVED, not a public env knob (EV-4): the experimental
        # deepagents harness is an explicit opt-in; otherwise langflow is used iff a
        # Langflow deployment is fully configured, else the in-process langgraph
        # default. The ``fake`` runner is test-only — reachable via the library
        # ``runner=``/``override=`` arg, never from the environment.
        if os.environ.get("REBAR_LLM_EXPERIMENTAL_HARNESS", "").strip().lower() == "deepagents":
            runner = "deepagents"
        elif os.environ.get("LANGFLOW_URL") and os.environ.get("LANGFLOW_FLOW_ID"):
            runner = "langflow"
        else:
            runner = "langgraph"
        # Config-file layer for the non-secret knobs ([tool.rebar.llm]); env (and
        # `rebar -c llm.*`) override it. Secrets/runtime/derived values stay env-only.
        table = _read_llm_file_table(repo_root)
        cli = _root_config.cli_overrides_for("llm")

        # mcp_servers: env JSON > rebar -c llm.mcp_servers=<json> > file table/JSON.
        mcp_servers: dict = {}
        mcp_raw = cli.get("mcp_servers") or os.environ.get("REBAR_LLM_MCP_SERVERS")
        if mcp_raw:
            try:
                parsed = json.loads(mcp_raw)
                if isinstance(parsed, dict):
                    mcp_servers = parsed
            except json.JSONDecodeError:
                mcp_servers = {}
        else:
            file_mcp = table.get("mcp_servers")
            if isinstance(file_mcp, dict):
                mcp_servers = file_mcp
            elif isinstance(file_mcp, str):
                try:
                    parsed = json.loads(file_mcp)
                    if isinstance(parsed, dict):
                        mcp_servers = parsed
                except json.JSONDecodeError:
                    mcp_servers = {}
        # repo_path is a RUNTIME-only override (env only) — not a [tool.rebar.llm] key.
        repo_path = os.environ.get("REBAR_LLM_REPO_PATH") or str(_root_config.repo_root(repo_root))
        return cls(
            runner=runner,
            model=_llm_str(table, cli, "REBAR_LLM_MODEL", "model", DEFAULT_MODEL),
            model_provider=_llm_str(table, cli, "REBAR_LLM_MODEL_PROVIDER", "model_provider", None),
            base_url=_llm_str(table, cli, "REBAR_LLM_BASE_URL", "base_url", None),
            api_key=os.environ.get("REBAR_LLM_API_KEY") or None,
            max_tokens=_llm_int(table, cli, "REBAR_LLM_MAX_TOKENS", "max_tokens", 8000),
            max_iterations=_llm_int(
                table, cli, "REBAR_LLM_MAX_STEPS", "max_steps", 25, legacy="REBAR_LLM_MAX_ITERS"
            ),
            timeout_s=_llm_int(table, cli, "REBAR_LLM_TIMEOUT", "timeout", 600),
            repo_path=repo_path,
            mcp_servers=mcp_servers,
            langfuse=LangfuseConfig.from_env(),
            langflow_url=os.environ.get("LANGFLOW_URL") or None,
            langflow_api_key=os.environ.get("LANGFLOW_API_KEY") or None,
            langflow_flow_id=os.environ.get("LANGFLOW_FLOW_ID") or None,
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
        "deepagents": _module_available("deepagents"),
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
