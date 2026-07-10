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
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rebar.llm import findings as _findings
from rebar.llm.config import LLMConfig
from rebar.llm.errors import (
    LLMConfigError,
    LLMError,
    LLMRunnerError,
    LLMUnavailableError,
    StructuredOutputError,
    UnretryableOutputError,
)

logger = logging.getLogger(__name__)


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
        uses settings this runner does not yet honour — both offline, no model call."""
        _import_pydantic_ai()
        _pai_check_config(self._config)

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
            tools = pai_tools.filesystem_tools(cfg.repo_path) + pai_tools.rebar_tools(
                cfg.tickets_path or cfg.repo_path, allow_comment=allow_comment
            )
            if req.extra_tools:
                tools = [*tools, *req.extra_tools]
            toolsets = pai_tools.mcp_toolsets(cfg.mcp_servers)
        resolved = _pai_model(cfg)
        model = self._model_override or resolved
        # Exclude rebar's own agent from a LOCAL Claude-Code payload optimizer (bug
        # sue-skimp-tear): when ANTHROPIC_BASE_URL points at a loopback proxy (e.g.
        # headroom on 127.0.0.1), it corrupts our multi-turn AGENTIC tool-loop requests
        # into an empty provider stream, collapsing the plan-review/completion verifiers to
        # INDETERMINATE. Pin an Anthropic model to the DIRECT public API so rebar bypasses
        # the local proxy; real (non-loopback) gateways and the test model_override path are
        # left untouched, and REBAR_LLM_ALLOW_LOCAL_PROXY=1 opts back in.
        # The retrying httpx.AsyncClient to close on teardown (story arcticduck); None on the
        # non-anthropic / model_override paths, which build no client here.
        _http_client = None
        if self._model_override is None and resolved.startswith("anthropic"):
            # ONE unified construction for ANY anthropic model (normal AND loopback-bypass):
            # both get the retrying transport with SDK max_retries=0. `_direct` (None on the
            # normal path) only varies the base_url. Before this, the normal path let
            # pydantic-ai build its own provider with the SDK default retries and no transport.
            _direct = _local_proxy_bypass_base_url()
            _name = resolved.split(":", 1)[1] if ":" in resolved else resolved
            # Per-request READ timeout (story hoopoe): the transport-level bound on a hung
            # model, reusing cfg.timeout_s. This is authoritative on the anthropic path (our
            # custom client); non-anthropic providers keep the model_settings['timeout'] below.
            import httpx as _httpx

            _http_timeout = _httpx.Timeout(
                read=float(cfg.timeout_s), connect=10.0, write=30.0, pool=10.0
            )
            model, _http_client = _build_retrying_anthropic_model(
                _name, base_url=_direct, cfg=cfg, http_timeout=_http_timeout
            )
        # Provenance records the PROVIDER-QUALIFIED string actually invoked (or a marker
        # for an injected test model), not the bare config model — so a parity diff sees
        # exactly what ran.
        ran_model = (
            f"test:{type(self._model_override).__name__}" if self._model_override else resolved
        )
        kwargs: dict[str, Any] = {
            "system_prompt": req.system_prompt,
            "tools": tools,
            "toolsets": toolsets,
            # Per-tool execution timeout (story hoopoe): bounds a hung ASYNC/MCP tool. A
            # no-op on single_turn (tools=[]) and for sync in-process tools (async cancel
            # can't interrupt a blocking call — those are bounded by the derived step caps).
            "tool_timeout": float(cfg.llm_tool_timeout_s),
        }
        # Prompt caching (story 0250) — anthropic-GATED. The stable bytes re-sent across
        # the container fan-out (the WHOLE parent plan) live in `system_prompt`;
        # `anthropic_cache_instructions` puts a `cache_control` breakpoint on that block
        # (anthropic.py:1611-1616, the no-instruction-parts branch caches the system
        # prompt block directly), and `anthropic_cache_tool_definitions` caches the tool
        # surface on agentic calls (a no-op on single_turn `tools=[]`). These keys are
        # anthropic-only and would error on openai/gemini, so they are gated to the
        # resolved anthropic provider and applied at THIS shared seam only — no
        # RunRequest content-list change, so the structured-output retry path is
        # untouched. A test model_override is non-anthropic, so caching is off there.
        cache_settings = _anthropic_cache_settings(resolved if not self._model_override else "")
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
        try:
            if req.mode == "text":
                agent = Agent(model, **kwargs)
                run_result = agent.run_sync(req.instructions, usage_limits=usage_limits)
                outcome = {"messages": [SimpleNamespace(content=str(run_result.output))]}
                usage = _extract_usage(run_result)
            else:
                structured, usage = _pai_structured(
                    Agent, model, resolved, req, kwargs, usage_limits
                )
                outcome = {"structured_response": structured}
        except UsageLimitExceeded as exc:
            logger.warning(
                "llm call [%s] mode=%s model=%s hit step budget "
                "(request_limit=%d max_iterations=%d) in %.1fs",
                _call_label,
                req.execution_mode,
                ran_model,
                req_limit,
                eff_max_iter,
                time.monotonic() - _t0,
            )
            raise LLMRunnerError(
                f"agent exceeded its step budget (max_iterations={eff_max_iter}; "
                "~1 model request per tool call). Raise REBAR_LLM_MAX_STEPS or narrow "
                "the task."
            ) from exc
        except LLMError:
            raise  # our own typed errors (e.g. StructuredOutputError) pass through unchanged
        except Exception as exc:  # noqa: BLE001 — a SYSTEMIC provider failure (auth / missing
            # key / connection / rate-limit). Unify into the provider-agnostic
            # LLMUnavailableError so every prompt-using client gets ONE recognizable
            # "LLM couldn't run" signal — never a swallowed empty result (fuel-posse-ball).
            logger.warning(
                "llm call [%s] mode=%s model=%s FAILED in %.1fs: %s",
                _call_label,
                req.execution_mode,
                ran_model,
                time.monotonic() - _t0,
                exc,
            )
            err = LLMUnavailableError(f"the LLM provider call failed: {exc}")
            # Attach the classified disposition as METADATA (story civilized-immediate-mamba).
            # This does NOT change the raised type — every existing `except LLMUnavailableError`
            # still catches, and the per-seam wiring + exit-code use is story blackbear's. Kept
            # total (classify_llm_failure never raises), so enriching the error can't mask it.
            from rebar.llm.failure import ClassifyContext, classify_llm_failure

            err.outcome = classify_llm_failure(exc, ClassifyContext(model=ran_model))  # type: ignore[attr-defined]
            raise err from exc
        finally:
            # Close the per-run retrying httpx.AsyncClient (story arcticduck). aclose() is
            # async; after run_sync()'s own loop is gone, the stdlib asyncio.run() closes the
            # pool from this synchronous caller. Best-effort — cleanup never fails the run.
            if _http_client is not None:
                import asyncio

                try:
                    asyncio.run(_http_client.aclose())
                except Exception:  # noqa: BLE001 — teardown is best-effort; log, never raise
                    logger.warning("llm transport client aclose failed on teardown", exc_info=True)
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
def _import_pydantic_ai():
    try:
        from pydantic_ai import Agent
    except ImportError as exc:
        raise LLMConfigError(
            "the pydantic_ai runner needs the 'agents' extra (pydantic-ai-slim). "
            "Install it with: pip install 'nava-rebar[agents]'"
        ) from exc
    return Agent


# Internal provider names -> the Pydantic AI model-string prefix. A small, declarative
# map (NOT per-provider behaviour) so the provider is chosen purely by the model string.
_PAI_PROVIDER_PREFIX = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google_genai": "google-gla",
    "google": "google-gla",
}


# Max chars of the model's faulty prior reply echoed back in the bounded-retry reask
# (story drake) — enough to diff a near-miss, bounded so a huge blob can't balloon the prompt.
_FAULTY_OUTPUT_SNIPPET_CHARS = 2000


def _pai_structured(Agent, model, resolved: str, req: RunRequest, kwargs: dict, usage_limits):
    """Obtain a validated structured object via the reliability stack (1268).

    NATIVE path: where the provider enforces a strict json_schema (output_mode ->
    NativeOutput), Pydantic AI does constrained decoding + validation + the bounded
    retry — no json-repair needed. PROMPTED path (everyone else, incl. Anthropic):
    generate FREE TEXT, then run the DETERMINISTIC tolerant parse (json-repair) +
    Pydantic validators, with a single bounded retry that feeds the validation error
    back to the SAME model (NOT a second interpreter LLM). Returns
    ``(validated_model_instance, usage_dict)`` — the usage of the run that produced the
    accepted output (story 0250 cache-token observability)."""
    from pydantic_ai import NativeOutput

    from rebar.llm import contracts, structured

    model_cls = contracts.response_model_for(req.output_schema)
    mode_obj = structured.output_mode(model_cls, resolved, thinking=req.thinking)
    if isinstance(mode_obj, NativeOutput):
        agent = Agent(
            model, output_type=mode_obj, retries={"output": structured.OUTPUT_RETRIES}, **kwargs
        )
        run_result = agent.run_sync(req.instructions, usage_limits=usage_limits)
        # Silent-success parity (story drake): the PromptedOutput path below already checks
        # the stop reason; the NativeOutput path previously returned output DIRECTLY, so a
        # truncated/refused NativeOutput turn was returned as a hollow verdict. Run the same
        # check here — a length/max_tokens/content_filter/refusal finish_reason raises
        # UnretryableOutputError → the gate degrades to INDETERMINATE, never a hollow PASS.
        structured.check_stop_reason(getattr(run_result.response, "finish_reason", None))
        return run_result.output, _extract_usage(run_result)

    # PromptedOutput case: free-text + deterministic parse/validate + bounded retry. The
    # schema directive is appended so the model knows the EXACT output keys (the json-repair
    # path generates free text, so — unlike NativeOutput/PromptedOutput-as-output_type — the
    # schema is not otherwise conveyed; without it the model guesses keys and tolerant parsing
    # drops them to an empty object).
    agent = Agent(model, **kwargs)  # free text (output_type defaults to str)
    schema_hint = structured.schema_directive(model_cls)
    prompt = f"{req.instructions}\n\n{schema_hint}"
    last: Exception | None = None
    for _ in range(structured.OUTPUT_RETRIES + 1):
        result = agent.run_sync(prompt, usage_limits=usage_limits)
        try:
            # A refused / TRUNCATED turn is surfaced as a clear error BEFORE the tolerant
            # parse — else json-repair would "fix" a truncated fragment into a
            # plausible-but-wrong object (the false-accept the stop-reason guard prevents).
            structured.check_stop_reason(getattr(result.response, "finish_reason", None))
            parsed = structured.parse_structured(str(result.output), model_cls)
            return parsed, _extract_usage(result)
        except UnretryableOutputError:
            # A truncation (hit the output cap), refusal, or content-filter is a complete,
            # unusable turn — re-running the same call reproduces it. FAST-FAIL instead of
            # re-paying this (often agentic, multi-minute) call OUTPUT_RETRIES more times.
            raise
        except StructuredOutputError as exc:
            last = exc
            # Feed the model its OWN faulty prior reply (bounded) so it can diff its mistake
            # — the LangChain RetryWithErrorOutputParser / Instructor pattern (story drake).
            faulty = str(result.output)
            if len(faulty) > _FAULTY_OUTPUT_SNIPPET_CHARS:
                faulty = faulty[:_FAULTY_OUTPUT_SNIPPET_CHARS] + " …[truncated]"
            prompt = (
                f"{req.instructions}\n\n{schema_hint}\n\nYour previous reply could not be "
                f"parsed/validated ({exc}). Your previous reply was:\n{faulty}\n\n"
                f"Reply with ONLY the JSON object matching the schema above — no prose, "
                f"no code fence."
            )
    assert last is not None  # the loop only exits here after a failed parse set `last`
    raise last  # exhausted the bounded retry; surface the last validation error


def effective_max_iterations(floor: int, requested: int | None) -> int:
    """The PER-REQUEST agent step budget (bug 59bc). A caller may RAISE the budget for a single
    call by carrying a higher ``max_iterations`` on its ``RunRequest.config`` (e.g. the Pass-2
    verifier scaled by its finding count), without mutating a shared runner's ``self._config``
    under other steps. The request can only raise the operator-configured floor, never lower it —
    so ``max(floor, requested)``; a missing/None request value leaves the floor untouched."""
    return max(floor, requested or floor)


def effective_max_tokens(floor: int, requested: int | None) -> int:
    """The PER-REQUEST output-token cap (bug spy-luge-wool / sole-teal-churn) — the exact analogue
    of :func:`effective_max_iterations` for the per-call OUTPUT budget. A finding-rich Pass-2 verify
    emits ~1 verification object per finding, so its structured output overflows a fixed cap
    (``finish_reason=length``) and the whole review collapses to INDETERMINATE. A caller scales
    the cap for a single call via ``RunRequest.config.max_tokens``; it can only RAISE the operator
    floor, never lower it — ``max(floor, requested)`` — a missing/None request leaves it as-is."""
    return max(floor, requested or floor)


def _extract_usage(run_result) -> dict[str, int]:
    """Pull the per-run token usage off a pydantic-ai ``AgentRunResult`` (story 0250).

    Pins the pydantic-ai 1.107.0 ``RunUsage`` field names — note the library NORMALIZES
    Anthropic's raw ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` to
    ``cache_read_tokens`` / ``cache_write_tokens`` (usage.py:194-200). Also reads
    ``requests`` — the model-REQUEST count for this run, the step-usage signal the
    ``max_iterations`` / ``request_limit`` budget bounds (so a run's headroom against the
    step floor is observable; used to size the verifier/reviewer floors from data rather
    than guesswork). Defensive: a missing ``.usage()`` (e.g. an injected test model) yields
    an empty dict, never an error — usage is observability, never load-bearing."""
    try:
        # pydantic-ai 1.107.0 deprecates the ``.usage()`` METHOD in favour of the
        # ``.usage`` PROPERTY (which exposes the token attrs directly). Read the
        # property's attrs — only fall back to CALLING it for a legacy build where
        # ``.usage`` is still a bare method (no attrs), so we never trip the
        # call-the-property deprecation warning on the supported version.
        u = run_result.usage
        if not hasattr(u, "input_tokens") and callable(u):
            u = u()
    except Exception:  # noqa: BLE001 — usage is best-effort observability, never fails a run
        return {}
    return {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(u, "cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(u, "cache_write_tokens", 0) or 0),
        # The model-REQUEST count (~1 per agentic tool-call cycle). Surfaced so Pass-2
        # verify step usage is observable vs its budget (the agentic verifier's
        # step-budget headroom — bug 59bc); 0/absent for a single-turn call.
        "requests": int(getattr(u, "requests", 0) or 0),
    }


def _anthropic_cache_settings(resolved: str):
    """Anthropic-GATED prompt-cache model settings, or ``None`` for any other provider
    (story 0250). ``anthropic_cache_instructions`` puts a ``cache_control`` breakpoint on
    the system-prompt block (the byte-stable parent plan); ``anthropic_cache_tool_definitions``
    caches the tool surface on agentic calls. Both keys live on ``AnthropicModelSettings``
    and error on openai/gemini, so they are emitted ONLY when the resolved model string is
    anthropic-qualified — on every other provider the call is unchanged (no cache_* sent)."""
    if not resolved.startswith("anthropic"):
        return None
    from pydantic_ai.models.anthropic import AnthropicModelSettings

    return AnthropicModelSettings(
        anthropic_cache_instructions=True,
        anthropic_cache_tool_definitions=True,
    )


def _pai_check_config(cfg: LLMConfig) -> None:
    """Refuse, LOUDLY, config this runner does not yet honour rather than silently
    dropping it. The Pydantic AI runner picks the model purely from the model string,
    so an explicit ``base_url`` / ``api_key`` (OpenAI-compatible local servers) would
    be silently ignored — a real capability gap. Fail with a clear message until it is
    wired through a Pydantic AI provider object."""
    unsupported = [k for k in ("base_url", "api_key") if getattr(cfg, k, None)]
    if unsupported:
        raise LLMConfigError(
            f"the pydantic_ai runner does not yet support {unsupported} "
            f"(OpenAI-compatible local-server config); omit these settings. "
            f"Not silently ignored."
        )


_DIRECT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def _local_proxy_bypass_base_url() -> str | None:
    """The DIRECT Anthropic base_url to use INSTEAD of a loopback ``ANTHROPIC_BASE_URL``,
    or ``None`` when no bypass applies.

    A local Claude-Code payload optimizer (e.g. headroom on ``127.0.0.1``) inherited via
    ``ANTHROPIC_BASE_URL`` corrupts rebar's own multi-turn agentic tool-loop requests into
    an empty provider stream (bug sue-skimp-tear), so rebar's internal agent must talk to
    Anthropic directly. Returns the direct public API URL ONLY when ``ANTHROPIC_BASE_URL``
    is set to a loopback host; a non-loopback gateway is respected (``None``), an unset var
    is a no-op (``None``), and ``REBAR_LLM_ALLOW_LOCAL_PROXY`` truthy opts back in
    (``None``)."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if not base:
        return None
    if os.environ.get("REBAR_LLM_ALLOW_LOCAL_PROXY", "").strip().lower() in ("1", "true", "yes"):
        return None
    from urllib.parse import urlparse

    host = (urlparse(base).hostname or "").strip().lower()
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return _DIRECT_ANTHROPIC_BASE_URL
    return None


# HTTP statuses the transport retries. 529 (Anthropic overloaded) is included explicitly —
# pydantic-ai's sample retry list omits it. Status codes are not exceptions by default, so
# `validate_response` raises for them (below) to make the retry predicate fire.
_RETRY_STATUSES = frozenset({429, 529, 500, 502, 503, 504})


def _build_retrying_anthropic_model(
    name: str, *, base_url: str | None, cfg: LLMConfig, http_timeout=None, _wrapped_transport=None
):
    """Build an ``AnthropicModel`` whose ``AsyncAnthropic`` client carries a retrying
    ``AsyncTenacityTransport`` (story morbid-uncultured-arcticduck). Retry is owned SOLELY by
    the transport (SDK ``max_retries=0``); a construction-time guard fails fast rather than
    silently regress to SDK-managed retries. Returns ``(model, http_client)`` — the caller
    closes ``http_client`` on run teardown via ``asyncio.run(http_client.aclose())``.

    ``base_url=None`` uses the Anthropic SDK default (the normal path); a non-empty value is
    the loopback-proxy-bypass direct URL. ``http_timeout`` is story hoopoe's per-attempt
    ``httpx.Timeout`` when present, else a bounded default from ``cfg.timeout_s`` (never
    unbounded). A transient ``{429,529,5xx}``/``httpx.TimeoutException``/``httpx.NetworkError``
    blip is re-sent BELOW the agent loop, so completed tool calls are never re-executed;
    ``Retry-After`` is honored (capped at ``llm_retry_max_wait_s``), else exponential backoff."""
    import httpx
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
    from tenacity import retry_if_exception_type, stop_after_attempt

    def _validate_response(response: httpx.Response) -> None:
        if response.status_code in _RETRY_STATUSES:
            response.raise_for_status()

    def _before_sleep(state) -> None:
        sleep = getattr(getattr(state, "next_action", None), "sleep", None)
        logger.warning(
            "llm transport retry: attempt %d failed, sleeping %.1fs before retry",
            state.attempt_number,
            float(sleep or 0.0),
        )

    attempts = max(1, int(cfg.llm_retry_max_attempts))
    transport = AsyncTenacityTransport(
        config=RetryConfig(
            retry=(
                retry_if_exception_type(httpx.HTTPStatusError)
                | retry_if_exception_type(httpx.TimeoutException)
                | retry_if_exception_type(httpx.NetworkError)
            ),
            wait=wait_retry_after(fallback_strategy=None, max_wait=float(cfg.llm_retry_max_wait_s)),
            stop=stop_after_attempt(attempts),
            reraise=True,
            before_sleep=_before_sleep,
        ),
        # ``_wrapped_transport`` is a test seam (a MockTransport); production uses the real
        # httpx transport.
        wrapped=_wrapped_transport
        if _wrapped_transport is not None
        else httpx.AsyncHTTPTransport(),
        validate_response=_validate_response,
    )
    timeout = http_timeout if http_timeout is not None else httpx.Timeout(float(cfg.timeout_s))
    http_client = httpx.AsyncClient(transport=transport, timeout=timeout)
    anthropic_client = AsyncAnthropic(
        base_url=base_url or None, max_retries=0, http_client=http_client
    )
    # Construction-time guard: never silently regress to SDK-managed retries.
    if anthropic_client.max_retries != 0:
        raise LLMConfigError(
            "transport-retry guard: AsyncAnthropic.max_retries must be 0 "
            "(retry is owned by the httpx transport, not the SDK)"
        )
    model = AnthropicModel(name, provider=AnthropicProvider(anthropic_client=anthropic_client))
    return model, http_client


def _pai_model(cfg: LLMConfig):
    """The Pydantic AI model string for ``cfg`` (provider-qualified). If ``cfg.model``
    already carries a ``provider:`` prefix it is used verbatim; otherwise the provider
    is inferred (or taken from ``cfg.model_provider``) and mapped to Pydantic AI's
    prefix — no per-provider code, the string is the only switch."""
    m = cfg.model
    if ":" in m:
        return m
    from rebar.llm.config import infer_provider

    prov = cfg.model_provider or infer_provider(m, None)
    prefix = _PAI_PROVIDER_PREFIX.get(prov or "", prov)
    return f"{prefix}:{m}" if prefix else m


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
