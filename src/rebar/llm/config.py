"""Configuration + backend detection for the rebar LLM agent-operations framework.

``LLMConfig`` is a plain dataclass resolved from the environment (and explicit
overrides). It is **stdlib-only** — importing it never pulls the agent runtime
(pydantic-ai) or anthropic — so ``import rebar.llm`` stays dependency-free; the
heavy libraries are imported lazily by the runner only when an operation runs.

Environment variables (all optional; sensible defaults):

  REBAR_LLM_MODEL         model id (default ``claude-opus-4-8``); the runner is the
                          provider-agnostic pydantic_ai runtime (``fake`` is test-only,
                          reachable only via the library ``runner=`` arg).
  REBAR_LLM_MAX_TOKENS    per-response token ceiling (default 8000)
  REBAR_LLM_MAX_STEPS     Max agent loop steps before abort (~2 per tool call; default
                          50 ~= 25 tool calls).
  REBAR_LLM_TIMEOUT       per-operation wall-clock seconds (default 600)
  REBAR_LLM_REPO_PATH     repo root the agent's read-only file tools see (default: repo root)
  REBAR_LLM_MCP_SERVERS   JSON object of MCP servers (Pydantic AI MCP toolset shape)
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

DEFAULT_MODEL = "claude-opus-4-8"
# The decisive non-frontier model used by the gate VERIFIERS (plan-review Pass-2 verify and
# the completion verifier) when the operator has NOT explicitly chosen a model (i.e.
# cfg.model == DEFAULT_MODEL). A focused yes/no verification is a decisive, non-open-ended
# judgement, so a cheaper/faster model is sufficient; an explicit operator model still wins.
# Single source of truth — imported by both completion.py and plan_review (no duplication).
VERIFIER_DEFAULT_MODEL = "claude-sonnet-4-6"

# The active code read-root for the running gate (epic raze-vet-ditch S3). When a gate
# runs in `attested` mode it materializes a snapshot at the client-pinned SHA and sets
# this for the duration of the run; `LLMConfig.from_env` then resolves `repo_path` to the
# snapshot, so EVERY config built deep in the gate (citation resolution, reconcile, the
# agent itself) reads the pinned snapshot rather than the server's mutable checkout. A
# ContextVar is thread- and asyncio-task-safe (no global env mutation across concurrent
# gates). Unset (the default) preserves the prior in-place behavior — exactly `local` mode.
_active_code_root: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rebar_llm_code_root", default=None
)


def current_code_root() -> str | None:
    """The active gate's code read-root (an attested snapshot dir), or ``None``."""
    return _active_code_root.get()


def resolve_code_root(
    repo_root: str | os.PathLike[str] | None = None,
    *,
    cfg_repo_path: str | None = None,
    allow_checkout_fallback: bool = True,
    require: bool = False,
) -> str | None:
    """The single authoritative code read-root resolver for the LLM gates.

    Cascade (first truthy wins):

      1. an explicit ``repo_root`` (a caller override),
      2. ``cfg_repo_path`` (a pinned snapshot already resolved onto an explicit ``LLMConfig``),
      3. the ACTIVE attested-gate snapshot (:func:`current_code_root`) — a gate pins this to
         ``[snapshot].ref`` (``origin/main`` HEAD by default), so an in-gate caller that
         threads nothing still grounds against the pinned snapshot,
      4. the live checkout root (:func:`rebar.config.repo_root`, which itself falls back to
         the cwd and so never returns ``None``) — UNLESS ``allow_checkout_fallback`` is False.

    Centralizing this is what kills the *class* of bug where a gate consumer handed
    ``repo_root=None`` (because the value was dropped on one of the threading hops) silently
    degrades — e.g. the det-floor P2 'resolution' check abstaining ``no_repo_root``, or an
    agentic verifier reading the server's mutable checkout instead of the pinned snapshot.

    The default (``allow_checkout_fallback=True``) NEVER returns ``None`` and is for the gate
    BOUNDARY (a workflow run needs a concrete root; in non-attested local mode the checkout IS
    the correct root). Lightweight context builders that must NOT force a checkout default
    (where ``None`` legitimately means "no code to ground against", and a forced checkout root
    would induce writes) pass ``allow_checkout_fallback=False`` to get snapshot-or-``None``.

    ``require=True`` makes the read-root contract ENFORCEABLE for a stage that genuinely cannot
    run blind: if the cascade would yield ``None`` (only reachable with
    ``allow_checkout_fallback=False``), it raises :class:`~rebar.llm.errors.LLMConfigError`
    (fail-closed) instead of returning ``None`` — so a stage that requires a root never silently
    degrades against one (the #71 class of bug). It is opt-in (default ``False`` preserves the
    snapshot-or-``None`` behavior) and composes with the cascade: a resolved snapshot/checkout
    satisfies it without raising. See docs/adr/0006-llm-stage-seam-contracts.md."""
    if repo_root:
        return str(repo_root)
    if cfg_repo_path:
        return cfg_repo_path
    snapshot = current_code_root()
    if snapshot:
        return snapshot
    resolved = str(_root_config.repo_root()) if allow_checkout_fallback else None
    if resolved is None and require:
        from rebar.llm.errors import LLMConfigError

        raise LLMConfigError(
            "resolve_code_root: a code read-root is REQUIRED but none could be resolved "
            "(no explicit repo_root, no cfg.repo_path, no active attested snapshot, and "
            "allow_checkout_fallback=False). A gate stage must not run blind against a None "
            "root — thread a root, activate a snapshot, or allow the checkout fallback."
        )
    return resolved


# The active TICKET-store read-root for the running gate. The agent's rebar ticket tools
# resolve the store under `cfg.repo_path` (the code snapshot) — but the ticket store lives
# on the orphan `tickets` branch (gitignored `.tickets-tracker/`) and is ABSENT from the
# code snapshot, so a gate sets this to a separately materialized, pinned copy of the store
# (see `rebar._snapshot.materialize_tickets`) and `LLMConfig.from_env` resolves
# `tickets_path` to it. Mirrors `_active_code_root`; unset (local mode) reads the live
# checkout's store (which already has `.tickets-tracker/`).
_active_tickets_root: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rebar_llm_tickets_root", default=None
)


def current_tickets_root() -> str | None:
    """The active gate's ticket-store read-root (a pinned snapshot of the store), or ``None``."""
    return _active_tickets_root.get()


def current_code_sha() -> str | None:
    """The pinned SHA of the active attested snapshot, or ``None`` (local / no gate).

    Derived from the content-addressed snapshot layout: an attested code root is
    ``<store>/<sha>`` (``rebar._snapshot`` keys entries by full commit SHA), so the dir
    name IS the SHA. A local read root (the checkout) is not SHA-named → ``None``."""
    root = current_code_root()
    if not root:
        return None
    name = os.path.basename(root.rstrip(os.sep))
    if len(name) == 40 and all(c in "0123456789abcdef" for c in name):
        return name
    return None


# The active gate RUN config (epic veiny-trout-brink). The run boundary (`produce_*` in
# gate_dispatch) resolves the caller's `LLMConfig` ONCE — honoring an explicit `config=` — and
# sets this for the duration of the workflow run, so every gate op (and the non-step
# ProductionBatchRunner) reads the SAME resolved config instead of re-deriving it from the
# environment per op. This is the model/runner identity the verdict reports, fixing the
# divergence where a caller's explicit model/runner was honored for the LLM calls but the
# verdict's `model`/`runner` fields still reflected the env. Threaded as a ContextVar (NOT
# StepContext, which stays config-agnostic; NOT workflow inputs, which the non-step batch runner
# cannot read) — mirroring the active read-root ContextVars above.
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


# Whether we are inside a code-reading gate's snapshot session (epic raze-vet-ditch S-RETRO
# safeguard). Set by `gate_source.gate_read_root` for BOTH attested AND local runs — so it
# marks "a gate deliberately chose this read root", distinct from `current_code_root` (which
# is only set for attested). The runtime guard `assert_gated` uses it to FAIL CLOSED when a
# tool-using agent's file tools are built outside any gate session — catching a new agentic
# op (e.g. a generic run_workflow agent step) added without following the snapshot process.
_in_gate_session: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "rebar_llm_in_gate_session", default=False
)


def in_gate_session() -> bool:
    """True iff execution is inside a code-reading gate's snapshot session."""
    return _in_gate_session.get()


@contextlib.contextmanager
def gate_session() -> Iterator[None]:
    """Mark the block as running inside a gate's snapshot session (attested OR local)."""
    token = _in_gate_session.set(True)
    try:
        yield
    finally:
        _in_gate_session.reset(token)


def assert_gated(context: str = "agentic file access") -> None:
    """Fail closed when a tool-using agent reads files OUTSIDE the snapshot gate process.

    The safeguard (epic raze-vet-ditch) against a NEW agentic operation being added without
    routing through ``rebar.llm.gate_source`` (which pins an attested snapshot or an explicit
    local read). Any agent that wires read-only file tools MUST run inside ``gate_read_root``;
    otherwise it would silently read the server's mutable checkout — the exact class of bug
    this epic exists to prevent. ``REBAR_GATE_ALLOW_UNGATED=1`` is a logged escape hatch for a
    deliberate, audited exception."""
    if _in_gate_session.get():
        return
    if os.environ.get("REBAR_GATE_ALLOW_UNGATED", "").strip().lower() in ("1", "true", "yes"):
        import logging

        logging.getLogger("rebar.llm.config").warning(
            "%s ran OUTSIDE a snapshot gate session (REBAR_GATE_ALLOW_UNGATED override)", context
        )
        return
    raise RuntimeError(
        f"{context} was attempted OUTSIDE the repo-snapshot gate process (epic "
        "raze-vet-ditch): a tool-using agent must run inside rebar.llm.gate_source."
        "gate_read_root (attested snapshot or explicit local), never against the server's "
        "mutable checkout. Route the operation through gate_source, or set "
        "REBAR_GATE_ALLOW_UNGATED=1 to override (audited)."
    )


@contextlib.contextmanager
def use_code_root(path: str | None) -> Iterator[None]:
    """Bind the gate's code read-root for the duration of the block (``None`` = no override,
    i.e. read the in-place checkout — local mode).

    Caveat: a ``ContextVar`` is inherited by asyncio tasks but NOT by raw threads — code that
    rebuilds an :class:`LLMConfig` on a worker thread (e.g. a future ``map`` workflow step's
    fan-out) must propagate context via ``contextvars.copy_context().run`` or it will fall
    through to the checkout. The current gate workflows rebuild config only on the calling
    thread, so the snapshot is honored everywhere they read it."""
    token = _active_code_root.set(path)
    try:
        yield
    finally:
        _active_code_root.reset(token)


@contextlib.contextmanager
def use_tickets_root(path: str | None) -> Iterator[None]:
    """Bind the gate's ticket-store read-root for the duration of the block (``None`` = no
    override, i.e. read the in-place checkout's store — local mode). Mirrors
    :func:`use_code_root`; the same raw-thread ContextVar caveat applies."""
    token = _active_tickets_root.set(path)
    try:
        yield
    finally:
        _active_tickets_root.reset(token)


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
# Execution backends. `pydantic_ai` is THE runtime (story d6d1 cutover dropped the
# in-process graph stack). `fake` is the offline test seam.
RUNNERS = ("pydantic_ai", "fake")

# Model-name prefix → provider (used for diagnostics + clear errors and to pick the
# provider-qualified model string the pydantic_ai runtime dispatches on).
_PROVIDER_PREFIXES = (
    ("claude", "anthropic"),
    ("gpt-", "openai"),
    ("gpt4", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("chatgpt", "openai"),
    ("gemini", "google_genai"),
)


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


def resolve_model(cfg: LLMConfig, *, step: str | None = None, workflow: str | None = None) -> str:
    """Resolve the model id for a workflow step by the documented precedence (WS-D3):

        step > workflow > config > env > default

    The first three are explicit here; ``cfg.model`` already folds the last two
    (``REBAR_LLM_MODEL`` env, else ``DEFAULT_MODEL``). So a per-step ``model:``
    (e.g. ``anthropic:claude-opus-4-8`` or ``openai:gpt-4o``) wins, then a
    workflow-level ``model:``, then whatever the config/env/default resolved to.
    Returns a model id consumable by the runner (``provider:model`` or a bare model
    whose provider is inferred)."""
    return step or workflow or cfg.model


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


def _llm_float(table: dict, cli: dict, env_name: str, file_key: str, default: float):
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
    # gpt-*→openai, gemini-*→google_genai) to build the provider-qualified model
    # string the pydantic_ai runtime dispatches on. Set it explicitly for ambiguous
    # names.
    model_provider: str | None = None
    base_url: str | None = None  # OpenAI-compatible endpoint (local models)
    api_key: str | None = None  # explicit key (e.g. a dummy key for local servers)
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    timeout_s: int = DEFAULT_TIMEOUT_S
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

    @classmethod
    def from_env(cls, *, repo_root=None) -> LLMConfig:
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
        return cls(
            runner=runner,
            model=_llm_str(table, cli, "REBAR_LLM_MODEL", "model", DEFAULT_MODEL),
            model_provider=_llm_str(table, cli, "REBAR_LLM_MODEL_PROVIDER", "model_provider", None),
            base_url=_llm_str(table, cli, "REBAR_LLM_BASE_URL", "base_url", None),
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
        # Net-new extras (epic a88f / WS-J): detected via the core guard, no import.
        "eval_extra": _extra_installed("eval"),
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
