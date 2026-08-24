"""Configuration + backend detection for the rebar LLM agent-operations framework.

``LLMConfig`` is a plain dataclass resolved from the environment (and explicit
overrides). It is **stdlib-only** — importing it never pulls the agent runtime
(pydantic-ai) or anthropic — so ``import rebar.llm`` stays dependency-free; the
heavy libraries are imported lazily by the runner only when an operation runs.

Environment variables (all optional; sensible defaults):

                          The runner is the provider-agnostic pydantic_ai runtime
                          (``fake`` is test-only, reachable only via the library
                          ``runner=`` arg).
  REBAR_LLM_MAX_TOKENS    per-response token ceiling (default 8000)
  REBAR_LLM_MAX_STEPS     Max agent loop steps before abort (~2 per tool call; default
                          50 ~= 25 tool calls).
  REBAR_LLM_TIMEOUT       per-operation wall-clock seconds (default 600)
  REBAR_LLM_REPO_PATH     repo root the agent's read-only file tools see (default: repo root)
  REBAR_LLM_MCP_SERVERS   JSON object of MCP servers (Pydantic AI MCP toolset shape)
  REBAR_LLM_CONFIG_FILE   path to a TOML file whose ``[llm]`` table LAYERS (deep-merges,
                          per key) over the discovered config — for an environment that
                          needs its own settings without editing the checkout's. Missing
                          path = hard error, never a silent fallback.
  REBAR_LLM_BEDROCK_REGION    AWS region for the Bedrock provider (default: boto3's own
                              region resolution — AWS_REGION/AWS_DEFAULT_REGION/profile).
  ANTHROPIC_API_KEY       model credentials (required to actually run an operation)
  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST   tracing + prompts (optional)
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.util import find_spec

from rebar import config as _root_config

# The gate read-root / snapshot-session domain lives in `gate_context` (ticket b300 moved ~185
# lines out of this file to clear the 800-line cap; see that module's docstring for why the cut
# is inert mass rather than the real absorber). These names are RE-EXPORTED here and this is the
# ONLY import path any consumer under `src/` uses — `from_env` below reads two of them, and
# thirteen `monkeypatch.setattr` targets in the suite name `rebar.llm.config.<name>` and rebind
# THESE globals. Repointing any consumer at `rebar.llm.gate_context` would leave those patches
# applying to a module the consumer no longer reads: the tests would pass while asserting nothing.
from rebar.llm.gate_context import (  # noqa: F401  (re-export: see above)
    _active_code_root,
    _active_tickets_root,
    _in_gate_session,
    assert_gated,
    current_code_root,
    current_code_sha,
    current_tickets_root,
    gate_session,
    in_gate_session,
    resolve_code_root,
    use_code_root,
    use_tickets_root,
)

DEFAULT_MODEL = "claude-opus-4-8"
# The decisive non-frontier model used by the gate VERIFIERS (plan-review Pass-2 verify and
# the completion verifier) when the operator has NOT explicitly chosen a model (i.e.
# cfg.model == DEFAULT_MODEL). A focused yes/no verification is a decisive, non-open-ended
# judgement, so a cheaper/faster model is sufficient; an explicit operator model still wins.
# Single source of truth — imported by both completion.py and plan_review (no duplication).
VERIFIER_DEFAULT_MODEL = "claude-sonnet-4-6"


# The active gate RUN config (epic veiny-trout-brink). The run boundary (`produce_*` in
# gate_dispatch) resolves the caller's `LLMConfig` ONCE — honoring an explicit `config=` — and
# sets this for the duration of the workflow run, so every gate op (and the non-step
# ProductionBatchRunner) reads the SAME resolved config instead of re-deriving it from the
# environment per op. This is the model/runner identity the verdict reports, fixing the
# divergence where a caller's explicit model/runner was honored for the LLM calls but the
# verdict's `model`/`runner` fields still reflected the env. Threaded as a ContextVar (NOT
# StepContext, which stays config-agnostic; NOT workflow inputs, which the non-step batch runner
# cannot read) — mirroring the active read-root ContextVars in `rebar.llm.gate_context`. This one
# stays HERE (and did not move with them in ticket b300) because it is genuinely about
# `LLMConfig`: it holds one, and `resolve_gate_config` falls back to `LLMConfig.from_env`, so
# relocating it would only buy a lazy import back the other way.
_active_gate_config: contextvars.ContextVar[LLMConfig | None] = contextvars.ContextVar(
    "rebar_llm_gate_config", default=None
)


@contextlib.contextmanager
def gate_config(cfg: LLMConfig) -> Iterator[None]:
    """Set the active gate-run config for the dynamic extent of the ``with`` block (one gate
    run), so the ops resolve the SAME caller-resolved config. Dropped on exit (never leaks)."""
    token = _active_gate_config.set(cfg)
    try:
        yield
    finally:
        _active_gate_config.reset(token)


def resolve_gate_config(repo_root: str | os.PathLike[str] | None = None) -> LLMConfig:
    """The resolved config for a gate op: the run boundary's config when inside a
    :func:`gate_config` scope (a gate run), else a fresh :meth:`LLMConfig.from_env` (the
    standalone-op fallback). The ops call THIS, never ``from_env`` directly, so a caller's
    explicit ``config=`` is honored uniformly across every op AND the verdict's ``model`` /
    ``runner`` fields (epic veiny-trout-brink)."""
    active = _active_gate_config.get()
    return active if active is not None else LLMConfig.from_env(repo_root=repo_root)


# Single source of truth for the per-call output-token cap default. Referenced by
# both the LLMConfig field default and the env/table resolution fallback so the
# default lives in ONE place (docs/config.md documents the same value).
DEFAULT_MAX_TOKENS = 16000
# Same single-source-of-truth pattern for the agent step cap + per-call wall-clock
# timeout (each previously duplicated the literal across field default + resolution).
# Raised 50 → 250 (≈125 tool-call cycles): 50 (≈25 cycles) is far too low for an agentic
# REVIEW — a code-grounding finder or a multi-child container call exhausts it mid-work and
# raises a step-budget LLMRunnerError. The sibling gates already floor higher per-op
# (completion 480, review/operations 120); raising the framework default safeguards every
# agentic caller (incl. plan-review's Pass-1, which never applied its own floor) so the
# default behavior is "do more tool use" rather than "fail". An operator can still lower it
# via REBAR_LLM_MAX_STEPS; a per-op floor still wins via max(floor, configured).
DEFAULT_MAX_ITERATIONS = 250
DEFAULT_TIMEOUT_S = 600
# Cross-ticket overlap detection (epic only-crave-art) — LLM-feature tunables live on
# LLMConfig, never VerifyConfig (_config_schema.py reserves the llm.* layer). The Cupid
# ticket-digest op (ee3d) instructs the model to emit MIN..MAX atomic propositions and
# post-validates the count (truncate above max; flag low_proposition_count below min).
DEFAULT_OVERLAP_PROPOSITIONS_MIN = 2
DEFAULT_OVERLAP_PROPOSITIONS_MAX = 6
# Stage-1 BM25F candidate generation (2d0f/5a8f): top-K candidates; boilerplate prune
# (ignore terms appearing in > this fraction of digests); overlap floor (fraction of query
# terms a candidate must share to be returned). Field weights are a code constant in
# retrieve.py, not a config knob.
DEFAULT_OVERLAP_K = 20
DEFAULT_OVERLAP_MAX_DOC_FREQ = 0.5
DEFAULT_OVERLAP_MIN_SHOULD_MATCH = 0.15
# Enrichment queue (e1f4): the soak (debounce) between plan-review certification and drain
# eligibility, and the claim lease TTL (a crashed drainer's claim is treated as unclaimed
# after it expires — self-healing, no separate reaper process).
DEFAULT_OVERLAP_SOAK_MIN = 60
DEFAULT_OVERLAP_LEASE_TTL_MIN = 15
# Stage-2 pairwise judge (9022): the per-ordering confidence a candidate must clear to be
# surfaced, and the max number of advisory link suggestions surfaced per query ticket.
DEFAULT_OVERLAP_CONF_THRESHOLD = 0.7
DEFAULT_OVERLAP_SURFACE_CAP = 3
# Tier-1 opportunistic drain (c1de): the drain mode (off|async|always), the per-run batch
# cap, and the cheap gate-check latency budget (ms) for the write-path maybe_drain no-op.
DEFAULT_OVERLAP_DRAIN = "async"
# 20, not 5 (operator ruling OQ3 on bug 6148-5d81-8e80-41e8): with LLM calls outside the
# drain lock the batch is the claim-window size, raised toward the lease-derived bound
# (enrich_drain._lease_bounded_batch clamps any configured value to lease_ttl_s // 40 = 22
# at the default 15-minute lease) so a run can never outlive its claims.
DEFAULT_OVERLAP_DRAIN_BATCH = 20
DEFAULT_OVERLAP_DRAIN_GATE_BUDGET_MS = 20
# Transport-layer retry for Anthropic gate calls (story arcticduck, epic jira-reb-687):
# the tenacity retry envelope wrapping the httpx transport (SDK max_retries=0). Attempts
# counts the first try; max wait caps the Retry-After / exponential backoff per sleep.
DEFAULT_LLM_RETRY_MAX_ATTEMPTS = 4
DEFAULT_LLM_RETRY_MAX_WAIT_S = 60
DEFAULT_LLM_TOOL_TIMEOUT_S = 120
# Execution backends. `pydantic_ai` is THE runtime (story d6d1 cutover dropped the
# in-process graph stack). `fake` is the offline test seam.
RUNNERS = ("pydantic_ai", "fake")

# Model-name prefix → provider (used for diagnostics + clear errors and to pick the
# provider-qualified model string the pydantic_ai runtime dispatches on).
#
# EVERY value here must be a name a runtime registry can resolve — a rebar builder or a provider
# pydantic-ai itself recognizes. The model-class/ladder path composes `<value>:<model>` VERBATIM
# from this table, so a value neither side knows produces a target that passes config resolution
# and can only fail at CALL time, when a gate finally runs — the whole point of inferring a
# provider is defeated if the inferred name cannot be built. `google` is pydantic-ai's current
# canonical name for the Gemini Developer API; `google-gla` and `google-vertex` also resolve but
# are deprecated aliases it removes in v2.0, so they are not what a fresh mapping should emit.
# `tests/unit/test_provider_qualifier.py` pins the invariant across the whole table.
_PROVIDER_PREFIXES = (
    ("claude", "anthropic"),
    ("gpt-", "openai"),
    ("gpt4", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("chatgpt", "openai"),
    ("gemini", "google"),
)


# A provider-qualified model is ``<provider>:<model>`` — but a MODEL ID MAY ITSELF CONTAIN A COLON,
# so "contains a colon" cannot decide whether a string is qualified. Bedrock's canonical ids carry a
# version suffix with one (``anthropic.claude-haiku-4-5-20251001-v1:0``), which is the majority form
# AWS publishes. Reading such an id as ``provider:model`` yields a "provider" of
# ``anthropic.claude-haiku-4-5-20251001-v1`` and kills the run with an error naming a fragment of
# the operator's own model id.
#
# The deciding question is not what the prefix LOOKS like but whether it IS a provider name, so
# this is a MEMBERSHIP test against the names below — the same move LangChain
# (``prefix in _BUILTIN_PROVIDERS``), LiteLLM (``prefix in litellm.provider_list``) and pydantic-ai
# (``infer_provider_class`` raising ``Unknown provider``) all make. A shape test cannot catch a
# typo'd provider, and would silently promote any future model id whose pre-colon prefix happened
# to look identifier-like.
#
# The set is a literal of plain strings on purpose: this runs during config resolution, where the
# optional `[agents]` extra may not be importable at all, so it must not reach into
# ``rebar.llm.providers`` (see that module's docstring on staying stdlib-only at import time).
# ``tests/unit/test_core_optionality.py`` enforces that property. The names are exactly what
# ``providers._pydantic_ai_known_providers()`` returns; a drift test pins the two together, and
# also pins ``ProviderSession._builders`` as a subset.
#
# Membership answers "is this a provider QUALIFIER", which is NOT "can this provider be BUILT" —
# the latter stays ``ProviderSession``'s job. No name is grandfathered in: notably ``test`` is
# pydantic-ai's bare TestModel string rather than a provider, and it rejects both ``test:…`` and
# ``google_genai:…`` as qualifiers, so admitting either here would make rebar more permissive than
# the library it wraps.
KNOWN_PROVIDER_NAMES: frozenset[str] = frozenset(
    {
        "anthropic",
        "bedrock",
        "cerebras",
        "cohere",
        "deepseek",
        "gateway/anthropic",
        "gateway/bedrock",
        "gateway/google-cloud",
        "gateway/groq",
        "gateway/openai",
        "google",
        "google-cloud",
        "google-gla",
        "google-vertex",
        "grok",
        "groq",
        "heroku",
        "huggingface",
        "mistral",
        "moonshotai",
        "openai",
        "openai-chat",
        "openai-responses",
        "vertexai",
        "xai",
    }
)


def split_provider_qualifier(model: str) -> tuple[str | None, str]:
    """Split ``provider:model`` into ``(provider, model)``, or ``(None, model)`` when the string
    carries no provider qualifier — including when it contains a colon that belongs to the model id.

    THE single place that answers "is this string provider-qualified?", so the qualifier and
    :func:`infer_provider` cannot drift apart (they were two independent colon scans before 03b0,
    and both were wrong in the same way).

    An UNRECOGNIZED prefix yields ``(None, model)`` rather than raising, and that is load-bearing:
    ``"anthropic.claude-haiku-4-5-20251001-v1:0"`` splits to a prefix that is by construction not a
    provider name, so "not qualified" is the only answer that keeps such an id intact. Rejecting a
    bad provider is the job of the caller that was HANDED one explicitly
    (:func:`~rebar.llm.model_classes._resolve_target`), where the operator can be told what they
    typed wrong."""
    prefix, sep, rest = model.partition(":")
    if sep and rest and prefix in KNOWN_PROVIDER_NAMES:
        return prefix, rest
    return None, model


def infer_provider(model: str, explicit: str | None = None) -> str | None:
    """Resolve the provider for a model: an explicit setting, a ``provider:model``
    prefix, or inference from the model name. Returns None if undeterminable."""
    if explicit:
        return explicit
    qualifier, _ = split_provider_qualifier(model)
    if qualifier:
        return qualifier
    low = model.lower()
    for prefix, provider in _PROVIDER_PREFIXES:
        if low.startswith(prefix):
            return provider
    return None


def resolve_model(cfg: LLMConfig, *, step: str | None = None, workflow: str | None = None) -> str:
    """Resolve the model id for a workflow step by the documented precedence (WS-D3):

        step > workflow > config > default

    The first three are explicit here; ``cfg.model`` already folds the last
    (``[tool.rebar.llm].model``, else ``DEFAULT_MODEL``). So a per-step ``model:``
    (e.g. ``anthropic:claude-opus-4-8`` or ``openai-chat:gpt-4o``) wins, then a
    workflow-level ``model:``, then whatever the config/env/default resolved to.
    Returns a model id consumable by the runner (``provider:model`` or a bare model
    whose provider is inferred).

    The precedence WINNER may be a reserved MODEL CLASS name (``trivial``/``standard``/
    ``frontier``), so it is resolved through the class table; any other string comes back
    unchanged (task 7761). The import is lazy INSIDE the body on purpose: ``model_classes``
    imports this module at scope, so a module-level import here would close a cycle.

    The class table is read from ``cfg.repo_path`` — the SAME root the rest of ``cfg`` resolved
    against — rather than from ambient discovery, so a step's class cannot resolve against a
    different project than its own config did (bug 2876)."""
    from rebar.llm.model_classes import resolve_model_string

    return resolve_model_string(step or workflow or cfg.model, cfg.repo_path)


def denied_paths(root: str) -> tuple[str, ...]:
    """Realpaths the agent must never read OR cite: git internals, reconciler
    state, and the live event store — resolved from rebar.config.tracker_dir(root)
    so the REBAR_TRACKER_DIR override (a
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
    except Exception:  # noqa: BLE001 — best-effort config-path candidate: skip the tracker dir if it can't be resolved
        pass
    return tuple(dict.fromkeys(os.path.realpath(p) for p in candidates))


def is_denied(abs_path: str, denied: tuple[str, ...]) -> bool:
    return any(abs_path == d or abs_path.startswith(d + os.sep) for d in denied)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


# ── [tool.rebar.llm] config-file layer (0ac6 slice 4) ─────────────────────────
#
# llm.* is resolved HERE (not the stdlib-core typed Config) so importing rebar.llm
# never pulls the agents stack into core. The non-secret, non-runtime, non-derived
# knobs are settable in a ``[tool.rebar.llm]`` table (pyproject / rebar.toml [llm] /
# XDG user config), read via the core loader's
# discovery so file LOCATIONS + precedence match the rest of rebar. Resolution per
# key: ``rebar -c llm.KEY=VALUE`` (CLI) > ``REBAR_LLM_<KEY>`` env > config file >
# default. Secrets (REBAR_LLM_API_KEY / ANTHROPIC/OPENAI keys / LANGFUSE_*),
# the runtime-only REBAR_LLM_REPO_PATH, and the DERIVED runner stay env-only and
# are NOT config-file keys.


def _read_llm_file_table(repo_root=None) -> dict:
    """The merged ``[tool.rebar.llm]`` table (user < project < ``REBAR_LLM_CONFIG_FILE``),
    or ``{}``. A malformed core config degrades to env-only — a broken pyproject must never
    break an LLM op — but the ``REBAR_LLM_CONFIG_FILE`` pointer is layered OUTSIDE that
    degrade, so an environment that explicitly named a config file is never silently
    ignored (it deep-merges per key over the discovered table; see
    :func:`rebar.config.layer_llm_config_file`).

    A running gate PINS its code read-root (``use_code_root``, an attested snapshot), and
    ``gate_source.gate_read_root`` states the invariant that EVERY config rebuilt deep in the
    gate reads that root. Discovery below runs through ``rebar.config``, whose ``repo_root()``
    only knows ``explicit > REBAR_ROOT > git toplevel of CWD`` — so a caller threading no root
    resolved against the AMBIENT cwd and, when that missed the project's ``[tool.rebar.llm]``
    table, silently handed :func:`rebar.llm.model_classes.parse_class_slots` ``{}``, routing a
    Bedrock-configured project at the built-in DIRECT-ANTHROPIC class defaults (bug 2876).
    Defaulting to the active gate root fixes every such reader at once — the model-class table
    and the scalar ``LLMConfig.from_env().model`` alike — because both land here. An EXPLICIT
    ``repo_root`` still wins, so threading a root never becomes a no-op, and outside a gate
    ``current_code_root()`` is ``None`` and ambient discovery is unchanged.
    """
    if repo_root is None:
        repo_root = current_code_root()
    try:
        discovered = _root_config.read_reserved_section("llm", repo_root)
    except _root_config.ConfigError:
        discovered = {}
    return _root_config.layer_llm_config_file(discovered)


def _llm_str_source(table: dict, cli: dict, env_name: str | None, file_key: str, default):
    """Resolve a string setting to ``(value, source)``: CLI > env > file > default (blank →
    fall through). The ``source`` labels the layer that actually won — ``"cli"``, the env
    var's own name, ``"repo-config"`` for the config-file table, or ``None`` for the default —
    from the SAME pass that produced the value, so a provenance label derived from it can
    never diverge from the resolution (cda8). ``env_name=None`` means the setting has NO env
    channel (CLI + config file only) — used by ``model``, whose bare ``REBAR_LLM_MODEL`` env
    was removed and tombstoned."""
    if file_key in cli and str(cli[file_key]).strip():
        return str(cli[file_key]).strip(), "cli"
    raw = os.environ.get(env_name) if env_name is not None else None
    if raw is not None and raw.strip():
        return raw.strip(), env_name
    fv = table.get(file_key)
    if fv is not None and str(fv).strip():
        return str(fv).strip(), "repo-config"
    return default, None


def _llm_str(table: dict, cli: dict, env_name: str | None, file_key: str, default):
    """Resolve a string setting: CLI > env > file > default (blank → fall through).

    Delegates to :func:`_llm_str_source` (the ONE resolution pass) and drops the source —
    identical value semantics for every existing key."""
    return _llm_str_source(table, cli, env_name, file_key, default)[0]


def _llm_int(table: dict, cli: dict, env_name: str, file_key: str, default: int):
    """Resolve an int setting: CLI > env > file > default. An unparseable higher
    layer falls through to the next."""
    candidates: list = []
    if file_key in cli:
        candidates.append(cli[file_key])
    env_raw = os.environ.get(env_name)
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


def _llm_float(table: dict, cli: dict, env_name: str, file_key: str, default: float | None):
    """Resolve a float setting: CLI > env > file > default. An unparseable higher
    layer falls through to the next (mirrors :func:`_llm_int`)."""
    candidates: list = []
    if file_key in cli:
        candidates.append(cli[file_key])
    env_raw = os.environ.get(env_name)
    if env_raw is not None and env_raw.strip():
        candidates.append(env_raw)
    fv = table.get(file_key)
    if fv is not None and not isinstance(fv, bool):
        candidates.append(fv)
    for c in candidates:
        try:
            return float(str(c).strip())
        except (TypeError, ValueError):
            continue
    return default


def _llm_drain_mode(raw: str) -> str:
    """Validate the overlap_drain enum; an unrecognized value falls back to the default."""
    v = str(raw).strip().lower()
    return v if v in ("off", "async", "always") else DEFAULT_OVERLAP_DRAIN


@dataclass
class LangfuseConfig:
    """Langfuse credentials/host, plus whether OTLP tracing is *enabled* (Langfuse is the
    optional trace endpoint only; prompts are git-canonical and never fetched from Langfuse).

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
    runner: str = "pydantic_ai"
    model: str = DEFAULT_MODEL
    # Provider is OPTIONAL: it is inferred from the model name (claude-*→anthropic,
    # gpt-*→openai, gemini-*→google) to build the provider-qualified model
    # string the pydantic_ai runtime dispatches on. Set it explicitly for ambiguous
    # names.
    model_provider: str | None = None
    base_url: str | None = None  # OpenAI-compatible endpoint (local models)
    api_key: str | None = None  # explicit key (e.g. a dummy key for local servers)
    # Opt-in LOCAL capture of the raw model reply on FINAL structured-parse failure (story
    # 2fd6). None (the default) = off: nothing is written and the failure path is byte-for-byte
    # unchanged. When set, the terminal failure of the prompted reask loop writes ONE artifact
    # here (best-effort; rotated to the newest 20 per directory) and names its path in the error.
    parse_failure_artifact_dir: str | None = None
    # Bedrock (story S3/2932). NO rebar-managed key: Bedrock authenticates through the
    # AMBIENT AWS credential chain (instance role / AWS_PROFILE / boto3's own default chain),
    # so unlike `api_key` above there is no Bedrock credential field here at all.
    bedrock_region_name: str | None = None  # None -> boto3's own region resolution
    # WHICH layer supplied bedrock_region_name — "cli", "REBAR_LLM_BEDROCK_REGION", or
    # "repo-config"; None when the knob is unset (cda8). Set by `from_env` from the SAME
    # resolution pass that produced the value (never re-derived), and recorded verbatim as
    # `region_source` in the verdict's provider provenance when the knob wins the region
    # chain. Label-only metadata: it never influences which region value is used.
    bedrock_region_source: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    timeout_s: int = DEFAULT_TIMEOUT_S
    # Sampling temperature. ``None`` (the default) sends NO temperature, so the provider default
    # is used — byte-unchanged for every existing caller. When set it rides into the model call's
    # ModelSettings (see runner). The Pass-2 verifier pins this to 0 (greedy) so a re-run of the
    # same finding does not resample its narrow yes/no verification and flip a block/advisory
    # decision (run-to-run non-determinism, upstream review-code report §2). It is a REPRODUCIBILITY
    # floor, not a correctness fix — determinate answers come from the question design.
    temperature: float | None = None
    # Transport-layer retry (story arcticduck): the httpx AsyncTenacityTransport wrapping
    # every Anthropic call retries a transient {429,529,5xx}/timeout/network blip below the
    # SDK (SDK max_retries=0). ``llm_retry_max_attempts`` is stop_after_attempt(N); N<=1
    # disables retry (fail-fast back-out). ``llm_retry_max_wait_s`` caps the Retry-After /
    # exponential-backoff wait.
    llm_retry_max_attempts: int = DEFAULT_LLM_RETRY_MAX_ATTEMPTS
    llm_retry_max_wait_s: int = DEFAULT_LLM_RETRY_MAX_WAIT_S
    # Per-TOOL execution timeout (story hoopoe): bounds a hung ASYNC/MCP tool via
    # Agent(tool_timeout=…). A no-op for sync in-process tools (they block the loop);
    # those are bounded by the derived step caps. The per-REQUEST read timeout reuses
    # ``timeout_s`` (no separate key).
    llm_tool_timeout_s: int = DEFAULT_LLM_TOOL_TIMEOUT_S
    repo_path: str | None = None
    # The read root for the agent's rebar TICKET tools — a pinned snapshot of the ticket
    # store in attested mode (the orphan `tickets` branch is absent from the code snapshot
    # `repo_path`), or `None` to read the in-place checkout's store (local mode). Set from
    # `current_tickets_root()` by `from_env`.
    tickets_path: str | None = None
    mcp_servers: dict = field(default_factory=dict)
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)
    # Cross-ticket overlap detection (epic only-crave-art) — proposition-count bounds
    # for the Cupid ticket-digest op (ee3d).
    overlap_propositions_min: int = DEFAULT_OVERLAP_PROPOSITIONS_MIN
    overlap_propositions_max: int = DEFAULT_OVERLAP_PROPOSITIONS_MAX
    # Stage-1 BM25F candidate generation (5a8f).
    overlap_k: int = DEFAULT_OVERLAP_K
    overlap_max_doc_freq: float = DEFAULT_OVERLAP_MAX_DOC_FREQ
    overlap_min_should_match: float = DEFAULT_OVERLAP_MIN_SHOULD_MATCH
    # Enrichment queue (e1f4).
    overlap_soak_min: int = DEFAULT_OVERLAP_SOAK_MIN
    overlap_lease_ttl_min: int = DEFAULT_OVERLAP_LEASE_TTL_MIN
    # Stage-2 pairwise judge (9022).
    overlap_conf_threshold: float = DEFAULT_OVERLAP_CONF_THRESHOLD
    overlap_surface_cap: int = DEFAULT_OVERLAP_SURFACE_CAP
    # Tier-1 drain (c1de).
    overlap_drain: str = DEFAULT_OVERLAP_DRAIN
    overlap_drain_batch: int = DEFAULT_OVERLAP_DRAIN_BATCH
    overlap_drain_gate_budget_ms: int = DEFAULT_OVERLAP_DRAIN_GATE_BUDGET_MS

    @classmethod
    def from_env(cls, *, repo_root=None) -> LLMConfig:
        # Tombstones: the removed REBAR_LLM_MAX_ITERS (use REBAR_LLM_MAX_STEPS) and the
        # removed bare REBAR_LLM_MODEL. Enforced HERE (not in the core config layer) so a
        # retired LLM knob fails loud only when the LLM stack actually loads.
        # RemovedInputError is a BaseException, so the broad ``except Exception`` in this
        # method's tracker-probe path can't swallow it.
        # Tombstone: the removed bare REBAR_LLM_MODEL (use the model_classes slots). Same
        # placement and rationale as REBAR_LLM_MAX_ITERS above — the CONFIG key
        # `[tool.rebar.llm].model` is unaffected and still resolves.
        for _retired in ("REBAR_LLM_MAX_ITERS", "REBAR_LLM_MODEL"):
            if _retired in os.environ:
                from rebar._deprecations import RemovedInputError, removed_input

                raise RemovedInputError(removed_input("env", _retired))
        # The runner is DERIVED, not a public env knob (EV-4). The provider-agnostic
        # in-process ``pydantic_ai`` runner is THE runtime (story d6d1 cutover: the
        # in-process graph stack was dropped after the PydanticAI runner was validated
        # live across every operation). The ``fake`` runner is test-only — reachable via
        # the library ``runner=``/``override=`` arg, never from the environment.
        runner = "pydantic_ai"
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
        # repo_path is a RUNTIME-only override — not a [tool.rebar.llm] key. Precedence:
        #   active gate code root (an attested snapshot — wins so EVERY from_env-built
        #   config deep in a gate run reads the pinned snapshot, never the mutable checkout)
        #   > REBAR_LLM_REPO_PATH env > the resolved repo root (the in-place checkout).
        repo_path = (
            current_code_root()
            or os.environ.get("REBAR_LLM_REPO_PATH")
            or str(_root_config.repo_root(repo_root))
        )
        # The agent's rebar ticket tools read the PINNED ticket-store snapshot when a gate
        # set it (None when unset -> the live checkout's store; preserves prior behavior).
        tickets_path = current_tickets_root()
        # Bedrock region: value + origin from the ONE resolution pass (cda8) — the source
        # label can never disagree with which layer actually won. Precedence unchanged:
        # CLI > REBAR_LLM_BEDROCK_REGION > config file > None.
        bedrock_region_name, bedrock_region_source = _llm_str_source(
            table, cli, "REBAR_LLM_BEDROCK_REGION", "bedrock_region_name", None
        )
        return cls(
            runner=runner,
            # No env channel: the bare REBAR_LLM_MODEL was removed (tombstoned above);
            # `model` now resolves CLI > [tool.rebar.llm].model > DEFAULT_MODEL.
            model=_llm_str(table, cli, None, "model", DEFAULT_MODEL),
            model_provider=_llm_str(table, cli, "REBAR_LLM_MODEL_PROVIDER", "model_provider", None),
            base_url=_llm_str(table, cli, "REBAR_LLM_BASE_URL", "base_url", None),
            parse_failure_artifact_dir=_llm_str(
                table,
                cli,
                "REBAR_LLM_PARSE_FAILURE_ARTIFACT_DIR",
                "parse_failure_artifact_dir",
                None,
            ),
            bedrock_region_name=bedrock_region_name,
            bedrock_region_source=bedrock_region_source,
            api_key=os.environ.get("REBAR_LLM_API_KEY") or None,
            max_tokens=_llm_int(
                table, cli, "REBAR_LLM_MAX_TOKENS", "max_tokens", DEFAULT_MAX_TOKENS
            ),
            max_iterations=_llm_int(
                table,
                cli,
                "REBAR_LLM_MAX_STEPS",
                "max_steps",
                DEFAULT_MAX_ITERATIONS,
            ),
            timeout_s=_llm_int(table, cli, "REBAR_LLM_TIMEOUT", "timeout", DEFAULT_TIMEOUT_S),
            # Default None → provider default (unchanged); an operator may pin a global
            # temperature, and the Pass-2 verify steps pin 0 via a `with:` input (see runs.py).
            temperature=_llm_float(table, cli, "REBAR_LLM_TEMPERATURE", "temperature", None),
            llm_retry_max_attempts=_llm_int(
                table,
                cli,
                "REBAR_LLM_RETRY_MAX_ATTEMPTS",
                "llm_retry_max_attempts",
                DEFAULT_LLM_RETRY_MAX_ATTEMPTS,
            ),
            llm_retry_max_wait_s=_llm_int(
                table,
                cli,
                "REBAR_LLM_RETRY_MAX_WAIT_S",
                "llm_retry_max_wait_s",
                DEFAULT_LLM_RETRY_MAX_WAIT_S,
            ),
            llm_tool_timeout_s=_llm_int(
                table,
                cli,
                "REBAR_LLM_TOOL_TIMEOUT_S",
                "llm_tool_timeout_s",
                DEFAULT_LLM_TOOL_TIMEOUT_S,
            ),
            repo_path=repo_path,
            tickets_path=tickets_path,
            mcp_servers=mcp_servers,
            langfuse=LangfuseConfig.from_env(),
            overlap_propositions_min=_llm_int(
                table,
                cli,
                "REBAR_LLM_OVERLAP_PROPOSITIONS_MIN",
                "overlap_propositions_min",
                DEFAULT_OVERLAP_PROPOSITIONS_MIN,
            ),
            overlap_propositions_max=_llm_int(
                table,
                cli,
                "REBAR_LLM_OVERLAP_PROPOSITIONS_MAX",
                "overlap_propositions_max",
                DEFAULT_OVERLAP_PROPOSITIONS_MAX,
            ),
            overlap_k=_llm_int(table, cli, "REBAR_LLM_OVERLAP_K", "overlap_k", DEFAULT_OVERLAP_K),
            overlap_max_doc_freq=_llm_float(
                table,
                cli,
                "REBAR_LLM_OVERLAP_MAX_DOC_FREQ",
                "overlap_max_doc_freq",
                DEFAULT_OVERLAP_MAX_DOC_FREQ,
            ),
            overlap_min_should_match=_llm_float(
                table,
                cli,
                "REBAR_LLM_OVERLAP_MIN_SHOULD_MATCH",
                "overlap_min_should_match",
                DEFAULT_OVERLAP_MIN_SHOULD_MATCH,
            ),
            overlap_soak_min=_llm_int(
                table,
                cli,
                "REBAR_LLM_OVERLAP_SOAK_MIN",
                "overlap_soak_min",
                DEFAULT_OVERLAP_SOAK_MIN,
            ),
            overlap_lease_ttl_min=_llm_int(
                table,
                cli,
                "REBAR_LLM_OVERLAP_LEASE_TTL_MIN",
                "overlap_lease_ttl_min",
                DEFAULT_OVERLAP_LEASE_TTL_MIN,
            ),
            overlap_conf_threshold=_llm_float(
                table,
                cli,
                "REBAR_LLM_OVERLAP_CONF_THRESHOLD",
                "overlap_conf_threshold",
                DEFAULT_OVERLAP_CONF_THRESHOLD,
            ),
            overlap_surface_cap=_llm_int(
                table,
                cli,
                "REBAR_LLM_OVERLAP_SURFACE_CAP",
                "overlap_surface_cap",
                DEFAULT_OVERLAP_SURFACE_CAP,
            ),
            overlap_drain=_llm_drain_mode(
                _llm_str(
                    table, cli, "REBAR_LLM_OVERLAP_DRAIN", "overlap_drain", DEFAULT_OVERLAP_DRAIN
                )
            ),
            overlap_drain_batch=_llm_int(
                table,
                cli,
                "REBAR_LLM_OVERLAP_DRAIN_BATCH",
                "overlap_drain_batch",
                DEFAULT_OVERLAP_DRAIN_BATCH,
            ),
            overlap_drain_gate_budget_ms=_llm_int(
                table,
                cli,
                "REBAR_LLM_OVERLAP_DRAIN_GATE_BUDGET_MS",
                "overlap_drain_gate_budget_ms",
                DEFAULT_OVERLAP_DRAIN_GATE_BUDGET_MS,
            ),
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
        # The provider-agnostic Pydantic AI runtime (the `agents` extra). The provider
        # is chosen by the model string, so there are no per-provider integration
        # packages to detect — anthropic/openai/google all run on the same stack.
        "pydantic_ai": _module_available("pydantic_ai"),
        "anthropic_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_api_key": bool(os.environ.get("OPENAI_API_KEY")),
        "langfuse_configured": LangfuseConfig.from_env().enabled,
        # Net-new extra (epic a88f / WS-J): detected via the core guard, no import.
        "tracing_extra": _extra_installed("tracing"),
    }


def _extra_installed(extra: str) -> bool:
    """Thin bridge to the core optional-dependency guard (rebar._optional)."""
    from rebar._optional import extra_installed

    return extra_installed(extra)


def agents_extra_installed() -> bool:
    """True when the ``nava-rebar[agents]`` extra is importable — i.e. the
    provider-agnostic Pydantic AI runtime is present. The provider is selected by the
    model string, so no per-provider integration package is required to run."""
    return available_backends()["pydantic_ai"]
