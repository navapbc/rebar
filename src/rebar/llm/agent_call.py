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
from rebar.llm.runaway_guard import ToolCallLedger, runaway_guard_toolsets

if TYPE_CHECKING:
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import RunRequest

logger = logging.getLogger(__name__)

MEMO_ALLOWLIST: frozenset[str] = frozenset(
    {"read_file", "search_files", "list_directory", "show_ticket"}
)

_MEMO_NUDGE_L1 = (
    "\n\n[Note: this result was already retrieved earlier in this run. "
    "Consider whether you have gathered sufficient evidence to proceed.]"
)
_MEMO_NUDGE_L2 = (
    "\n\n[Warning: this exact query has been repeated multiple times. "
    "You already have this result. Stop repeating it and produce your final answer now.]"
)


def _read_window_cap() -> int:
    """read_file's per-read line-window cap (``pai_tools._READ_MAX_LINES``), imported so the
    path-normalized read_file memo reproduces read_file's range slice EXACTLY and can tell a
    COMPLETE whole-file read (row count below the cap → the file ended) from a truncated one."""
    from rebar.llm.pai_tools import _READ_MAX_LINES

    return _READ_MAX_LINES


def _read_file_args(tool_args: Any) -> tuple[str | None, int, int]:
    """Extract ``(normalized_path, line_start, line_end)`` from a read_file call's args, applying
    read_file's own defaults (``line_start=1``, ``line_end=0`` = EOF). Path is normalized so
    ``src/x.py`` and ``./src/x.py`` share one cache entry. Returns ``(None, …)`` when the path is
    missing/unusable so the caller passes the call straight through."""
    import os

    path = tool_args.get("path") if isinstance(tool_args, dict) else None
    if not isinstance(path, str) or not path:
        return None, 1, 0
    ls = tool_args.get("line_start", 1)
    le = tool_args.get("line_end", 0)
    ls = 1 if ls is None else int(ls)
    le = 0 if le is None else int(le)
    return os.path.normpath(path), ls, le


def _slice_cached_read(
    lines: list[str], cached_max: int, line_start: int, line_end: int, cap: int
) -> str:
    """Reproduce read_file's range slice over a cached whole-file read. ``lines`` are the cached
    formatted ``"{n}\\ttext"`` rows for line numbers ``1..cached_max`` (contiguous). Mirrors
    read_file exactly: ``lo = max(1, line_start)``; ``hi = line_end`` when set and ``>= lo`` else
    EOF (``cached_max``); ``hi`` capped at ``lo + cap - 1``; the ``"(empty range)"`` sentinel for
    an empty selection."""
    lo = max(1, line_start)
    hi = line_end if (line_end and line_end >= lo) else cached_max
    hi = min(hi, lo + cap - 1)
    selected = lines[lo - 1 : hi] if lo <= cached_max else []
    return "\n".join(selected) or "(empty range)"


TOOL_STEP_STEERING_NOTICE = (
    "Tool results are no longer being provided: the evidence-gathering budget for this "
    "run is exhausted. Do not call any more tools. Produce your final answer now, using "
    "only the evidence already gathered above."
)


def _steering_toolsets(tools: list, toolsets: list, limit: int) -> list:
    """Wrap each toolset (function tools moved in first) with a steering boundary that executes
    calls at run_step <= limit via the wrapped toolset, and returns the steering notice past it
    — keeping tool definitions advertised for the whole run (the provider-protocol requirement:
    an empty tool surface over a toolUse history is a Bedrock/Anthropic 400 rejection).
    """
    from dataclasses import dataclass

    from pydantic_ai.toolsets import FunctionToolset, WrapperToolset

    @dataclass
    class _SteeringToolset(WrapperToolset):
        limit: int = 0

        async def call_tool(self, name, tool_args, ctx, tool):
            if ctx.run_step > self.limit:
                return TOOL_STEP_STEERING_NOTICE
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)

    all_toolsets = [FunctionToolset(tools), *toolsets]
    return [_SteeringToolset(wrapped=ts, limit=limit) for ts in all_toolsets]


def _memo_toolsets(tools: list, toolsets: list, *, ledger: ToolCallLedger) -> list:
    """Wrap every toolset with a memoizing layer that caches results for the four
    allowlisted read-only tools and appends a graduated nudge on duplicate calls.
    Non-allowlisted tools always pass through with no caching.

    Every path that actually reaches a wrapped tool reports the ORIGINAL call's
    signature to ``ledger.record_executed`` — the runaway guard's loop predicate runs
    over executed work only, so a repeat the cache absorbs is never loop evidence
    (see :class:`~rebar.llm.runaway_guard.ToolCallLedger`).

    The cache, per-signature repeat counts, and per-signature locks live in this
    ENCLOSING scope, not on the toolset instance — pydantic-ai copies a toolset per run
    step while PREPARING the request, so instance attributes set inside ``call_tool`` do
    not survive to the next turn (the exact reason the runaway guard keeps its
    ``signatures`` window here too). A single shared cache across the wrapped toolsets is
    correct: a tool-call SIGNATURE is unique to its tool, so there is no cross-toolset
    collision.

    A per-signature :class:`asyncio.Lock` serialises the check-execute-store critical
    section so that a PARALLEL batch of N identical calls in ONE turn (pydantic-ai runs a
    turn's tool calls concurrently) still executes the wrapped tool exactly once — the
    first caller executes and populates the cache while the rest await, then read the
    cached result. Distinct signatures take distinct locks, so genuine breadth still runs
    concurrently.
    """
    import asyncio
    from dataclasses import dataclass

    from pydantic_ai.toolsets import FunctionToolset, WrapperToolset

    cache: dict[str, Any] = {}
    counts: dict[str, int] = {}
    locks: dict[str, asyncio.Lock] = {}
    # read_file is memoized by NORMALIZED PATH, not by exact signature (epic 10ae/story 2948,
    # lever 2). The f6fc exact-signature memo keyed on line_start/line_end, so re-reads of the
    # same file at DIFFERENT ranges slipped past it — the measured dominant waste (48 read_file
    # calls hitting only 11 distinct files, 77% re-reads). The path cache holds the file's WHOLE
    # content once (a single line_start=1/line_end=0 read through the wrapped tool) and serves
    # every subsequent range by slicing it. The other three allowlisted tools keep exact-sig memo.
    # Each value: {"lines": list[str], "max": int, "complete": bool}.
    read_cache: dict[str, dict] = {}  # keyed by normalized path
    read_counts: dict[str, int] = {}

    def _lock_for(sig: str) -> asyncio.Lock:
        # Safe without a meta-lock: dict get/create runs to completion between awaits on
        # the single event loop, so two coroutines cannot race to create two locks.
        lock = locks.get(sig)
        if lock is None:
            lock = asyncio.Lock()
            locks[sig] = lock
        return lock

    @dataclass
    class _MemoNudgeToolset(WrapperToolset):
        async def call_tool(self, name, tool_args, ctx, tool):
            if name not in MEMO_ALLOWLIST:
                ledger.record_executed(usage_log.tool_call_signature(name, tool_args))
                return await self.wrapped.call_tool(name, tool_args, ctx, tool)
            if name == "read_file":
                return await self._read_file_call(name, tool_args, ctx, tool)
            sig = usage_log.tool_call_signature(name, tool_args)
            async with _lock_for(sig):
                if sig in cache:
                    counts[sig] = counts[sig] + 1
                    nudge = _MEMO_NUDGE_L1 if counts[sig] == 1 else _MEMO_NUDGE_L2
                    return cache[sig] + nudge
                ledger.record_executed(sig)
                result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
                # Errors (the read-only tools return an "Error:"-prefixed STRING; they never
                # raise into the agent loop) are transient — never cache them, so the retry
                # re-executes. Deterministic sentinel-empty results ("(no matches)",
                # "(empty)") ARE cached: memoizing them is the core waste this eliminates.
                if isinstance(result, str) and result.startswith("Error:"):
                    return result
                cache[sig] = result
                counts[sig] = 0
                return result

        async def _read_file_call(self, name, tool_args, ctx, tool):
            """Path-normalized read_file memo (lever 2): cache the whole file once, serve any range
            by slicing. First read of a path → whole-file read + slice (no nudge); a later read of
            the same path (ANY range) → slice-from-cache + graduated nudge. An "Error:"-prefixed
            whole-file read is not cached (transient → re-executes). When the whole-file read hit
            the window cap (a >cap-line file) and the request reaches past the cached window, fall
            back to the exact-range read so a truncated cache never fabricates "(empty range)"."""
            path, ls, le = _read_file_args(tool_args)
            if path is None:
                ledger.record_executed(usage_log.tool_call_signature(name, tool_args))
                return await self.wrapped.call_tool(name, tool_args, ctx, tool)
            cap = _read_window_cap()

            def _servable(entry: dict) -> bool:
                # A COMPLETE whole-file cache serves any range; a truncated one only serves a
                # request whose explicit end is within the cached window.
                return entry["complete"] or bool(le and le <= entry["max"])

            async with _lock_for(f"read_file::{path}"):
                entry = read_cache.get(path)
                if entry is None:
                    ledger.record_executed(usage_log.tool_call_signature(name, tool_args))
                    whole = await self.wrapped.call_tool(
                        name,
                        {"path": tool_args.get("path"), "line_start": 1, "line_end": 0},
                        ctx,
                        tool,
                    )
                    if isinstance(whole, str) and whole.startswith("Error:"):
                        return whole  # transient — do not cache; a retry re-executes
                    wlines = [] if whole == "(empty range)" else str(whole).split("\n")
                    cached_max = 0
                    if wlines:
                        head = wlines[-1].split("\t", 1)[0]
                        cached_max = int(head) if head.isdigit() else len(wlines)
                    entry = {"lines": wlines, "max": cached_max, "complete": cached_max < cap}
                    read_cache[path] = entry
                    read_counts[path] = 0
                    if _servable(entry):
                        return _slice_cached_read(entry["lines"], entry["max"], ls, le, cap)
                    # Truncated file, request past the cached window → exact-range read, uncached.
                    return await self.wrapped.call_tool(name, tool_args, ctx, tool)
                # Repeat read of a cached path (any range): serve with the graduated nudge.
                read_counts[path] = read_counts[path] + 1
                nudge = _MEMO_NUDGE_L1 if read_counts[path] == 1 else _MEMO_NUDGE_L2
                if _servable(entry):
                    return _slice_cached_read(entry["lines"], entry["max"], ls, le, cap) + nudge
                ledger.record_executed(usage_log.tool_call_signature(name, tool_args))
                return await self.wrapped.call_tool(name, tool_args, ctx, tool) + nudge

    all_toolsets = [FunctionToolset(tools), *toolsets] if tools else list(toolsets)
    return [_MemoNudgeToolset(wrapped=ts) for ts in all_toolsets]


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
    ``_pai_structured``, plus the ``tool_step_limit`` steering wrapper that keeps tool
    definitions advertised while answering post-limit calls with a fixed notice (ADR 0056
    decision 3; plain parameters, no carrier — the signature does not reach the ADR's
    ~6-parameter threshold for one).

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
    # Completion evidence governance is selected ONLY by private, schema-invisible metadata
    # on a function tool.  At this seam ``runner`` has already appended ``extra_tools`` to the
    # repository function tools, so no request flag, reviewer-name inference, or public carrier
    # is needed.  Keep the import local with the other pydantic-ai toolset machinery.
    completion_policy = None
    if tools:
        from rebar.llm.completion_tool_policy import completion_evidence_policy_from_tools

        completion_policy = completion_evidence_policy_from_tools(tools)
    ledger = ToolCallLedger()  # one per Agent call, shared by memo + runaway guard
    if req.tool_step_limit is not None and tools:
        # Steering convergence boundary — keeps tool DEFINITIONS advertised (a
        # provider-protocol requirement: an empty tool surface over a toolUse history is a
        # Bedrock/Anthropic 400), and answers post-limit CALLS with a fixed notice instead
        # of executing them, so the model produces its final answer from evidence already
        # gathered. Intentionally not a forced tool.
        limit = max(0, int(req.tool_step_limit))
        toolsets = _steering_toolsets(tools, toolsets, limit)
        tools = []
    if tools or toolsets:
        # Memo layer (f6fc): caches read-only tool calls and appends a nudge on repeats.
        # Sits BETWEEN steering (innermost) and the runaway guard (outermost). The shared
        # ledger tells the guard which calls actually executed (bug 3211): the guard's
        # loop predicate must observe executed work, not repeats the memo made free.
        toolsets = _memo_toolsets(tools, toolsets, ledger=ledger)
        tools = []
    if completion_policy is not None and toolsets:
        # Completion-only finite evidence boundary.  It sits OUTSIDE memoization so cached
        # repository responses still count as model evidence responses, and INSIDE runaway so
        # the generic loop guard continues to observe every attempted call.
        from rebar.llm.completion_tool_policy import wrap_completion_evidence_policy

        toolsets = wrap_completion_evidence_policy(toolsets, completion_policy)
    if tools or toolsets:
        # Runaway loop breaker (bug c827), armed for EVERY Agent call with a tool surface
        # — not only step-limited ones — and put OUTERMOST so it also observes steered
        # calls. Wrapping only; tool definitions stay advertised. An empty surface
        # (single_turn) stays byte-identical: no wrapper, no guard.
        toolsets = runaway_guard_toolsets(tools, toolsets, ledger=ledger)
        tools = []

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
