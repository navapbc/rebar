"""Runners — the pluggable execution backends behind every LLM operation.

A ``Runner`` takes a :class:`RunRequest` (a resolved system prompt + task
instructions + config) and returns a validated ``review_result`` dict. This is the
seam that makes the framework portable: the same operation runs in-process here
(``LangGraphRunner``) or against a hosted Langflow deployment elsewhere
(``LangflowRunner``), and a ``FakeRunner`` lets the whole pipeline be exercised
offline with no model/network.

Heavy libraries (langchain/langgraph/langfuse/anthropic) are imported **inside**
the runner methods, never at module top, so ``import rebar.llm`` stays stdlib-only.
The default substrate is LangChain/LangGraph — the one agent runtime native to both
Langflow and Langfuse — but it is entirely optional (the ``nava-rebar[agents]``
extra); a missing extra raises a clear, actionable error.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rebar.llm import findings as _findings
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError, StructuredOutputError

# Directories the read-only file tools must never expose to the agent (the live
# event store, git internals, reconciler state).
_DENY_DIRS = (".git", ".tickets-tracker", ".bridge_state")


@dataclass
class RunRequest:
    system_prompt: str
    instructions: str
    config: LLMConfig
    reviewers: list[str] = field(default_factory=list)
    target: dict = field(default_factory=dict)
    langfuse_prompt: object | None = None


@runtime_checkable
class Runner(Protocol):
    name: str

    def run(self, req: RunRequest) -> dict:
        """Execute the request and return a validated ``review_result`` dict."""
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

    def run(self, req: RunRequest) -> dict:
        result = _findings.build_result(
            self._findings,
            runner=self.name,
            model=None,
            trace_id=None,
            target=req.target,
            reviewers=req.reviewers,
            summary=self._summary,
            reviewer_id=req.reviewers[0] if len(req.reviewers) == 1 else None,
        )
        _findings.resolve_citations(result, req.config.repo_path)
        return _findings.validate_result(result)


# ── Langflow runner (stub — protocol seam for hosted deployments) ──────────────
class LangflowRunner:
    """Run an operation against a hosted Langflow deployment via its REST API
    (``POST /api/v1/run/{flow_id}``, header ``x-api-key``).

    Stubbed in this milestone (this environment can't run Langflow). The protocol
    seam is defined so a deployment elsewhere can be wired without touching the
    operation layer: the resolved prompt/context is passed as ``input_value`` and
    the flow is a thin transport that returns the same findings shape. Set
    ``LANGFLOW_URL`` (+ ``LANGFLOW_API_KEY``) and implement ``run`` to enable."""

    name = "langflow"

    def __init__(self, config: LLMConfig):
        self._config = config

    def run(self, req: RunRequest) -> dict:
        raise NotImplementedError(
            "the Langflow runner is a stub in this release. Run a Langflow "
            "deployment, set LANGFLOW_URL (+ LANGFLOW_API_KEY), and use the default "
            "langgraph runner (REBAR_LLM_RUNNER=langgraph) in environments without "
            "Langflow. See docs/llm-framework.md."
        )


# ── LangGraph runner (default in-process backend) ─────────────────────────────
class LangGraphRunner:
    name = "langgraph"

    def __init__(self, config: LLMConfig):
        self._config = config

    def run(self, req: RunRequest) -> dict:
        cfg = self._config
        create_agent, ToolStrategy, ChatAnthropic = _import_langgraph()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMConfigError(
                "ANTHROPIC_API_KEY is not set — the langgraph runner needs model "
                "credentials. Export ANTHROPIC_API_KEY (or use a FakeRunner override)."
            )
        model = ChatAnthropic(model=cfg.model, max_tokens=cfg.max_tokens, timeout=cfg.timeout_s)
        tools = _filesystem_tools(cfg.repo_path) + _mcp_tools(cfg.mcp_servers)
        response_model = _findings.findings_response_model()
        agent = create_agent(
            model,
            tools,
            system_prompt=req.system_prompt,
            response_format=ToolStrategy(response_model, handle_errors=True),
        )
        invoke_cfg: dict = {"recursion_limit": cfg.max_iterations}
        with _trace(cfg) as (trace_id, callbacks):
            if callbacks:
                invoke_cfg["callbacks"] = callbacks
            if req.langfuse_prompt is not None:
                invoke_cfg["metadata"] = {"langfuse_prompt": req.langfuse_prompt}
            outcome = agent.invoke(
                {"messages": [{"role": "user", "content": req.instructions}]},
                config=invoke_cfg,
            )
        structured = outcome.get("structured_response")
        if structured is None:
            # #36349: a plain-text turn yields no structured payload. An empty
            # review is indistinguishable from a clean one, so fail loudly rather
            # than silently returning zero findings.
            raise StructuredOutputError(
                "the agent returned no structured findings (no structured_response). "
                "Treating this as a failed review rather than a clean one."
            )
        data = structured.model_dump() if hasattr(structured, "model_dump") else dict(structured)
        result = _findings.build_result(
            data.get("findings", []),
            runner=self.name,
            model=cfg.model,
            trace_id=trace_id,
            target=req.target,
            reviewers=req.reviewers,
            summary=data.get("summary"),
            reviewer_id=req.reviewers[0] if len(req.reviewers) == 1 else None,
        )
        _findings.resolve_citations(result, cfg.repo_path)
        return _findings.validate_result(result)


def get_runner(config: LLMConfig, *, override: Runner | None = None) -> Runner:
    """Select the runner for ``config`` (or use an explicit ``override``, the test
    injection seam). ``langgraph`` (default) requires the ``agents`` extra."""
    if override is not None:
        return override
    if config.runner == "fake":
        return FakeRunner()
    if config.runner == "langflow":
        return LangflowRunner(config)
    return LangGraphRunner(config)


# ── lazy imports + helpers ────────────────────────────────────────────────────
def _import_langgraph():
    try:
        from langchain.agents import create_agent
        from langchain.agents.structured_output import ToolStrategy
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:
        raise LLMConfigError(
            "the langgraph runner needs the 'agents' extra. Install it with: "
            "pip install 'nava-rebar[agents]'"
        ) from exc
    return create_agent, ToolStrategy, ChatAnthropic


def _mcp_tools(servers: dict) -> list:
    if not servers:
        return []
    import asyncio

    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(servers)
    return asyncio.run(client.get_tools())


def _safe_path(root: str, rel: str) -> str:
    """Resolve ``rel`` under ``root``, refusing traversal and the deny-listed
    state directories. Raises ValueError (surfaced to the agent as a tool error)."""
    abs_path = os.path.realpath(os.path.join(root, rel))
    if abs_path != root and not abs_path.startswith(root + os.sep):
        raise ValueError(f"path escapes the repository root: {rel}")
    rest = abs_path[len(root):].lstrip(os.sep)
    parts = rest.split(os.sep) if rest else []
    if parts and parts[0] in _DENY_DIRS:
        raise ValueError(f"path is not accessible to review: {rel}")
    return abs_path


def _filesystem_tools(repo_path: str | None) -> list:
    """Read-only, sandboxed file tools rooted at ``repo_path``. Output is
    line-numbered (``<lineno>: <content>``) so the agent can cite ``path:line``
    accurately — the proven citation-reliability technique. No write/edit/bash."""
    from langchain_core.tools import tool

    root = os.path.realpath(repo_path or ".")

    @tool
    def read_file(path: str, line_start: int = 1, line_end: int = 0) -> str:
        """Read a repo file, returning lines as `<lineno>: <content>`. Optionally
        restrict to the [line_start, line_end] range (1-based; line_end<=0 = end)."""
        target = _safe_path(root, path)
        with open(target, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        lo = max(1, line_start)
        hi = len(lines) if line_end <= 0 else min(line_end, len(lines))
        return "".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1)) or "(empty)"

    @tool
    def list_directory(path: str = ".") -> str:
        """List entries of a repo directory (directories suffixed with '/')."""
        target = _safe_path(root, path)
        entries = []
        for name in sorted(os.listdir(target)):
            if name in _DENY_DIRS:
                continue
            full = os.path.join(target, name)
            entries.append(name + ("/" if os.path.isdir(full) else ""))
        return "\n".join(entries) or "(empty)"

    @tool
    def search_files(pattern: str, path: str = ".", max_results: int = 50) -> str:
        """Regex-search repo files under `path`; returns `path:lineno: line`
        matches (capped at max_results)."""
        import re

        base = _safe_path(root, path)
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"invalid regex: {exc}"
        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _DENY_DIRS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append(f"{rel}:{i}: {line.rstrip()}")
                                if len(hits) >= max_results:
                                    return "\n".join(hits)
                except OSError:
                    continue
        return "\n".join(hits) or "(no matches)"

    return [read_file, list_directory, search_files]


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
    try:
        with client.start_as_current_span(name="rebar.review") as span:
            yield (getattr(span, "trace_id", None), [handler])
    except Exception:
        # Span API drift across SDK versions shouldn't fail the review — still trace.
        yield (None, [handler])
