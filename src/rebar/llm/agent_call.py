"""The Agent-call EDGE cluster — the kwargs handed to one ``Agent(...)`` invocation and the
two records emitted after it returns (ADR 0056 decision 3, the last of its four extractions).

This module holds the blocks ADR 0056 names and its earlier stories left inline:
``build_agent_kwargs`` (the fourth named extraction) and the post-call telemetry pair the
ADR's Consequences section calls out.

``structured_run.py`` is the natural sibling — ``build_agent_kwargs`` consumes what
``build_model_settings`` returns — but could not host them without pushing it past the
``100 <= loc < 500`` band ``test_structured_run_seam.py`` pins on it.

These three functions do NOT call each other. What binds them is that they are the EDGES of
exactly one Agent call, sharing ``run()``'s per-call locals (``usage``, ``ran_model``,
``call_label``, ``req_limit``, ``eff_max_iter``); ``structured_run.py`` keeps its distinct job
of DRIVING the structured-output retry loop and computing the settings.

Leaf module (the ``structured_run.py`` / ``capabilities.py`` convention): imports NOTHING from
``runner`` at runtime — the one annotation naming ``RunRequest`` is deferred via
``from __future__ import annotations`` + ``TYPE_CHECKING`` — so ``import rebar.llm`` stays
stdlib-only and the dependency direction is one-way (``runner`` -> ``agent_call``, never back).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from rebar.llm import usage_log
from rebar.llm.config import infer_provider

if TYPE_CHECKING:
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import RunRequest

logger = logging.getLogger(__name__)


def build_agent_kwargs(
    cfg: LLMConfig,
    req: RunRequest,
    tools: list,
    toolsets: list,
    *,
    model_settings: dict | None,
    web_caps: Any | None,
) -> dict[str, Any]:
    """Assemble the kwargs dict handed to ``Agent(model, **kwargs)`` and to
    ``_pai_structured``, plus the ``tool_step_limit`` rewrite that produces the tool surface
    it stores (ADR 0056 decision 3; plain parameters, no carrier — the signature does not
    reach the ADR's ~6-parameter threshold for one).

    ``web_caps`` is passed IN rather than computed here, deliberately:
    ``web_search_capabilities`` is a ``runner`` module global the suite reaches via
    ``monkeypatch.setattr(runner_mod, ...)``, and moving its CALL SITE into this module would
    resolve the name through THIS module's globals and silently defeat such a patch — the
    hazard ADR 0056's Consequences section records. Keeping the call in ``run()`` means this
    extraction needs no monkeypatch repoint at all. (It also keeps ONE seam deciding web access
    and the provenance that attests it — bug 129e.)

    Both optional keys are OMITTED rather than set to ``None`` when absent: a present-but-None
    value would reach the provider, so an unflagged request must stay byte-identical to the
    pre-capability era."""
    if req.tool_step_limit is not None and tools:
        # Executable convergence boundary — intentionally not a forced tool.
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
    # Prompt-cache breakpoint relocation (bug 1dbe). When the caller named a byte-stable
    # ``cache_prefix`` (the plan-review passes' ``shared_plan_prefix``), split the system into
    # the static PREFIX block + a DYNAMIC instruction carrying the REMAINDER, so pydantic-ai's
    # anthropic/bedrock ``*_cache_instructions`` place the breakpoint at the PREFIX boundary
    # (they mark the last STATIC block; a dynamic instruction after it is excluded). The model
    # still receives prefix+remainder byte-identically — only the cache boundary moves — so
    # this is behaviour-preserving. ``effective_cache_prefix`` owns the non-empty PROPER-prefix
    # guard; any other value (or None) leaves the byte-identical single-string system prompt
    # above untouched.
    prefix = req.effective_cache_prefix()
    if prefix is not None:
        remainder = req.system_prompt[len(prefix) :]

        # A zero-argument FUNCTION (a closure over ``remainder``) makes pydantic-ai mark this a
        # DYNAMIC instruction, which is what moves the cache breakpoint ahead of it onto the
        # static prefix block. It must take NO parameters: a parameter (even one with a default)
        # is read by pydantic-ai as a RunContext arg and breaks instruction assembly.
        def _remainder_instructions() -> str:
            return remainder

        kwargs["system_prompt"] = prefix
        kwargs["instructions"] = _remainder_instructions
    # Web search (bug ff64; provider-independent since bug 129e) — resolved by the caller for
    # every provider alike; an UNflagged request stays byte-identical (no ``capabilities`` key).
    if web_caps is not None:
        kwargs["capabilities"] = web_caps
    if model_settings:
        kwargs["model_settings"] = model_settings
    return kwargs


def log_call_success(
    usage: dict,
    *,
    call_label: str,
    execution_mode: str,
    ran_model: str,
    req_limit: int,
    eff_max_iter: int,
    started_at: float,
) -> None:
    """Observability — one structured record per successful LLM call: which
    reviewer/criterion, execution mode, model, and wall-clock, so a slow/serial fan-out (e.g.
    the container per-child loop) is visible without a debugger. Quiet by default; enable with
    REBAR_LOG_LEVEL=INFO. Failures log at WARNING via ``interpret_failure``.

    Called BEFORE ``finalize_outcome`` in ``run()`` and it must STAY there: finalization can
    raise (schema validation), and this line is already emitted when it does — moving it after
    would lose the record for exactly the runs whose outcome is in question."""
    logger.info(
        "llm call [%s] mode=%s model=%s ok in %.1fs "
        "steps=%d/%d budget=%d (in=%d out=%d cache_read=%d cache_write=%d)",
        call_label,
        execution_mode,
        ran_model,
        time.monotonic() - started_at,
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


def record_call_spend(
    usage: dict,
    *,
    call_label: str,
    ran_model: str,
    duration_s: float | None = None,
    ticket: str | None = None,
) -> None:
    """Durable spend record for the weekly billable CI jobs (and, since bug aec1, for a gate
    session's default ``.rebar/usage.jsonl`` — ``usage_log._resolve_sink`` owns that precedence
    and the no-op case) — the runner is the one chokepoint shared by both the external tier and
    the live prompt-eval, so a single sink covers both.

    Attribute the row to the workflow step that made the call (b690). ``op`` is the PROMPT
    name, which cannot separate steps that share a prompt, so the step id and the raw declared
    class token ride in on a ContextVar the step executor binds. Both are absent for a call
    made outside any step, and ``record`` omits them accordingly.

    ``duration_s``/``ticket`` (bug aec1) are optional and forwarded verbatim: the runner owns
    both facts (it holds the ``time.monotonic()`` start and the request target), so they are
    passed IN rather than recomputed here — this module is an edge helper with no access to
    either, and re-deriving a duration from a later clock read would measure the wrong span."""
    step = usage_log.active_step()
    step_id, model_token = step if step is not None else (None, None)
    usage_log.record(
        usage,
        op=call_label,
        model=ran_model,
        provider=infer_provider(ran_model),
        step=step_id,
        model_class=usage_log.declared_model_class(model_token),
        duration_s=duration_s,
        ticket=ticket,
    )
