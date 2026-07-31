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
import math
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rebar.llm import findings as _findings
from rebar.llm import usage_log
from rebar.llm.anthropic_model import (
    _DIRECT_ANTHROPIC_BASE_URL,  # noqa: F401  (re-exported for tests / back-compat)
    _anthropic_web_search_capabilities,
    _local_proxy_bypass_base_url,  # noqa: F401  (re-exported for tests / back-compat)
    _pai_model,
)
from rebar.llm.capabilities import (
    ModelCapabilities,
    cache_settings_for,
    capabilities_for,
    provenance_for,
)
from rebar.llm.config import LLMConfig, infer_provider
from rebar.llm.errors import (
    LLMConfigError,
    LLMError,
    LLMRunnerError,
    LLMUnavailableError,
)
from rebar.llm.model_classes import (
    build_fallback_model,
    entered_fallback_model,
    fallback_targets_for,
)
from rebar.llm.providers import ProviderSession
from rebar.llm.structured_run import (
    _extract_usage,
    _import_pydantic_ai,  # noqa: F401  (re-exported: tests patch it on `runner`)
    _pai_check_config,  # noqa: F401  (re-exported: tests import it from `runner`)
    _pai_structured,
    _warn_if_zeroed_usage,
    effective_max_iterations,
    effective_max_tokens,
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
    # Per-operation extra tools appended to the agent's tool list (e.g. a read-only
    # rebar ``show_ticket`` for the completion verifier). DEFAULTED None so existing
    # review callers are unchanged. (Post-cutover the pydantic_ai runner supplies
    # show_ticket natively, so this is always None in practice.)
    extra_tools: list | None = None
    # Extended-thinking flag (1268). When set, the structured-output stack uses
    # PromptedOutput rather than a provider-native/strict constraint — a CURRENT
    # Anthropic API constraint (it 400s when extended thinking is on together with a
    # forced/native output constraint), not a workaround for any forced-tool mechanism
    # (none exists in the stack; output is selected by `output_mode()`). The RECOMMENDED
    # authoring pattern for a step that needs deep reasoning AND structured output is to
    # SPLIT it into two steps — a `mode="text"` reasoning step then a `mode="structured"`
    # extraction step (both already supported by the engine) — rather than forcing one
    # step to do both; this flag covers the single-step case.
    thinking: bool = False
    # Hard ceilings for deliberately bounded exploratory sub-calls. Unlike
    # ``config.max_iterations`` these may LOWER an operation-wide floor.
    iteration_limit: int | None = None
    output_token_limit: int | None = None
    # Remove all tools after this pydantic-ai run step, leaving a final turn
    # that can summarize gathered evidence as text.
    tool_step_limit: int | None = None
    # Server-side web-search flag (bug ff64), set by the plan-review routing seam for a
    # criterion whose routing entry declares ``"web": true`` (T1 initially). Honored ONLY
    # on an anthropic-resolved model (the server-side ``web_search`` capability); every
    # other provider and every unflagged request is byte-identical. DEFAULTED False.
    web: bool = False


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
            return {**payload, "runner": self.name, "model": None, "trace_id": None}
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


# ── Pydantic AI runner (provider-agnostic, behind the same seam) ──────────────
class PydanticAIRunner:
    """Run an operation on a provider-agnostic Pydantic AI agent (epic
    hump-seam-spice / 7d58) — single-turn LLM calls AND tool-using agents with a full
    capability surface (filesystem + MCP + least-priv rebar ops) and NO per-provider
    code: the provider is chosen by the model string (``anthropic:…`` / ``openai:…`` /
    ``google-gla:…``). Structured output is selected by ``output_mode()`` —
    ``NativeOutput`` for providers with strict constrained decoding, ``PromptedOutput``
    for everyone else (including Anthropic when extended thinking is active, which
    Anthropic 400s if combined with a forced/native output constraint); no forced-tool
    ``ToolOutput`` is used anywhere in the stack. The structured-output reliability stack
    (NativeOutput/json-repair/bounded retry) is implemented in the structured module
    (story 1268).

    ``model_override`` injects a Pydantic AI model (e.g. ``TestModel``) for offline
    tests, exactly mirroring the ``FakeRunner`` seam without a live, billable call."""

    name = "pydantic_ai"

    def __init__(self, config: LLMConfig, *, model_override=None):
        self._config = config
        self._model_override = model_override

    def preflight(self) -> None:
        """Fail fast if the ``agents`` extra (pydantic-ai-slim) is absent or the config
        uses settings this runner does not yet honour — both offline, no model call.

        Attaches an ``.outcome`` disposition to a raised ``LLMError`` (story blackbear) so a
        preflight failure surfaces the same ``resolution_class`` channel as a mid-run outage
        (mamba's run seam) — a config error classifies non-retryable, so it maps to the
        existing INDETERMINATE exit, never the retryable exit 11."""
        from rebar.llm.errors import LLMError

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

        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits

        from rebar.llm import pai_tools

        cfg = self._config
        _pai_check_config(cfg)
        # Best-effort OTLP→Langfuse tracing: no-op without the [tracing] extra / Langfuse
        # keys, never raises, idempotent. Write-only (never read back into a decision).
        from rebar.llm.tracing import setup_tracing

        setup_tracing(cfg.langfuse)
        # single_turn (story 4b2f): exactly ONE model call with NO tools and NO
        # toolsets — the agent cannot enter a tool loop. agentic: the full
        # filesystem + rebar (+ MCP) tool surface, as before.
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
            # PINNED snapshot copy of the ticket store, so a comment write would land in a
            # throwaway dir and be lost — withhold it. (REBAR_MCP_READONLY also withholds it.)
            # Local mode reads the live checkout, where a comment is a real write, so it is
            # allowed there. `current_code_root()` is set only in attested mode.
            from rebar.llm.config import current_code_root

            allow_comment = (not _readonly_gate()) and current_code_root() is None
            # The rebar ticket tools read the PINNED ticket-store snapshot when set (the
            # orphan `tickets` branch is absent from the code snapshot `cfg.repo_path`),
            # else the in-place checkout's store. The file tools stay on the code snapshot.
            # `grounding_tools` adds the environment-aware `resolve_symbol` (bug 406f)
            # so the finder can CONFIRM a third-party/stdlib symbol the repo-scoped
            # file tools cannot see, rather than asserting it is hallucinated.
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
        # Provider resolution is delegated to the per-run ProviderSession seam (story
        # S1 / one-provider-factory) — the ONE place that answers "how is a Provider
        # built for provider X", including when the answer is "it isn't":
        #   - a rebar-registered provider (today, only anthropic) is eagerly built
        #     through `infer_model(provider_factory=session.provider_factory)`, which
        #     owns its client's lifecycle via the session;
        #   - a provider pydantic-ai itself recognizes but rebar does not build
        #     (openai/google/...) is left as a lazy model STRING for pydantic-ai's own
        #     `Agent` construction to resolve later, exactly as before this seam
        #     existed — eagerly building it here would force that provider's OPTIONAL
        #     package (openai/google are opt-in, never installed by the `agents`
        #     extra) to be importable just to wire up model_settings below, before any
        #     real call is made: a regression this seam must not introduce;
        #   - a name NEITHER side recognizes raises the typed LLMConfigError HERE
        #     (before any Agent/tool-loop work), so it can never be misclassified by
        #     the broad `except Exception` further down as a provider OUTAGE — the
        #     opposite of what a misspelled/unsupported provider name actually is.
        # `model_override` (the offline TestModel harness) bypasses all of this and
        # builds no client.
        provider_session = ProviderSession(cfg)
        _provider_name = resolved.split(":", 1)[0] if ":" in resolved else resolved
        # The fallback chain (task cc33) of the model CLASS whose primary is `resolved` — empty
        # for a class with no `fallback` configured, which is every deployment until an operator
        # opts in, so the three branches below stay byte-identical there.
        fallback_targets = (
            () if self._model_override is not None else fallback_targets_for(resolved)
        )
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
        # Provenance records the PROVIDER-QUALIFIED string actually invoked (or a marker
        # for an injected test model), not the bare config model — so a parity diff sees
        # exactly what ran.
        ran_model = (
            f"test:{type(self._model_override).__name__}" if self._model_override else resolved
        )
        # Agent-build invariant (story anole): for a tool-using op on a REAL model object,
        # fail fast if the provider can't call tools (else pydantic-ai silently drops them).
        # Gated on model_override is None (the test double is never checked) and tools present.
        if self._model_override is None and tools:
            # Per CANDIDATE: the wrapper inherits the base `.profile`, whose defaults would pass
            # while a sub-model that cannot call tools sits in the chain, waiting to drop them.
            for candidate_model, candidate in zip(
                getattr(model, "models", [model]), candidates, strict=True
            ):
                _check_tool_capability(candidate_model, candidate)
        if req.tool_step_limit is not None and tools:
            # Executable convergence boundary. This is intentionally not a
            # forced structured-output tool on the exploratory history.
            from pydantic_ai.toolsets import FunctionToolset

            limit = max(0, int(req.tool_step_limit))

            def available(run_ctx, _tool_def):
                return run_ctx.run_step <= limit

            all_toolsets = [FunctionToolset(tools), *toolsets]
            tools = []
            toolsets = [toolset.filtered(available) for toolset in all_toolsets]

        kwargs: dict[str, Any] = {
            "system_prompt": req.system_prompt,
            "tools": tools,
            "toolsets": toolsets,
            # Per-tool execution timeout (story hoopoe): bounds a hung ASYNC/MCP tool. A
            # no-op on single_turn (tools=[]) and for sync in-process tools (async cancel
            # can't interrupt a blocking call — those are bounded by the derived step caps).
            "tool_timeout": float(cfg.llm_tool_timeout_s),
        }
        # Prompt caching (story 0250; capability-based since story S2). The stable bytes
        # re-sent across the container fan-out (the WHOLE parent plan) live in
        # `system_prompt`; `anthropic_cache_instructions` puts a `cache_control` breakpoint
        # on that block (anthropic.py:1611-1616, the no-instruction-parts branch caches the
        # system prompt block directly), and `anthropic_cache_tool_definitions` caches the
        # tool surface on agentic calls (a no-op on single_turn `tools=[]`). `capabilities_for`
        # reads the resolved model's PROFILE (never a provider-name string, so Bedrock-hosted
        # Claude — whose model string says `bedrock`, not `anthropic` — still gets its cache
        # keys) and `cache_settings_for` dispatches on the resulting `prompt_cache_style`;
        # each style's keys are provider-specific and would error on an unrelated provider, so
        # they are applied at THIS shared seam only — no RunRequest content-list change, so the
        # structured-output retry path is untouched.
        # Resolved ONCE, threaded into `_pai_structured` below (never disagree): `model` (a real
        # object, whose PROFILE may carry a provider override, S4) for a real run, but `resolved`
        # (the config STRING) for `model_override` — its profile is irrelevant/misleading there,
        # and every model_override test pins the string behavior; cache_settings stays None then.
        # A chain resolves capabilities over the whole CANDIDATE SET (see
        # `_intersect_capabilities`), never over the wrapper: `FallbackModel` carries no profile
        # and its `.provider` is None, so asking IT returns DEFAULT capabilities silently.
        if fallback_targets:
            caps = _intersect_capabilities([capabilities_for(m) for m in model.models])
        else:
            caps = capabilities_for(resolved if self._model_override is not None else model)
        cache_settings = None if self._model_override else cache_settings_for(caps)
        # Provider provenance (story S5/343b): stamp WHAT actually ran — resolved
        # provider/model, the endpoint host (None for the first-class/no-custom-base_url
        # path), and the effective capability record — onto the verdict, additively,
        # alongside the existing `model` string. Built from the SAME `caps` already
        # resolved above (never recomputed — see capabilities.provenance_for's docstring).
        provider_provenance = provenance_for(
            provider=_provider_name, model=resolved, base_url=cfg.base_url, caps=caps
        )
        if fallback_targets:
            # A verdict produced by a fallback must not attest the primary. The ordered candidate
            # set is known now; `ran_model` is filled in after the run from the response that
            # actually answered (the wrapper's own name is the synthetic `fallback:a,b` string).
            provider_provenance["candidates"] = list(candidates)
        # Server-side web search (bug ff64) — anthropic-GATED like the cache settings
        # above (an injected test model never gets a provider server tool). Attached as a
        # pydantic-ai capability; any non-flagged-anthropic request stays byte-identical
        # (no ``capabilities`` key).
        web_caps = _anthropic_web_search_capabilities(
            resolved if not self._model_override else "", web=req.web
        )
        if web_caps is not None:
            kwargs["capabilities"] = web_caps
        # Wire the configured OUTPUT cap into the call. cfg.max_tokens was previously DROPPED
        # (only the cache flags were sent as model_settings), so pydantic-ai fell back to its
        # max_tokens=4096 default — far too small for a multi-child container review, whose
        # output truncated (stop_reason=max_tokens) and tripped the structured-output retry.
        # max_tokens is a base ModelSettings field, so it rides alongside the cache flags.
        model_settings = dict(cache_settings) if cache_settings is not None else {}
        # The output cap is PER-REQUEST too (bug spy-luge-wool / sole-teal-churn): a finding-rich
        # Pass-2 verifier carries a scaled max_tokens on ``req.config`` so its structured output
        # doesn't truncate (finish_reason=length), without mutating a shared runner's self._config.
        # A request can only RAISE the configured floor, never lower it.
        eff_max_tokens = effective_max_tokens(
            cfg.max_tokens, getattr(req.config, "max_tokens", None)
        )
        if req.output_token_limit is not None:
            eff_max_tokens = min(eff_max_tokens, max(256, int(req.output_token_limit)))
        if eff_max_tokens:
            model_settings["max_tokens"] = eff_max_tokens
        # Wire the configured wall-clock timeout so the operator's REBAR_LLM_TIMEOUT
        # actually bounds each LLM call. Audit reliability #6: cfg.timeout_s was resolved
        # into LLMConfig but never passed to the model, so every call silently rode the
        # Anthropic SDK's ~600 s default regardless of the operator's setting. `timeout`
        # is a base ModelSettings field mapping to the underlying httpx/Anthropic client
        # request timeout. DEFAULT_TIMEOUT_S (600) equals the SDK default, so an unset
        # knob is never lowered below it; an explicit operator value is honored verbatim.
        if cfg.timeout_s:
            model_settings["timeout"] = float(cfg.timeout_s)
        # Sampling temperature (upstream review-code report §2): a per-request override on
        # ``req.config`` wins over the runner's own cfg, mirroring the max_tokens seam above; both
        # are None by default so the provider default is used (byte-unchanged). The Pass-2 verify
        # steps carry temperature=0 (greedy) so re-running the same finding does not resample its
        # narrow verification and flip a block/advisory decision.
        temperature = getattr(req.config, "temperature", None)
        if temperature is None:
            temperature = cfg.temperature
        if temperature is not None:
            # Withdraw `temperature` for a model MEASURED to reject it (story S3/2932: e.g.
            # `us.anthropic.claude-opus-4-7` 400s "temperature is deprecated for this model").
            # `caps` (resolved once, above) already carries this per-model fact, so this is a
            # capability check, never a provider-name branch. Logged once (not per-call) via
            # the resolved-model dedup set below.
            if not caps.supports_temperature:
                if resolved not in _TEMPERATURE_WITHDRAWN_LOGGED:
                    _TEMPERATURE_WITHDRAWN_LOGGED.add(resolved)
                    logger.info(
                        "llm: omitting temperature for model=%s — measured to reject it "
                        "(capabilities.supports_temperature is False)",
                        resolved,
                    )
            else:
                model_settings["temperature"] = float(temperature)
        if model_settings:
            kwargs["model_settings"] = model_settings
        # pydantic-ai's request_limit counts MODEL REQUESTS (~1 per tool-call cycle).
        # Halve cfg.max_iterations (which is authored as ~2 steps per tool-call cycle)
        # so a given cfg.max_iterations allows the intended number of tool-call cycles
        # (and so we DON'T silently inherit pydantic-ai's default request_limit=50).
        # request_limit bounds model TURNS, not tool calls WITHIN a turn — a tool that fails
        # and gets re-called can spray many calls in few turns (pydantic-ai #2593). Add
        # tool_calls_limit as the in-turn backstop so a failing/looping tool cannot burn the
        # whole budget (the retry-to-exhaustion failure mode). Set generously above the
        # expected ~max_iterations/2 tool calls, so it only trips on a genuine runaway.
        # The step budget is PER-REQUEST: a caller (e.g. the workflow agent step) may raise
        # max_iterations for THIS call by carrying a higher value on ``req.config`` — needed so a
        # finding-rich Pass-2 verifier gets a budget scaled to its work without a shared runner's
        # self._config changing under other steps (bug 59bc). The request can only RAISE the floor
        # (``max``), never lower the operator-configured budget. ``self._config`` (cfg) is the
        # floor; req.config is the per-call override.
        eff_max_iter = effective_max_iterations(
            cfg.max_iterations, getattr(req.config, "max_iterations", None)
        )
        if req.iteration_limit is not None:
            eff_max_iter = min(eff_max_iter, max(2, int(req.iteration_limit)))
        # The model-REQUEST ceiling (~1 per tool-call cycle). Bound to a LOCAL so the telemetry
        # logs report it directly instead of reading it back off the UsageLimits object (which a
        # test may stub) — and so the step-usage line reports the EFFECTIVE per-request budget.
        req_limit = max(1, math.ceil(eff_max_iter / 2))
        usage_limits = UsageLimits(
            request_limit=req_limit,
            tool_calls_limit=max(8, eff_max_iter),
        )
        # Observability (one structured record per LLM call): which reviewer/criterion,
        # execution mode, model, and wall-clock — so a slow/serial fan-out (e.g. the
        # container per-child loop) is visible without a debugger. Quiet by default;
        # enable with REBAR_LOG_LEVEL=INFO. Failures log at WARNING.
        _call_label = (
            ",".join(req.reviewers) if req.reviewers else (req.target.get("ticket_id") or "?")
        )
        _t0 = time.monotonic()
        usage: dict[str, int] = {}
        run_messages: list[Any] = []
        # `agent.run_sync` never enters the model (only `async with agent` does), so a chain's
        # sub-models — and thus the HTTP client lifecycle pydantic-ai's OWN providers manage
        # through that entry — would never be entered. A plain model has no such requirement,
        # so this stack stays empty there.
        model_scope = ExitStack()
        if fallback_targets:
            model_scope.enter_context(entered_fallback_model(model))
        try:
            if req.mode == "text":
                agent = Agent(model, **kwargs)
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
                    structured, usage = _pai_structured(
                        Agent, model, caps, req, kwargs, usage_limits
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
                warn_if_cache_ineffective(
                    usage, caching_requested=cache_settings is not None, model=ran_model
                )
        except UsageLimitExceeded as exc:
            budget_diag = usage_log.failure_usage(
                run_messages, request_limit=req_limit, tool_calls_limit=max(8, eff_max_iter)
            )
            logger.warning(
                "llm call [%s] mode=%s model=%s hit step budget "
                "(request_limit=%d max_iterations=%d) in %.1fs %s",
                _call_label,
                req.execution_mode,
                ran_model,
                req_limit,
                eff_max_iter,
                time.monotonic() - _t0,
                usage_log.format_repetition(budget_diag),
            )
            budget_err = LLMRunnerError(
                f"agent exceeded its step budget (max_iterations={eff_max_iter}; "
                "~1 model request per tool call). Raise REBAR_LLM_MAX_STEPS or narrow "
                "the task."
            )
            budget_err.diagnostic = budget_diag  # type: ignore[attr-defined]
            raise budget_err from exc
        except LLMError as exc:
            # Preserve the typed failure while attaching bounded counters from
            # the failed run (no prompt/tool content).
            exc.diagnostic = usage_log.failure_usage(  # type: ignore[attr-defined]
                run_messages,
                request_limit=req_limit,
                tool_calls_limit=max(8, eff_max_iter),
            )
            raise
        except Exception as exc:  # noqa: BLE001 — a SYSTEMIC provider failure (auth / missing
            # key / connection / rate-limit). Unify into the provider-agnostic
            # LLMUnavailableError so every prompt-using client gets ONE recognizable
            # "LLM couldn't run" signal — never a swallowed empty result (fuel-posse-ball).
            # Tried FIRST (story S3/2932): a provider rejecting a sampling parameter (e.g.
            # Bedrock's "temperature is deprecated for this model" on a model NOT in the
            # capabilities.py denylist) must fail LOUDLY and ACTIONABLY, not be misclassified
            # as an opaque outage by the broad LLMUnavailableError path below. Only when this
            # returns None (not a sampling-parameter rejection) does the existing path run,
            # unchanged.
            from rebar.llm.failure import translate_sampling_parameter_rejection

            sampling_err = translate_sampling_parameter_rejection(exc, ran_model)
            if sampling_err is not None:
                sampling_err.diagnostic = usage_log.failure_usage(  # type: ignore[attr-defined]
                    run_messages,
                    request_limit=req_limit,
                    tool_calls_limit=max(8, eff_max_iter),
                )
                raise sampling_err from exc
            logger.warning(
                "llm call [%s] mode=%s model=%s FAILED in %.1fs: %s",
                _call_label,
                req.execution_mode,
                ran_model,
                time.monotonic() - _t0,
                exc,
            )
            provider_err = LLMUnavailableError(f"the LLM provider call failed: {exc}")
            provider_err.diagnostic = usage_log.failure_usage(  # type: ignore[attr-defined]
                run_messages,
                request_limit=req_limit,
                tool_calls_limit=max(8, eff_max_iter),
            )
            # Attach the classified disposition as METADATA (story civilized-immediate-mamba).
            # This does NOT change the raised type — every existing `except LLMUnavailableError`
            # still catches, and the per-seam wiring + exit-code use is story blackbear's. Kept
            # total (classify_llm_failure never raises), so enriching the error can't mask it.
            from rebar.llm.failure import ClassifyContext, classify_llm_failure

            provider_err.outcome = classify_llm_failure(  # type: ignore[attr-defined]
                exc, ClassifyContext(model=ran_model)
            )
            raise provider_err from exc
        finally:
            # Exit the chain wrapper FIRST so pydantic-ai closes the clients IT owns, then close
            # whatever rebar's builders opened (story arcticduck / S1). The two sets are disjoint
            # — a provider handed a client never adopts it — so nothing is closed twice.
            model_scope.close()
            # ProviderSession.close() is itself best-effort (log, never raise).
            provider_session.close()
        if fallback_targets:
            # Attest the model that ANSWERED, read off the response rather than the wrapper.
            answered = _answering_model(run_messages, candidates)
            if answered is not None:
                ran_model = answered
            provider_provenance["ran_model"] = ran_model
        logger.info(
            "llm call [%s] mode=%s model=%s ok in %.1fs "
            "steps=%d/%d budget=%d (in=%d out=%d cache_read=%d cache_write=%d)",
            _call_label,
            req.execution_mode,
            ran_model,
            time.monotonic() - _t0,
            # Step-usage telemetry: model requests CONSUMED vs the request ceiling
            # (≈ max_iterations/2) and the authored step budget. One structured line per
            # run, so the verifier/reviewer step floors can be sized from observed headroom
            # (grep `llm call [completion-verifier]` / `[plan-reviewer]` and aggregate).
            usage.get("requests", 0),
            req_limit,
            eff_max_iter,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("cache_read_tokens", 0),
            usage.get("cache_write_tokens", 0),
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
        # Durable, opt-in spend record for the weekly billable CI jobs (no-op unless
        # REBAR_USAGE_LOG is set) — the runner is the one chokepoint shared by both the
        # external tier and the live prompt-eval, so a single sink covers both.
        usage_log.record(usage, op=_call_label, model=ran_model, provider=infer_provider(ran_model))
        return result


def get_runner(config: LLMConfig, *, override: Runner | None = None) -> Runner:
    """Select the runner for ``config`` (or use an explicit ``override``, the test
    injection seam). ``pydantic_ai`` (default) requires the ``agents`` extra; ``fake``
    is the offline test seam."""
    if override is not None:
        return override
    if config.runner == "fake":
        return FakeRunner()
    if config.runner == "pydantic_ai":
        return PydanticAIRunner(config)
    # from_env only ever derives a valid runner; a bad value can only come from an
    # explicit library LLMConfig(runner=...). Fail loudly rather than silently
    # running the default, naming the valid set (RUNNERS).
    from rebar.llm.config import RUNNERS

    raise LLMConfigError(f"unknown runner {config.runner!r}; valid runners: {RUNNERS}")


# ── lazy imports + helpers ────────────────────────────────────────────────────
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
        supports_temperature=all(c.supports_temperature for c in per_candidate),
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
    malformed config fails CLOSED (read-only). Previously this read ONLY the env var
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
