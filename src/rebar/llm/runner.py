"""Runners — the pluggable execution backends behind every LLM operation.

A ``Runner`` takes a :class:`RunRequest` (a resolved system prompt + task
instructions + config) and returns a validated ``review_result`` dict. This is the
seam that makes the framework portable: the default operation runs a
provider-agnostic Pydantic AI agent (``PydanticAIRunner``); a ``FakeRunner`` lets
the whole pipeline be exercised offline with no model/network.

Heavy libraries (pydantic-ai/anthropic) are imported **inside** the runner
methods, never at module top, so ``import rebar.llm`` stays stdlib-only. The
substrate is provider-agnostic (the provider is chosen by the model string), and
entirely optional (the ``nava-rebar[agents]`` extra); a missing extra raises a
clear, actionable error.
"""

from __future__ import annotations

import logging
import time
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from rebar.llm import findings as _findings
from rebar.llm import step_failures, usage_log
from rebar.llm.agent_call import build_agent_kwargs, log_call_success, record_call_spend
from rebar.llm.anthropic_model import (
    _DIRECT_ANTHROPIC_BASE_URL,  # noqa: F401  (re-exported for tests / back-compat)
    _local_proxy_bypass_base_url,  # noqa: F401  (re-exported for tests / back-compat)
    _pai_model,
)
from rebar.llm.capabilities import (
    cache_settings_for,
    capabilities_for,
    provenance_for,
    web_search_capabilities,
)
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError, LLMError
from rebar.llm.model_classes import (
    build_fallback_model,
    ensure_current_event_loop,
    entered_fallback_model,
    fallback_targets_for,
    primary_endpoint_for,
)
from rebar.llm.providers import ProviderSession
from rebar.llm.runner_support import (
    _TOOL_CAPABILITY_CHECKED,  # noqa: F401  (re-exported for tests / back-compat)
    _answering_model,
    _check_tool_capability,
    _intersect_capabilities,
    _readonly_gate,
)
from rebar.llm.structured_run import (
    FailureContext,
    _extract_usage,
    _import_pydantic_ai,
    _pai_check_config,
    _pai_structured,
    _warn_if_zeroed_usage,
    apply_structured_seams,
    build_model_settings,
    build_usage_limits,
    effective_max_iterations,  # noqa: F401  (re-exported: tests import it from `runner`)
    effective_max_tokens,  # noqa: F401  (re-exported: tests import it from `runner`)
    estimate_marked_prefix_tokens,
    interpret_failure,
    warn_if_cache_ineffective,
)

logger = logging.getLogger(__name__)

# Dedup set (story S3/2932) so the "temperature withdrawn" INFO line logs ONCE per resolved
# model string, not once per call — mirrors `_TOOL_CAPABILITY_CHECKED` below.
_TEMPERATURE_WITHDRAWN_LOGGED: set[str] = set()


@dataclass
class RunRequest:
    system_prompt: str
    instructions: str
    config: LLMConfig
    reviewers: list[str] = field(default_factory=list)
    target: dict = field(default_factory=dict)
    langfuse_prompt: object | None = None
    # Generalized output contract (WS-D1) — DEFAULTED so existing review callers are
    # unchanged. ``mode``: how to finalize the agent's outcome —
    # ``findings`` (the review_result pipeline, default) | ``structured`` (return the
    # agent's structured payload, validated against ``output_schema``) | ``text``
    # (return the final message text). ``output_schema``: a packaged JSON Schema name
    # constraining/validating ``structured`` output.
    output_schema: str | None = None
    mode: str = "findings"
    # Prompt-level execution mode (story 4b2f) — how the runner DRIVES the model,
    # distinct from `mode` (output shaping). ``agentic`` (default) is the tool-using
    # loop; ``single_turn`` is exactly ONE model call with NO tools/toolsets, going
    # through the structured-output path against ``output_schema``. The caller
    # (RunnerAgentStep) sets `mode="structured"` + `output_schema=<prompt.outputs>`
    # when this is single_turn, so the two stay consistent.
    execution_mode: str = "agentic"
    # Per-operation extra tools appended to the agent's tool list. DEFAULTED None so existing
    # review callers are unchanged. LIVE: the completion verifier's token-recovery path
    # (`llm/workflow/completion_recovery.py`, story 2948) sets it to a `record` tool threaded
    # through `workflow/runs.py`; asserted by `tests/unit/workflow/test_completion_banking.py`.
    extra_tools: list | None = None
    # Extended-thinking flag (1268). Under thinking the structured-output stack routes
    # PromptedOutput UNLESS the model is MEASURED to accept a native output constraint under
    # thinking (`caps.native_output_with_thinking`); not a workaround for any forced-tool
    # mechanism (none exists; output is selected by `output_mode()`). The documented Anthropic
    # 400 was `tool_choice` x thinking, NOT json_schema output x thinking, which is wire-legal
    # today (measured E1) — see `structured.output_mode` / `capabilities`. RECOMMENDED for a
    # step needing deep reasoning AND structured output: SPLIT into a `mode="text"` reasoning
    # step then a `mode="structured"` extraction step; this flag covers the single-step case.
    thinking: bool = False
    # Hard ceilings for deliberately bounded sub-calls; these may LOWER run-wide policy.
    iteration_limit: int | None = None
    output_token_limit: int | None = None
    structured_retry_limit: int | None = None
    transport_attempt_limit: int | None = None
    request_timeout_limit_s: int | None = None
    # Remove all tools after this pydantic-ai run step, leaving a final turn
    # that can summarize gathered evidence as text.
    tool_step_limit: int | None = None
    # Web-search flag (bug ff64), set by the plan-review routing seam for a criterion whose
    # routing entry declares ``"web": true`` (T1 initially). Honored on EVERY provider and
    # model since bug 129e — the provider's native tool where the model's profile supports it,
    # an in-process fallback where it does not; the old anthropic-prefix gate silently
    # withdrew a BLOCKING criterion's grounding tool at the Bedrock cutover. Every UNflagged
    # request is still byte-identical (no ``capabilities`` kwarg). DEFAULTED False.
    web: bool = False
    # Prompt-cache breakpoint control (bug 1dbe). When set, this is the byte-stable LEADING
    # segment of ``system_prompt`` at whose END the provider cache breakpoint should sit —
    # rather than at the end of the whole (per-pass-varying) system prompt. The runner seam
    # (``agent_call.build_agent_kwargs``) realises it by sending ``cache_prefix`` as the static
    # system block and the REMAINDER as a DYNAMIC instruction, which is where pydantic-ai's
    # anthropic/bedrock ``*_cache_instructions`` place the breakpoint (the last STATIC block).
    # The model still receives ``cache_prefix + remainder`` byte-identically — only the cache
    # boundary moves — so it is behaviour-preserving. Used by the plan-review passes that share
    # ``prompts.shared_plan_prefix(plan)`` so the finder's WRITE is READ by the Pass-2 verifier
    # (and a same-ticket re-review inside the TTL). Ignored unless it is a non-empty proper
    # prefix of ``system_prompt``. DEFAULTED None so every other caller is byte-unchanged.
    cache_prefix: str | None = None

    def effective_cache_prefix(self) -> str | None:
        """``cache_prefix`` iff it is a non-empty PROPER prefix of ``system_prompt``, else None.

        The SINGLE validity predicate the runner seam (``agent_call.build_agent_kwargs``), the
        marked-prefix estimate, and the workflow request builder all share (bug 1dbe) — so the
        "when does the breakpoint actually move" rule lives in one place and cannot drift. A
        None result means the breakpoint stays at the end of the whole system prompt (an unset,
        empty, non-matching, or full-length ``cache_prefix`` is a safe no-op)."""
        prefix = self.cache_prefix
        if (
            prefix
            and self.system_prompt.startswith(prefix)
            and len(prefix) < len(self.system_prompt)
        ):
            return prefix
        return None


@runtime_checkable
class Runner(Protocol):
    name: str

    def run(self, req: RunRequest) -> dict:
        """Execute the request and return a validated ``review_result`` dict."""
        ...

    def preflight(self) -> None:
        """Cheap, offline readiness check: raise ``LLMConfigError`` if this runner
        cannot run (e.g. the ``agents`` extra is absent or it is misconfigured),
        WITHOUT making a model/network call. Lets callers surface a clean
        degradation even on a no-op workload (e.g. a spec-scan with zero epics),
        so optionality failures never hide behind an empty batch loop."""
        ...


# ── Fake runner (offline / tests) ─────────────────────────────────────────────
class FakeRunner:
    """Deterministic runner that returns canned findings — no model, no network.

    The dependency-injection seam that lets the operation layer and the three
    interfaces be tested end-to-end without the ``agents`` extra or an API key."""

    name = "fake"

    def __init__(
        self,
        findings: list[dict] | None = None,
        summary: str | None = None,
        structured: dict | None = None,
    ):
        self._findings = findings or []
        self._summary = summary
        # Canned payload for ``mode="structured"`` ops (e.g. verify_completion): the raw
        # structured dict the agent would have emitted (e.g. {verdict, findings, summary}).
        self._structured = structured

    def preflight(self) -> None:
        """Always ready — no extra, no network."""

    def run(self, req: RunRequest) -> dict:
        # Structured mode (e.g. verify_completion): mirror finalize_outcome(mode="structured")
        # — return the canned payload validated against output_schema, plus provenance. The
        # operation does its own normalize/resolve/reconcile on top.
        if req.mode == "structured" and self._structured is not None:
            payload = _findings.validate_structured(dict(self._structured), req.output_schema)
            # Zeroed `_usage` so callers reading it off the run result (story 2948
            # successor-spend inheritance) see the real runner's key — a fake reports zero.
            return {**payload, "runner": self.name, "model": None, "trace_id": None, "_usage": {}}
        return _findings.finalize_findings(
            self._findings,
            runner=self.name,
            model=None,
            trace_id=None,
            target=req.target,
            reviewers=req.reviewers,
            summary=self._summary,
            reviewer_id=req.reviewers[0] if len(req.reviewers) == 1 else None,
            repo_path=req.config.repo_path,
        )


def _effective_config(base: LLMConfig, req: RunRequest) -> LLMConfig:
    """Return a per-request config copy without mutating or raising ``base`` policy.

    A runner instance is built ONCE per gate run and shared by every step, so ``base``
    is run-wide policy and ``req.config`` carries per-call model tuning. Bounded operations may
    additionally lower timeout/transport attempts; their effective values must be present before
    provider/model construction. Missing or empty model values retain the run-wide model.
    """
    updates: dict[str, Any] = {}
    requested = getattr(req.config, "model", None)
    if requested and requested != base.model:
        updates["model"] = requested
    if req.request_timeout_limit_s is not None:
        updates["timeout_s"] = min(base.timeout_s, max(1, int(req.request_timeout_limit_s)))
    if req.transport_attempt_limit is not None:
        updates["llm_retry_max_attempts"] = min(
            base.llm_retry_max_attempts, max(1, int(req.transport_attempt_limit))
        )
    return replace(base, **updates) if updates else base


# ── Pydantic AI runner (provider-agnostic, behind the same seam) ──────────────
class PydanticAIRunner:
    """Run an operation on a provider-agnostic Pydantic AI agent (epic
    hump-seam-spice / 7d58) — single-turn LLM calls AND tool-using agents with a full
    capability surface (filesystem + MCP + least-priv rebar ops) and NO per-provider
    code: the provider is chosen by the model string (``anthropic:…`` / ``openai:…`` /
    ``google-gla:…``). Structured output is selected by ``output_mode()`` —
    ``NativeOutput`` for providers with strict constrained decoding, ``PromptedOutput``
    for everyone else (including Anthropic under extended thinking UNLESS the model is
    measured to accept native output under thinking — see ``output_mode`` /
    ``capabilities.native_output_with_thinking``); no forced-tool
    ``ToolOutput`` is used anywhere in the stack. The structured-output reliability stack
    (NativeOutput/json-repair/bounded retry) is implemented in the structured module
    (story 1268).

    ``model_override`` injects a Pydantic AI model (e.g. ``TestModel``) for offline
    tests, exactly mirroring the ``FakeRunner`` seam without a live, billable call."""

    name = "pydantic_ai"

    def __init__(self, config: LLMConfig, *, model_override=None, runtime=None):
        self._config = config
        self._model_override = model_override
        # Optional non-serializable provider-native auth carrier (RP-04 S4). Threaded to the
        # per-run ProviderSession and consumed ONLY by the selected provider's builder; None
        # means byte-identical ambient (RP-01) construction.
        self._runtime = runtime

    def preflight(self) -> None:
        """Fail fast if the ``agents`` extra (pydantic-ai-slim) is absent or the config
        uses settings this runner does not yet honour — both offline, no model call.

        Attaches an ``.outcome`` disposition to a raised ``LLMError`` (story blackbear) so a
        preflight failure surfaces the same ``resolution_class`` channel as a mid-run outage
        (mamba's run seam) — a config error classifies non-retryable, so it maps to the
        existing INDETERMINATE exit, never the retryable exit 11."""
        try:
            _import_pydantic_ai()
            _pai_check_config(self._config)
        except LLMError as exc:
            from rebar.llm.failure import ClassifyContext, classify_llm_failure

            try:
                exc.outcome = classify_llm_failure(  # type: ignore[attr-defined]
                    exc, ClassifyContext(model=self._config.model)
                )
            except Exception:  # noqa: BLE001 — disposition is a hint; never mask the real error
                pass
            raise

    def run(self, req: RunRequest) -> dict:
        # Guard the agents extra FIRST — before importing any pydantic_ai submodule —
        # so an absent extra surfaces as a clean LLMConfigError (naming the extra), not a
        # raw ModuleNotFoundError from the `pydantic_ai.exceptions`/`.usage` imports below.
        # run() is reachable (library/CLI/MCP) without a preceding preflight().
        Agent = _import_pydantic_ai()

        from types import SimpleNamespace

        from pydantic_ai.usage import UsageLimits

        from rebar.llm import pai_tools

        cfg = _effective_config(self._config, req)
        _pai_check_config(cfg)
        # Best-effort OTLP→Langfuse tracing: no-op without the [tracing] extra / Langfuse
        # keys, never raises, idempotent. Write-only (never read back into a decision).
        from rebar.llm.tracing import setup_tracing

        setup_tracing(cfg.langfuse)
        # single_turn (story 4b2f): exactly ONE model call with NO tools/toolsets — the agent
        # cannot enter a tool loop. agentic: the full filesystem + rebar (+ MCP) tool surface.
        if req.execution_mode == "single_turn":
            tools: list = []
            toolsets: list = []
        else:
            # SAFEGUARD (epic raze-vet-ditch): a tool-using agent gets read-only file tools
            # over cfg.repo_path — which MUST be a gate-chosen read root (attested snapshot or
            # explicit local), never the server's mutable checkout reached by an op that
            # skipped the snapshot process. Fail closed here if no gate session is active.
            # Exempt a model_override run: that is the offline TestModel harness (it reads a
            # disposable tmp dir, never a production checkout), not a real agent operation.
            if self._model_override is None:
                from rebar.llm.config import assert_gated

                assert_gated("agentic filesystem tools")
            # Read-only ticket contract (the gates): in attested mode the agent reads a
            # PINNED snapshot copy of the ticket store, so a comment write would be lost —
            # withhold it. Local mode reads the live checkout, where a comment is a real
            # write, so it is allowed there. `current_code_root()` is set only in attested mode.
            from rebar.llm.config import current_code_root

            allow_comment = (not _readonly_gate()) and current_code_root() is None
            # The rebar ticket tools read the PINNED ticket-store snapshot when set, else the
            # in-place checkout's store. The file tools stay on the code snapshot.
            # `grounding_tools` adds `resolve_symbol` (bug 406f) so the finder can CONFIRM a
            # third-party/stdlib symbol the repo-scoped file tools cannot see.
            tools = (
                pai_tools.filesystem_tools(cfg.repo_path)
                + pai_tools.grounding_tools(cfg.repo_path)
                + pai_tools.rebar_tools(
                    cfg.tickets_path or cfg.repo_path, allow_comment=allow_comment
                )
            )
            if req.extra_tools:
                tools = [*tools, *req.extra_tools]
            toolsets = pai_tools.mcp_toolsets(cfg.mcp_servers)
        resolved = _pai_model(cfg)
        # A model class may configure its endpoint on the SLOT (`REBAR_LLM_<CLASS>_ENDPOINT` /
        # the `endpoint` field) rather than as a top-level `base_url`. Ops collapse the class
        # onto `cfg.model` via `resolve_model_string`, which drops that endpoint, so recover it
        # here from the resolved string (the same identity `fallback_targets_for` keys on) and
        # apply it as `base_url` when the operator set none — this registers rebar's
        # OpenAI-compatible builder for the PRIMARY model, stamps the endpoint host into the
        # signed provenance, and flips the tier to best_effort, uniformly with the top-level
        # `REBAR_LLM_BASE_URL` path. Without it the primary silently fell through to
        # pydantic-ai's stock OpenAIProvider and failed with "Missing credentials" (bug 6e70).
        # Never overrides an explicit `base_url`; a class with no slot endpoint is a no-op.
        # Both slot reads take the CONFIG's own root, not the ambient cwd (bug 2876).
        if not cfg.base_url and self._model_override is None:
            slot_endpoint = primary_endpoint_for(resolved, repo_root=cfg.repo_path)
            if slot_endpoint:
                cfg = replace(cfg, base_url=slot_endpoint)
        # Provider resolution is delegated to the per-run ProviderSession seam (story S1 /
        # one-provider-factory) — the ONE place answering "how is a Provider built for
        # provider X", incl. "it isn't": a rebar-registered provider (today, only anthropic)
        # is eagerly built via `infer_model(provider_factory=...)`, owning its client's
        # lifecycle via the session; a provider pydantic-ai recognizes but rebar does not
        # build (openai/google/...) stays a lazy model STRING for pydantic-ai's `Agent` to
        # resolve later, so that OPTIONAL package need not be importable yet; a name NEITHER
        # side recognizes raises the typed LLMConfigError HERE, so it can never be
        # misclassified by the broad `except Exception` further down as a provider OUTAGE.
        # `model_override` (the offline TestModel harness) bypasses all of this.
        # RP-04 S4: thread the optional native-auth runtime into the per-run session. Passed
        # only when present so an ambient run stays byte-identical to `ProviderSession(cfg)`
        # (the two are equivalent — `runtime` defaults to None).
        session_kwargs = {} if self._runtime is None else {"runtime": self._runtime}
        with ProviderSession(cfg, **session_kwargs) as provider_session:
            _provider_name = resolved.split(":", 1)[0] if ":" in resolved else resolved
            # The fallback chain (task cc33) of the model CLASS whose primary is `resolved` — empty
            # for a class with no `fallback` configured, which is every deployment until an operator
            # opts in, so the three branches below stay byte-identical there.
            _root = cfg.repo_path  # the CONFIG's own root, not the ambient cwd (bug 2876)
            no_chain = self._model_override is not None
            fallback_targets = () if no_chain else fallback_targets_for(resolved, repo_root=_root)
            candidates = [resolved]
            if self._model_override is not None:
                model = self._model_override
            elif fallback_targets:
                # A chain is built EAGERLY and WHOLE, on this ONE session: every candidate becomes a
                # real Model (so each entry's own `endpoint` is honored and every rebar-created
                # transport lands on one `_closeables`), then `FallbackModel` wraps them in order.
                model, candidates = build_fallback_model(
                    resolved, fallback_targets, session=provider_session
                )
            elif provider_session.supports(_provider_name):
                from pydantic_ai.models import infer_model

                model = infer_model(resolved, provider_factory=provider_session.provider_factory)
            elif provider_session.is_resolvable(_provider_name):
                model = resolved
            else:
                model = provider_session.provider_factory(
                    _provider_name
                )  # always raises LLMConfigError
            # Provenance records the PROVIDER-QUALIFIED string actually invoked (or a marker for
            # an injected test model), not the bare config model — so a parity diff sees exactly
            # what ran.
            ran_model = (
                f"test:{type(self._model_override).__name__}" if self._model_override else resolved
            )
            # Agent-build invariant (story anole): for a tool-using op on a REAL model object, fail
            # fast if the provider can't call tools (else pydantic-ai silently drops them). Gated
            # on model_override is None (the test double is never checked) and tools present.
            if self._model_override is None and tools:
                # Per CANDIDATE: the wrapper inherits the base `.profile`, whose defaults would pass
                # while a sub-model that cannot call tools sits in the chain, waiting to drop them.
                for candidate_model, candidate in zip(
                    getattr(model, "models", [model]), candidates, strict=True
                ):
                    _check_tool_capability(candidate_model, candidate)
            # Prompt caching (story 0250; capability-based since story S2). The stable bytes
            # re-sent across the container fan-out live in `system_prompt`;
            # `anthropic_cache_instructions` puts a `cache_control` breakpoint on that block
            # (anthropic.py:1611-1616, the no-instruction-parts branch caches the system prompt
            # block directly), and `anthropic_cache_tool_definitions` caches the tool surface on
            # agentic calls (a no-op on single_turn `tools=[]`). `capabilities_for` reads the
            # resolved model's PROFILE (never a provider-name string, so Bedrock-hosted Claude
            # still gets its cache keys) and `cache_settings_for` dispatches on the resulting
            # `prompt_cache_style`; each style's keys are provider-specific and would error on
            # an unrelated provider, so they are applied at THIS shared seam only — no
            # RunRequest content-list change, so the structured-output retry path is untouched.
            # Resolved ONCE, threaded into `_pai_structured` below: `model` (a real object, whose
            # PROFILE may carry a provider override, S4) for a real run, but `resolved` (the
            # config STRING) for `model_override` — its profile is irrelevant there, and every
            # model_override test pins the string behavior; cache_settings stays None then. A
            # chain resolves capabilities over the whole CANDIDATE SET
            # (`_intersect_capabilities`), never over the wrapper: `FallbackModel` carries no
            # profile and its `.provider` is None.
            if fallback_targets:
                caps = _intersect_capabilities([capabilities_for(m) for m in model.models])
            else:
                caps = capabilities_for(resolved if self._model_override is not None else model)
            cache_settings = (
                None
                if self._model_override
                else cache_settings_for(caps, execution_mode=req.execution_mode)
            )
            # Provider provenance (story S5/343b): stamp WHAT actually ran — resolved
            # provider/model, the endpoint host (None for the first-class/no-custom-base_url
            # path), and the effective capability record — onto the verdict, additively,
            # alongside the existing `model` string. Built from the SAME `caps` already
            # resolved above (never recomputed — see capabilities.provenance_for's docstring).
            # Web access (bug 129e). Resolved ONCE here so the capability actually attached and
            # the provenance that attests it cannot disagree. The ONLY thing that can withdraw it
            # is the injected-test-model path (`model_override`): a test double has no provider to
            # search with, and every model_override test pins the byte-identical wire shape. It is
            # NOT withdrawn for any real provider — that prefix-shaped withdrawal is the bug.
            web_requested = bool(req.web) and not self._model_override
            provider_provenance = provenance_for(
                provider=_provider_name,
                model=resolved,
                base_url=cfg.base_url,
                caps=caps,
                web=web_requested,
                bedrock_region_name=cfg.bedrock_region_name,
                bedrock_region_source=cfg.bedrock_region_source,
            )
            if fallback_targets:
                # A verdict produced by a fallback must not attest the primary. The ordered
                # candidate set is known now; `ran_model` is filled in after the run from the
                # response that actually answered (the wrapper's own name is the synthetic
                # `fallback:a,b` string).
                provider_provenance["candidates"] = list(candidates)
            # Web search (bug ff64, universalised by bug 129e): the provider's native tool where
            # its profile supports it, an in-process fallback everywhere else — no provider gate.
            # An unflagged request stays byte-identical (no ``capabilities`` key).
            web_caps = web_search_capabilities(web=web_requested)
            # Model settings + usage limits are built by the ADR 0056 decision-3 leaf helpers
            # (structured_run.py); see their docstrings for the max_tokens/timeout/temperature/
            # step-budget rationale (bug ids, measured numbers, and invariants preserved there).
            model_settings = build_model_settings(
                cfg, req, caps, resolved, cache_settings, model_override=self._model_override
            )
            # Sampling-temperature withdrawal (story S3/2932: e.g. `us.anthropic.claude-opus-4-7`
            # 400s "temperature is deprecated for this model") is decided PURELY inside
            # ``build_model_settings`` (no logging there — see its docstring and
            # ``_TEMPERATURE_WITHDRAWN_LOGGED`` below). The once-per-model dedup INFO log for that
            # withdrawal stays HERE, the only place allowed to mutate the dedup set.
            temperature = getattr(req.config, "temperature", None)
            if temperature is None:
                temperature = cfg.temperature
            if temperature is not None and not caps.supports_temperature:
                if resolved not in _TEMPERATURE_WITHDRAWN_LOGGED:
                    _TEMPERATURE_WITHDRAWN_LOGGED.add(resolved)
                    logger.info(
                        "llm: omitting temperature for model=%s — measured to reject it "
                        "(capabilities.supports_temperature is False)",
                        resolved,
                    )
            # The Agent kwargs (incl. the `tool_step_limit` convergence rewrite of the tool
            # surface) are assembled by the ADR 0056 decision-3 leaf helper in `agent_call.py`.
            # `web_caps` is resolved HERE and passed IN so the patchable
            # `web_search_capabilities` global stays a `runner` name.
            kwargs = build_agent_kwargs(
                cfg, req, tools, toolsets, model_settings=model_settings, web_caps=web_caps
            )
            # RP-01 S2: merge shared bounded-op seams (wire projection + output-retry counter).
            apply_structured_seams(kwargs, req, candidates, model_settings)
            usage_limits, req_limit, eff_max_iter = build_usage_limits(cfg, req, UsageLimits)
            # Observability (one structured record per LLM call): which reviewer/criterion,
            # execution mode, model, and wall-clock — so a slow/serial fan-out (e.g. the
            # container per-child loop) is visible without a debugger. Quiet by default;
            # enable with REBAR_LOG_LEVEL=INFO. Failures log at WARNING.
            _call_label = (
                ",".join(req.reviewers) if req.reviewers else (req.target.get("ticket_id") or "?")
            )
            _t0 = time.monotonic()
            # Widened from `dict[str, int]` for bug aec1: the run-shape keys merged in after a
            # successful call are not all ints (`finish_reason` is a string,
            # `top_repeated_tool_calls` a list of dicts). The token counters it carries are still
            # ints; only the annotation admits the shape fields alongside them.
            usage: dict[str, Any] = {}
            run_messages: list[Any] = []
            # `agent.run_sync` never enters the model (only `async with agent` does), so a chain's
            # sub-models — and thus the HTTP client lifecycle pydantic-ai's OWN providers manage
            # through that entry — would never be entered. A plain model has no such requirement.
            model_scope = ExitStack()
            if fallback_targets:
                model_scope.enter_context(entered_fallback_model(model))
            try:
                if req.mode == "text":
                    agent = Agent(model, **kwargs)
                    # Install this thread's event loop right before the synchronous run so
                    # pydantic-ai's `run_sync` does not resolve it through the deprecated
                    # `asyncio.get_event_loop()` fallback (ticket c7d5). Kept adjacent to the
                    # run — not hoisted earlier — so loop binding timing matches building the
                    # provider/model first, preserving provider client loop affinity.
                    ensure_current_event_loop()
                    with usage_log.collect_failure_messages(run_messages):
                        with usage_log.capture_attempt_messages():
                            run_result = agent.run_sync(req.instructions, usage_limits=usage_limits)
                    # Text is an intermediate artifact for bounded evidence
                    # gathering. A provider-truncated fragment is incomplete
                    # evidence and must never be handed to a verdict finalizer.
                    from rebar.llm import structured as _structured

                    if req.tool_step_limit is not None:
                        _structured.check_response(run_result.response)
                    outcome = {"messages": [SimpleNamespace(content=str(run_result.output))]}
                    usage = _extract_usage(run_result)
                else:
                    with usage_log.collect_failure_messages(run_messages):
                        # Only forward `artifact_dir` when the operator opted in, so the
                        # default path calls `_pai_structured` with its historical arg list.
                        _extra = (
                            {"artifact_dir": cfg.parse_failure_artifact_dir}
                            if cfg.parse_failure_artifact_dir
                            else {}
                        )
                        structured, usage = _pai_structured(
                            Agent, model, caps, req, kwargs, usage_limits, **_extra
                        )
                    outcome = {"structured_response": structured}
                # Agent-build invariant (story anole): telemetry warning on a REAL run whose
                # usage looks zeroed (never blocks; test doubles report zero usage, so skip them).
                if self._model_override is None:
                    _warn_if_zeroed_usage(usage)
                    # Cache-effectiveness telemetry (story S3/2932): caching can fail SILENTLY on
                    # some Bedrock models (MEASURED: cache_read=0 AND cache_write=0 while billing
                    # full input tokens, no error) — this is the signal an operator otherwise never
                    # sees. `cache_settings is not None` is the existing local for "caching was
                    # requested this call" (set above from `cache_settings_for(caps)`).
                    # Bug e3cd: measure the MARKED PREFIX against THIS model's documented
                    # floor. The old call compared total `input_tokens` against a model-blind
                    # 4096, which both missed real failures on low-floor models (opus-4-8,
                    # rebar's DEFAULT_MODEL, is 1024) and named the wrong quantity when a big
                    # payload rode unmarked after the breakpoint.
                    warn_if_cache_ineffective(
                        usage,
                        caching_requested=cache_settings is not None,
                        model=ran_model,
                        marked_prefix_tokens=estimate_marked_prefix_tokens(
                            cache_settings,
                            # Bug 1dbe: when the caller relocated the breakpoint to a
                            # ``cache_prefix``, the MARKED span is that prefix — not the whole
                            # system prompt. ``effective_cache_prefix`` owns the validity check
                            # (proper, non-empty prefix), falling back to the full system prompt.
                            system_prompt=req.effective_cache_prefix() or req.system_prompt,
                        ),
                        cache_min_prefix_tokens=caps.cache_min_prefix_tokens,
                    )
            except Exception as exc:  # noqa: BLE001 — the except spine is `interpret_failure`
                # (ADR 0056 decision 3, src/rebar/llm/structured_run.py): it dispatches on
                # UsageLimitExceeded / LLMError / anything-else in that load-bearing order and
                # always raises, so this broad catch is exactly as narrow as the three arms it
                # replaces.
                # Because it always raises, run()'s success-path `usage_log.record(...)` below is
                # unreachable here, so a call that burned tokens and THEN failed used to leave no
                # spend row (8455) — write it now; rules in `record_failure`'s docstring.
                usage_log.record_failure(
                    run_messages,
                    _call_label,
                    ran_model,
                    req_limit,
                    eff_max_iter,
                    duration_s=time.monotonic() - _t0,
                    ticket=req.target.get("ticket_id"),
                )
                # The spend row above goes to the JSONL usage log; this counts the SAME failure
                # in memory so a run that survives it can report it on the verdict coverage
                # (eclectic-industrial-argali). Most sub-steps swallow their failure and degrade
                # silently, which made repeated degradation invisible to a JSON consumer. A
                # no-op outside an active sink scope, and it cannot raise.
                step_failures.record(_call_label)
                interpret_failure(
                    exc,
                    run_messages,
                    FailureContext(
                        call_label=_call_label,
                        execution_mode=req.execution_mode,
                        ran_model=ran_model,
                        req_limit=req_limit,
                        eff_max_iter=eff_max_iter,
                        started_at=_t0,
                    ),
                )
            finally:
                # Exit the chain wrapper FIRST so pydantic-ai closes the clients IT owns, then
                # close whatever rebar's builders opened (story arcticduck / S1). The two sets
                # are disjoint — a provider handed a client never adopts it — so nothing is
                # closed twice.
                model_scope.close()
        if fallback_targets:
            # Attest the model that ANSWERED, read off the response rather than the wrapper.
            answered = _answering_model(run_messages, candidates)
            if answered is not None:
                ran_model = answered
            provider_provenance["ran_model"] = ran_model
        # Emitted BEFORE finalize_outcome and it must stay there — see log_call_success's
        # docstring: finalization can raise, and this record is wanted when it does.
        log_call_success(
            usage,
            call_label=_call_label,
            execution_mode=req.execution_mode,
            ran_model=ran_model,
            req_limit=req_limit,
            eff_max_iter=eff_max_iter,
            started_at=_t0,
        )
        result = _findings.finalize_outcome(
            outcome,
            mode=req.mode,
            output_schema=req.output_schema,
            runner=self.name,
            model=ran_model,
            provider_provenance=provider_provenance,
            trace_id=None,
            target=req.target,
            reviewers=req.reviewers,
            repo_path=cfg.repo_path,
            reviewer_id=req.reviewers[0] if len(req.reviewers) == 1 else None,
        )
        # Surface per-run token usage (incl. anthropic cache read/write) so callers can
        # record cache efficacy into coverage/observability. Private key — non-breaking
        # for every existing consumer of the review_result/structured dict.
        result["_usage"] = usage
        _merge_success_run_shape(
            usage,
            run_messages,
            request_limit=req_limit,
            tool_calls_limit=max(8, eff_max_iter),
            call_label=_call_label,
        )
        # The opt-in spend row (rules, and the step-attribution rationale, in its docstring).
        record_call_spend(
            usage,
            call_label=_call_label,
            ran_model=ran_model,
            duration_s=time.monotonic() - _t0,
            ticket=req.target.get("ticket_id"),
        )
        return result


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


def get_runner(config: LLMConfig, *, runtime=None, override: Runner | None = None) -> Runner:
    """Select the runner for ``config`` (or use an explicit ``override``, the test
    injection seam). ``pydantic_ai`` (default) requires the ``agents`` extra; ``fake``
    is the offline test seam. ``runtime`` is the optional non-serializable provider-native
    auth carrier (RP-04 S4), threaded into the ``pydantic_ai`` runner's per-run
    ``ProviderSession``; it is inert for ``fake``/``override`` runners."""
    if override is not None:
        return override
    if config.runner == "fake":
        return FakeRunner()
    if config.runner == "pydantic_ai":
        return PydanticAIRunner(config, runtime=runtime)
    # from_env only ever derives a valid runner; a bad value can only come from an
    # explicit library LLMConfig(runner=...). Fail loudly rather than silently
    # running the default, naming the valid set (RUNNERS).
    from rebar.llm.config import RUNNERS

    raise LLMConfigError(f"unknown runner {config.runner!r}; valid runners: {RUNNERS}")


# ── lazy imports + helpers ────────────────────────────────────────────────────
# The stateless helper cluster (`_check_tool_capability` + its `_TOOL_CAPABILITY_CHECKED`
# cache, `_intersect_capabilities`, `_answering_model`, `_readonly_gate`) lives in
# `runner_support` (imported at module top) to keep this module under the size cap; those
# names remain importable from `rebar.llm.runner` for callers and tests.
