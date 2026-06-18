"""Runners — the pluggable execution backends behind every LLM operation.

A ``Runner`` takes a :class:`RunRequest` (a resolved system prompt + task
instructions + config) and returns a validated ``review_result`` dict. This is the
seam that makes the framework portable: the default operation runs an in-process
LangGraph agent (``LangGraphRunner``); an opt-in deepagents harness
(``DeepAgentsRunner``) slots in behind the same protocol; and a ``FakeRunner``
lets the whole pipeline be exercised offline with no model/network.

Heavy libraries (langchain/langgraph/langfuse/anthropic) are imported **inside**
the runner methods, never at module top, so ``import rebar.llm`` stays stdlib-only.
The default substrate is LangChain/LangGraph — the agent runtime natively traced by
Langfuse — but it is entirely optional (the ``nava-rebar[agents]`` extra); a missing
extra raises a clear, actionable error.
"""

from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from rebar.llm import findings as _findings
from rebar.llm.config import LLMConfig
from rebar.llm.config import denied_paths as _denied_realpaths
from rebar.llm.errors import LLMConfigError, LLMRunnerError
from rebar.llm.fs_tools import _filesystem_tools


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

    def __init__(self, findings: list[dict] | None = None, summary: str | None = None):
        self._findings = findings or []
        self._summary = summary

    def preflight(self) -> None:
        """Always ready — no extra, no network."""

    def run(self, req: RunRequest) -> dict:
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


# ── LangGraph runner (default in-process backend) ─────────────────────────────
class LangGraphRunner:
    name = "langgraph"

    def __init__(self, config: LLMConfig):
        self._config = config

    def preflight(self) -> None:
        """Fail fast if the ``agents`` extra is absent (import-only, no call)."""
        _import_langgraph()

    def run(self, req: RunRequest) -> dict:
        cfg = self._config
        create_agent, ToolStrategy, init_chat_model = _import_langgraph()
        model = _build_model(cfg, init_chat_model)
        tools = _filesystem_tools(cfg.repo_path) + _mcp_tools(cfg.mcp_servers)
        # ToolStrategy (not ProviderStrategy) is deliberate: it is provider-PORTABLE
        # (works across Anthropic/OpenAI/Gemini), with in-loop self-correction
        # (handle_errors). NOTE: ToolStrategy forces tool_choice, which Anthropic
        # rejects when *extended thinking* is enabled (HTTP 400) — so do NOT enable
        # thinking on the model here. A missing structured_response is handled below.
        agent = create_agent(
            model,
            tools,
            system_prompt=req.system_prompt,
            response_format=ToolStrategy(_findings.findings_response_model(), handle_errors=True),
        )
        outcome, trace_id = _invoke_structured(agent, cfg, req)
        return _finalize_review(outcome, cfg, req, self.name, trace_id)


# ── DeepAgents runner (opt-in harness) ────────────────────────────────────────
class DeepAgentsRunner:
    """Run the operation on LangChain's deepagents harness (planning, subagents,
    large-result eviction) instead of our bare create_agent loop.

    OPT-IN (``REBAR_LLM_EXPERIMENTAL_HARNESS=deepagents``): the review default stays the
    ``langgraph`` runner with our own read-only, citation-disciplined file tools —
    this runner is the seam for future deepagents-based task types. It uses
    deepagents' native filesystem over a repo-rooted ``FilesystemBackend``, made
    **read-only** via a write-denying ``FilesystemPermission`` (confined to the
    repo root), plus **read-deny** rules over our state-dir deny-list
    (`.git`/`.tickets-tracker`/`.bridge_state`, incl. the REBAR_TRACKER_DIR/TICKETS_TRACKER_DIR
    override) so internal state can't be read here either — same guarantee the
    default langgraph runner enforces. Output is still constrained to our findings
    schema, so it returns a review_result."""

    name = "deepagents"

    def __init__(self, config: LLMConfig):
        self._config = config

    def preflight(self) -> None:
        """Fail fast if the langgraph base or deepagents harness is absent."""
        _import_langgraph()
        _import_deepagents()

    def run(self, req: RunRequest) -> dict:
        cfg = self._config
        _, ToolStrategy, init_chat_model = _import_langgraph()
        create_deep_agent, FilesystemBackend, FilesystemPermission = _import_deepagents()
        model = _build_model(cfg, init_chat_model)
        root = os.path.realpath(cfg.repo_path or ".")
        backend = FilesystemBackend(root_dir=root, virtual_mode=True)
        # Read-only overall, plus read-deny over the state-dir deny-list (as
        # backend-root-relative globs) so deepagents' own ls/read/grep can't reach
        # internal state — parity with the langgraph runner's _safe_path deny.
        permissions = [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]
        deny_globs = [
            f"/{d[len(root) + 1 :].replace(os.sep, '/')}/**"
            for d in _denied_realpaths(root)
            if d.startswith(root + os.sep)
        ]
        if deny_globs:
            permissions.append(
                FilesystemPermission(operations=["read"], paths=deny_globs, mode="deny")
            )
        agent = create_deep_agent(
            model=model,
            tools=_mcp_tools(cfg.mcp_servers),  # deepagents supplies its own ls/read/grep/glob
            system_prompt=req.system_prompt,
            backend=backend,
            permissions=permissions,
            response_format=ToolStrategy(_findings.findings_response_model(), handle_errors=True),
        )
        outcome, trace_id = _invoke_structured(agent, cfg, req)
        return _finalize_review(outcome, cfg, req, self.name, trace_id)


def get_runner(config: LLMConfig, *, override: Runner | None = None) -> Runner:
    """Select the runner for ``config`` (or use an explicit ``override``, the test
    injection seam). ``langgraph`` (default) requires the ``agents`` extra."""
    if override is not None:
        return override
    if config.runner == "fake":
        return FakeRunner()
    if config.runner == "deepagents":
        return DeepAgentsRunner(config)
    if config.runner == "langgraph":
        return LangGraphRunner(config)
    # from_env only ever derives a valid runner; a bad value can only come from an
    # explicit library LLMConfig(runner=...). Fail loudly rather than silently
    # running the default, naming the valid set (RUNNERS).
    from rebar.llm.config import RUNNERS

    raise LLMConfigError(f"unknown runner {config.runner!r}; valid runners: {RUNNERS}")


def _invoke(agent, cfg: LLMConfig, req: RunRequest) -> tuple[dict, str | None]:
    """Invoke a compiled agent under the (optional) Langfuse trace, returning
    ``(outcome, trace_id)``. Shared by the langgraph + deepagents runners."""
    # LangGraph counts super-steps (~2 per tool-call cycle: model node + tool node),
    # so this is roughly half the tool calls the agent can make before it trips.
    invoke_cfg: dict = {"recursion_limit": cfg.max_iterations}
    with _trace(cfg) as (trace_id, callbacks):
        if callbacks:
            invoke_cfg["callbacks"] = callbacks
        if req.langfuse_prompt is not None:
            # Best-effort prompt→trace linkage (see note: create_agent builds
            # messages internally, so this run-level metadata may not register).
            invoke_cfg["metadata"] = {"langfuse_prompt": req.langfuse_prompt}
        try:
            outcome = agent.invoke(
                {"messages": [{"role": "user", "content": req.instructions}]},
                config=invoke_cfg,
            )
        except Exception as exc:
            # Surface the opaque GraphRecursionError as a clean, actionable runner
            # error (matched by name to avoid importing langgraph.errors).
            if type(exc).__name__ == "GraphRecursionError":
                raise LLMRunnerError(
                    f"agent exceeded its step budget (recursion_limit="
                    f"{cfg.max_iterations}; ~2 steps per tool call). Raise "
                    "REBAR_LLM_MAX_STEPS or narrow the task."
                ) from exc
            raise
    return outcome, trace_id


# Repair nudge appended on the one retry when a structured-output mode produced
# none (the "parsed-is-None" branch, WS-D4).
_REPAIR_NUDGE = (
    "\n\nIMPORTANT: your previous turn returned NO structured result. You MUST emit "
    "the structured output now via the result tool — do not reply in plain prose."
)


def _invoke_structured(agent, cfg: LLMConfig, req: RunRequest) -> tuple[dict, str | None]:
    """Invoke with WS-D4 structured-output hardening on top of create_agent v1's
    ``ToolStrategy(handle_errors=True)`` (which self-corrects malformed tool-calls /
    non-empty ``invalid_tool_calls`` in-loop): if a structured-output mode
    (``findings``/``structured``) yields no ``structured_response`` on the first
    turn (parsed-is-None), retry ONCE with an explicit repair nudge. ``text`` mode
    needs no structured output, so it never retries. Returns ``(outcome, trace_id)``.

    Full validation of the in-loop repair behavior needs live model calls and is
    exercised by the WS-G evals; this layer adds the deterministic outer retry.
    """
    outcome, trace_id = _invoke(agent, cfg, req)
    if req.mode == "text" or outcome.get("structured_response") is not None:
        return outcome, trace_id
    repaired = replace(req, instructions=req.instructions + _REPAIR_NUDGE)
    outcome2, trace_id2 = _invoke(agent, cfg, repaired)
    return outcome2, (trace_id2 or trace_id)


def _finalize_review(
    outcome: dict, cfg: LLMConfig, req: RunRequest, runner_name: str, trace_id: str | None
) -> dict:
    """Finalize an agent outcome via the generalized strategy (WS-D1).

    Dispatches on ``req.mode`` (default ``findings`` → the review_result pipeline,
    so the review ops are unchanged); ``structured``/``text`` serve agentic workflow
    steps. Shared by the langgraph + deepagents runners."""
    return _findings.finalize_outcome(
        outcome,
        mode=req.mode,
        output_schema=req.output_schema,
        runner=runner_name,
        model=cfg.model,
        trace_id=trace_id,
        target=req.target,
        reviewers=req.reviewers,
        repo_path=cfg.repo_path,
        reviewer_id=req.reviewers[0] if len(req.reviewers) == 1 else None,
    )


# ── lazy imports + helpers ────────────────────────────────────────────────────
def _import_langgraph():
    try:
        from langchain.agents import create_agent
        from langchain.agents.structured_output import ToolStrategy
        from langchain.chat_models import init_chat_model
    except ImportError as exc:
        raise LLMConfigError(
            "the langgraph runner needs the 'agents' extra. Install it with: "
            "pip install 'nava-rebar[agents]'"
        ) from exc
    return create_agent, ToolStrategy, init_chat_model


def _import_deepagents():
    try:
        from deepagents import FilesystemPermission, create_deep_agent
        from deepagents.backends.filesystem import FilesystemBackend
    except ImportError as exc:
        raise LLMConfigError(
            "the deepagents runner needs the 'agents' extra (deepagents). "
            "Install it with: pip install 'nava-rebar[agents]'"
        ) from exc
    return create_deep_agent, FilesystemBackend, FilesystemPermission


def _build_model(cfg: LLMConfig, init_chat_model):
    """Construct the chat model for any provider via init_chat_model (provider is
    inferred from the model name unless cfg.model_provider is set). We never pass
    `temperature`: claude-opus-4.x reject it (HTTP 400), and other providers use
    their own default. base_url/api_key enable OpenAI-compatible local servers."""
    kwargs: dict = {"max_tokens": cfg.max_tokens, "timeout": cfg.timeout_s}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    try:
        return init_chat_model(cfg.model, model_provider=cfg.model_provider, **kwargs)
    except ImportError as exc:
        from rebar.llm.config import PROVIDER_PACKAGES, infer_provider

        provider = infer_provider(cfg.model, cfg.model_provider)
        pkg = PROVIDER_PACKAGES.get(provider or "", "the provider's langchain integration")
        raise LLMConfigError(
            f"model '{cfg.model}' needs the {provider or 'provider'} integration "
            f"package: pip install {pkg}"
        ) from exc


def _mcp_tools(servers: dict) -> list:
    """Load MCP tools (langchain-mcp-adapters). get_tools() is async; run it on a
    private loop in a worker thread so this works even when the caller is already
    inside a running event loop (asyncio.run() would raise there).

    A fresh ``MultiServerMCPClient`` is created per call — MCP sessions are
    stateless and re-spawned each review, so a stale session can't leak across
    runs. A configured server that fails to start/connect (a *downed* server), or
    that connects but advertises zero tools, must surface a CLEAR LLMRunnerError —
    NEVER be silently swallowed into a tool-less run (which would degrade the
    review with no signal that the operator's MCP tools went missing)."""
    if not servers:
        return []
    import asyncio

    from langchain_mcp_adapters.client import MultiServerMCPClient

    async def _load():
        return await MultiServerMCPClient(servers).get_tools()

    def _run():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_load())  # no loop running — the common (sync) case
        # A loop is already running in this thread: run our own loop in a worker.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _load()).result()

    try:
        tools = _run()
    except Exception as exc:
        raise LLMRunnerError(
            f"MCP server(s) {sorted(servers)} failed to load tools: {exc}. "
            "Check REBAR_LLM_MCP_SERVERS and that each server is reachable."
        ) from exc
    if not tools:
        raise LLMRunnerError(
            f"MCP server(s) {sorted(servers)} connected but advertised zero tools — "
            "refusing to run tool-less silently. Check the server configuration."
        )
    return tools


def _readonly_gate() -> bool:
    """True if the READONLY gate is set (``REBAR_MCP_READONLY`` truthy) — reused to
    withhold the comment tool, so a read-only deployment grants the agent read-only
    ticket access. Case-insensitive truthy (1/true/yes/on)."""
    val = os.environ.get("REBAR_MCP_READONLY", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _scoped_ticket_tools(repo_path: str | None, *, allow_comment: bool | None = None) -> list:
    """A LEAST-PRIVILEGE rebar ticket toolset for agent steps (WS-D3).

    Narrows the full library surface to exactly READ + COMMENT: ``show_ticket``
    (always) and ``comment_ticket`` (only when allowed). It exposes NOTHING ELSE —
    no create/edit/transition/claim/reopen/link/sign — so an agentic step can read a
    ticket and leave a comment but can never mutate work state or forge a signature.
    The comment tool is withheld under the READONLY gate (reusing it, per WS-D3), so
    a read-only deployment yields read-only ticket access."""
    from langchain_core.tools import tool

    if allow_comment is None:
        allow_comment = not _readonly_gate()

    @tool
    def show_ticket(ticket_id: str) -> str:
        """Read a ticket's compiled state (id, title, status, description, comments,
        deps, …) as JSON. Read-only."""
        import json

        import rebar

        try:
            return json.dumps(rebar.show_ticket(ticket_id, repo_root=repo_path))
        except Exception as exc:  # surface as a recoverable tool error, never a crash
            return f"Error: {exc}"

    tools = [show_ticket]

    if allow_comment:

        @tool
        def comment_ticket(ticket_id: str, body: str) -> str:
            """Append a comment to a ticket — the ONLY write this agent may perform.
            Cannot transition, edit, claim, or sign."""
            import rebar

            try:
                rebar.comment(ticket_id, body, repo_root=repo_path)
                return f"Commented on {ticket_id}."
            except Exception as exc:
                return f"Error: {exc}"

        tools.append(comment_ticket)

    return tools


@contextmanager
def _trace(cfg: LLMConfig):
    """Yield ``(trace_id, callbacks)`` for a run. No-op (``(None, [])``) unless
    Langfuse is configured (both keys present) AND installed — gating on
    key-presence BEFORE constructing the handler is the reliable no-op pattern."""
    if not cfg.langfuse.enabled:
        yield (None, [])
        return
    try:
        from langfuse import get_client
        from langfuse.langchain import CallbackHandler

        client = get_client()
        handler = CallbackHandler()
    except Exception:
        yield (None, [])
        return
    # Open the rebar.review root span via an ExitStack so it closes correctly
    # whether the body returns OR raises — and CRUCIALLY so this generator yields
    # exactly ONCE. (A naive ``with span: yield`` wrapped in ``try/except: yield``
    # double-yields when the body raises — contextmanager throws the exception
    # back in at the yield, the except catches it and yields again, and
    # @contextmanager dies with "generator didn't stop after throw()", masking the
    # real error. Opening the span outside the yield's try avoids that entirely.)
    trace_id = None
    with ExitStack() as stack:
        try:
            span = stack.enter_context(_langfuse_root_span(client))
            # Prefer the span's own id; fall back to the client's current-trace
            # lookup. Either is the OTEL 32-hex trace id that the public
            # /api/public/traces/{id} endpoint keys on (no transformation). A
            # span-API failure must not lose tracing — the handler still traces.
            trace_id = getattr(span, "trace_id", None)
            if not trace_id and hasattr(client, "get_current_trace_id"):
                trace_id = client.get_current_trace_id()
        except Exception:
            trace_id = None
        try:
            yield (trace_id, [handler])
        finally:
            # The SDK buffers spans on a background thread; a short-lived process
            # (CLI run) can exit before they flush, silently losing the trace.
            # Flush before returning. Best-effort — never fail the review on a
            # tracing hiccup. (The span itself is closed by the ExitStack, which
            # records any in-flight exception on it.)
            try:
                client.flush()
            except Exception:
                pass


def _langfuse_root_span(client):
    """Open the ``rebar.review`` root span, compatibly across Langfuse SDK majors.

    v4 renamed ``start_as_current_span(name=…)`` to
    ``start_as_current_observation(name=…, as_type="span")``; v3 used the former.
    Returns the context manager (yields a span object carrying ``trace_id``)."""
    if hasattr(client, "start_as_current_observation"):
        return client.start_as_current_observation(name="rebar.review", as_type="span")
    return client.start_as_current_span(name="rebar.review")
