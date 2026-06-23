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

import math
import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rebar.llm import findings as _findings
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError, LLMRunnerError, StructuredOutputError


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
    # Per-operation extra tools appended to the agent's tool list (e.g. a read-only
    # rebar ``show_ticket`` for the completion verifier). DEFAULTED None so existing
    # review callers are unchanged. (Post-cutover the pydantic_ai runner supplies
    # show_ticket natively, so this is always None in practice.)
    extra_tools: list | None = None
    # Historical structured-output strategy knob (kept for the RunRequest contract).
    # The pydantic_ai runtime always concludes naturally and parses/validates the
    # structured output via the reliability stack, so this field no longer drives
    # runner behaviour.
    output_strategy: str = "tool"
    # Extended-thinking flag (1268). When set, the structured-output stack uses
    # PromptedOutput (NOT a forced/native constraint, which Anthropic 400s when thinking
    # is on). The RECOMMENDED authoring pattern for a step that needs deep reasoning AND
    # structured output is to SPLIT it into two steps — a `mode="text"` reasoning step
    # then a `mode="structured"` extraction step (both already supported by the engine) —
    # rather than forcing one step to do both; this flag covers the single-step case.
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
    ``google-gla:…``). Structured output uses ``PromptedOutput`` (NOT the forced-tool
    ``ToolOutput``), the provider-agnostic mode the runtime POC proved also dodges the
    Claude extended-thinking + forced-tool 400. The reliability hardening
    (NativeOutput/json-repair/bounded retry) is layered on in a later story (1268).

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
        Agent = _import_pydantic_ai()
        tools = pai_tools.filesystem_tools(cfg.repo_path) + pai_tools.rebar_tools(
            cfg.repo_path, allow_comment=not _readonly_gate()
        )
        if req.extra_tools:
            tools = [*tools, *req.extra_tools]
        toolsets = pai_tools.mcp_toolsets(cfg.mcp_servers)
        resolved = _pai_model(cfg)
        model = self._model_override or resolved
        # Provenance records the PROVIDER-QUALIFIED string actually invoked (or a marker
        # for an injected test model), not the bare config model — so a parity diff sees
        # exactly what ran.
        ran_model = (
            f"test:{type(self._model_override).__name__}" if self._model_override else resolved
        )
        kwargs = {"system_prompt": req.system_prompt, "tools": tools, "toolsets": toolsets}
        # pydantic-ai's request_limit counts MODEL REQUESTS (~1 per tool-call cycle).
        # Halve cfg.max_iterations (which is authored as ~2 steps per tool-call cycle)
        # so a given cfg.max_iterations allows the intended number of tool-call cycles
        # (and so we DON'T silently inherit pydantic-ai's default request_limit=50).
        usage_limits = UsageLimits(request_limit=max(1, math.ceil(cfg.max_iterations / 2)))
        try:
            if req.mode == "text":
                agent = Agent(model, **kwargs)
                output = agent.run_sync(req.instructions, usage_limits=usage_limits).output
                outcome = {"messages": [SimpleNamespace(content=str(output))]}
            else:
                outcome = {
                    "structured_response": _pai_structured(
                        Agent, model, resolved, req, kwargs, usage_limits
                    )
                }
        except UsageLimitExceeded as exc:
            raise LLMRunnerError(
                f"agent exceeded its step budget (max_iterations={cfg.max_iterations}; "
                "~1 model request per tool call). Raise REBAR_LLM_MAX_STEPS or narrow "
                "the task."
            ) from exc
        return _findings.finalize_outcome(
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


def _pai_structured(Agent, model, resolved: str, req: RunRequest, kwargs: dict, usage_limits):
    """Obtain a validated structured object via the reliability stack (1268).

    NATIVE path: where the provider enforces a strict json_schema (output_mode ->
    NativeOutput), Pydantic AI does constrained decoding + validation + the bounded
    retry — no json-repair needed. PROMPTED path (everyone else, incl. Anthropic):
    generate FREE TEXT, then run the DETERMINISTIC tolerant parse (json-repair) +
    Pydantic validators, with a single bounded retry that feeds the validation error
    back to the SAME model (NOT a second interpreter LLM). Returns the validated
    Pydantic model instance."""
    from pydantic_ai import NativeOutput

    from rebar.llm import contracts, structured

    model_cls = contracts.response_model_for(req.output_schema)
    mode_obj = structured.output_mode(model_cls, resolved, thinking=req.thinking)
    if isinstance(mode_obj, NativeOutput):
        agent = Agent(
            model, output_type=mode_obj, retries={"output": structured.OUTPUT_RETRIES}, **kwargs
        )
        return agent.run_sync(req.instructions, usage_limits=usage_limits).output

    # PromptedOutput case: free-text + deterministic parse/validate + bounded retry.
    agent = Agent(model, **kwargs)  # free text (output_type defaults to str)
    prompt = req.instructions
    last: Exception | None = None
    for _ in range(structured.OUTPUT_RETRIES + 1):
        result = agent.run_sync(prompt, usage_limits=usage_limits)
        try:
            # A refused / TRUNCATED turn is surfaced as a clear error BEFORE the tolerant
            # parse — else json-repair would "fix" a truncated fragment into a
            # plausible-but-wrong object (the false-accept the stop-reason guard prevents).
            structured.check_stop_reason(getattr(result.response, "finish_reason", None))
            return structured.parse_structured(str(result.output), model_cls)
        except StructuredOutputError as exc:
            last = exc
            prompt = (
                f"{req.instructions}\n\nYour previous reply could not be parsed/validated "
                f"({exc}). Reply with ONLY the JSON object matching the schema — no prose, "
                f"no code fence."
            )
    raise last  # exhausted the bounded retry; surface the last validation error


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
    """True if the READONLY gate is set (``REBAR_MCP_READONLY`` truthy) — reused to
    withhold the comment tool, so a read-only deployment grants the agent read-only
    ticket access. Case-insensitive truthy (1/true/yes/on)."""
    val = os.environ.get("REBAR_MCP_READONLY", "").strip().lower()
    return val in ("1", "true", "yes", "on")
