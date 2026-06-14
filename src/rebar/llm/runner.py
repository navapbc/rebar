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
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rebar.llm import findings as _findings
from rebar.llm.config import LLMConfig
from rebar.llm.config import denied_paths as _denied_realpaths
from rebar.llm.config import is_denied as _is_denied
from rebar.llm.errors import LLMConfigError, StructuredOutputError


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
        create_agent, ToolStrategy, init_chat_model = _import_langgraph()
        model = _build_model(cfg, init_chat_model)
        tools = _filesystem_tools(cfg.repo_path) + _mcp_tools(cfg.mcp_servers)
        response_model = _findings.findings_response_model()
        # ToolStrategy (not ProviderStrategy) is deliberate: it is provider-PORTABLE
        # (works across Anthropic/OpenAI/Gemini), with in-loop self-correction
        # (handle_errors). NOTE: ToolStrategy forces tool_choice, which Anthropic
        # rejects when *extended thinking* is enabled (HTTP 400) — so do NOT enable
        # thinking on the model here. A missing structured_response is handled below.
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
                # Best-effort prompt→trace linkage. NOTE: Langfuse's first-class
                # linkage attaches `langfuse_prompt` to a LangChain PromptTemplate's
                # metadata; create_agent builds messages internally (no template),
                # so this run-level metadata may not register the link in every SDK
                # version. Harmless and forward-compatible if it doesn't.
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
        from langchain.chat_models import init_chat_model
    except ImportError as exc:
        raise LLMConfigError(
            "the langgraph runner needs the 'agents' extra. Install it with: "
            "pip install 'nava-rebar[agents]'"
        ) from exc
    return create_agent, ToolStrategy, init_chat_model


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
    inside a running event loop (asyncio.run() would raise there)."""
    if not servers:
        return []
    import asyncio

    from langchain_mcp_adapters.client import MultiServerMCPClient

    async def _load():
        return await MultiServerMCPClient(servers).get_tools()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_load())  # no loop running — the common (sync) case
    # A loop is already running in this thread: run our own loop in a worker.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _load()).result()


def _safe_path(root: str, rel: str, denied: tuple[str, ...]) -> str:
    """Resolve ``rel`` under ``root``, refusing traversal and any denied state path
    (by realpath). Raises ValueError (surfaced to the agent as a tool error)."""
    abs_path = os.path.realpath(os.path.join(root, rel))
    if abs_path != root and not abs_path.startswith(root + os.sep):
        raise ValueError(f"path escapes the repository root: {rel}")
    if _is_denied(abs_path, denied):
        raise ValueError(f"path is not accessible to review: {rel}")
    return abs_path


# Per-call caps so an agent loop can't blow up latency/cost/context on a huge file
# or tree. read_file is windowed (page with line_start/line_end), long lines are
# truncated, and discovery output is capped — the patterns SWE-agent/deepagents/
# Claude Code converge on (windowing is a *correctness* lever, not just cost).
_READ_MAX_LINES = 2000        # max lines returned by one read_file call
_READ_MAX_LINE_CHARS = 2000   # per-line cap (minified/generated lines)
_SCAN_MAX_FILES = 5000        # max files scanned by one search_files call
_SEARCH_MAX_LINE_CHARS = 500  # per-matched-line cap

# Vendored/generated dirs + binary/lock suffixes hidden from DISCOVERY
# (list_directory/search_files) so the agent isn't drowned on large projects.
# read_file is NOT filtered by these — an explicitly named file is always readable
# (only the security deny-list blocks it).
_NOISE_DIRS = frozenset({
    "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", "dist", "build", ".next", "target", ".gradle",
    ".idea", ".vscode", ".cache", "coverage", "htmlcov",
})
_NOISE_SUFFIXES = (
    ".lock", ".min.js", ".min.css", ".map", ".pyc", ".pyo", ".so", ".o", ".a",
    ".class", ".jar", ".bin", ".woff", ".woff2", ".ttf", ".eot", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".whl",
)


def _git_tracked(root: str) -> set[str] | None:
    """Realpaths git considers part of the project (tracked + untracked but NOT
    gitignored), or None if not a git repo / git is unavailable. Lets discovery
    hide .gitignore'd build output — the `git ls-files` approach code-review-graph
    uses — without us reimplementing .gitignore parsing."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = proc.stdout.decode("utf-8", "replace").split("\0")
    return {os.path.realpath(os.path.join(root, n)) for n in names if n}


def _discovery_filter(root: str):
    """(skip_dir, skip_file) predicates hiding vendored/generated/gitignored paths
    from the discovery tools. Computed once per tool-set construction."""
    tracked = _git_tracked(root)

    def skip_dir(name: str) -> bool:
        return name in _NOISE_DIRS

    def skip_file(abs_path: str, name: str) -> bool:
        if name.endswith(_NOISE_SUFFIXES):
            return True
        return tracked is not None and abs_path not in tracked

    return skip_dir, skip_file


def _filesystem_tools(repo_path: str | None) -> list:
    """Read-only, sandboxed file tools rooted at ``repo_path``. Output is
    line-numbered (``<lineno>: <content>``) so the agent can cite ``path:line``
    accurately — the proven citation-reliability technique. Reads are windowed,
    long lines truncated, and discovery hides vendored/generated/gitignored noise.
    No write/edit/bash."""
    from langchain_core.tools import tool

    root = os.path.realpath(repo_path or ".")
    denied = _denied_realpaths(root)
    skip_dir, skip_file = _discovery_filter(root)

    @tool
    def read_file(path: str, line_start: int = 1, line_end: int = 0) -> str:
        """Read a repository file as line-numbered text (`<lineno>: <content>`) so
        you can cite exact `path:line` locations. Each call returns a capped window
        of lines; PAGE through large files with line_start/line_end (1-based;
        line_end<=0 means to the end) rather than guessing — when output is
        truncated the result tells you the next line_start. Overlong lines are
        clipped. Prefer reading the specific region you need."""
        target = _safe_path(root, path, denied)
        lo = max(1, line_start)
        hard_hi = lo + _READ_MAX_LINES - 1  # read at most _READ_MAX_LINES lines
        requested_end = line_end if line_end > 0 else None
        out: list[str] = []
        hit_cap = False
        # Stream the file; never read more than the returned window into memory,
        # so the cap holds even on a huge file (a narrow range stays cheap).
        with open(target, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i < lo:
                    continue
                if i > hard_hi:
                    hit_cap = True  # more lines exist beyond the cap window
                    break
                if requested_end is not None and i > requested_end:
                    break
                text = line.rstrip("\n")
                if len(text) > _READ_MAX_LINE_CHARS:
                    text = text[:_READ_MAX_LINE_CHARS] + (
                        f" …(+{len(text) - _READ_MAX_LINE_CHARS} chars truncated)"
                    )
                out.append(f"{i}: {text}")
        if not out:
            return "(no lines in range; file may be empty or shorter than line_start)"
        body = "\n".join(out)
        if hit_cap:
            nxt = lo + _READ_MAX_LINES
            body += (
                f"\n… (output truncated at {_READ_MAX_LINES} lines; more remain — "
                f"call read_file with line_start={nxt} to continue)"
            )
        return body

    @tool
    def list_directory(path: str = ".") -> str:
        """List entries of a repo directory (directories end with '/'). Vendored/
        generated and git-ignored entries are hidden to cut noise; you can still
        read_file any specific path that isn't shown."""
        target = _safe_path(root, path, denied)
        entries: list[str] = []
        hidden = 0
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            rp = os.path.realpath(full)
            if _is_denied(rp, denied):
                continue
            is_dir = os.path.isdir(full)
            if (is_dir and skip_dir(name)) or (not is_dir and skip_file(rp, name)):
                hidden += 1
                continue
            entries.append(name + ("/" if is_dir else ""))
        body = "\n".join(entries) or "(empty)"
        if hidden:
            body += f"\n… ({hidden} ignored/generated item(s) hidden)"
        return body

    @tool
    def search_files(pattern: str, path: str = ".", max_results: int = 50) -> str:
        """Regex-search repo file CONTENTS under `path`; returns `path:lineno: line`
        matches (capped at max_results). Vendored/generated and git-ignored files
        are skipped. If you hit the cap, narrow the pattern or `path`."""
        import re

        base = _safe_path(root, path, denied)
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"invalid regex: {exc}"
        hits: list[str] = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                d for d in dirnames
                if not skip_dir(d)
                and not _is_denied(os.path.realpath(os.path.join(dirpath, d)), denied)
            ]
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rp = os.path.realpath(full)
                if _is_denied(rp, denied) or skip_file(rp, fn):
                    continue
                if scanned >= _SCAN_MAX_FILES:
                    return "\n".join(hits) + (
                        f"\n… (scan limit of {_SCAN_MAX_FILES} files reached; narrow `path`)"
                    )
                scanned += 1
                rel = os.path.relpath(full, root)
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if rx.search(line):
                                text = line.rstrip()
                                if len(text) > _SEARCH_MAX_LINE_CHARS:
                                    text = text[:_SEARCH_MAX_LINE_CHARS] + " …"
                                hits.append(f"{rel}:{i}: {text}")
                                if len(hits) >= max_results:
                                    return "\n".join(hits) + (
                                        f"\n… ({max_results}-match cap; narrow the pattern)"
                                    )
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
        try:
            with client.start_as_current_span(name="rebar.review") as span:
                yield (getattr(span, "trace_id", None), [handler])
        except Exception:
            # Span API drift across SDK versions shouldn't fail the review — still trace.
            yield (None, [handler])
    finally:
        # The v3 SDK buffers spans on a background thread; a short-lived process
        # (CLI run) can exit before they flush, silently losing the trace. Flush
        # before returning. Best-effort — never fail the review on a tracing hiccup.
        try:
            client.flush()
        except Exception:
            pass
