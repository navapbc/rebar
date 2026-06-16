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
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rebar.llm import findings as _findings
from rebar.llm.config import LLMConfig
from rebar.llm.config import denied_paths as _denied_realpaths
from rebar.llm.config import is_denied as _is_denied
from rebar.llm.errors import LLMConfigError, LLMRunnerError, StructuredOutputError


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


# ── Langflow runner (hosted-deployment backend over REST) ─────────────────────
class LangflowRunner:
    """Run an operation against a hosted Langflow deployment via its REST API
    (``POST {LANGFLOW_URL}/api/v1/run/{flow_id}``, header ``x-api-key``).

    The resolved reviewer prompt + task is sent as ``input_value`` (chat in/out);
    the flow is a thin transport whose final message must be **findings JSON**
    (``{"findings": [...], "summary": ...}`` or a bare findings list — the
    ReviewFindings shape). We extract that message from Langflow's deeply-nested
    response, parse it, and run it through the same normalize/validate/citation
    pipeline as every other runner. Configure ``LANGFLOW_URL``,
    ``LANGFLOW_FLOW_ID`` (+ optional ``LANGFLOW_API_KEY``). Uses stdlib urllib —
    no extra dependency."""

    name = "langflow"

    def __init__(self, config: LLMConfig):
        self._config = config

    def preflight(self) -> None:
        cfg = self._config
        if not cfg.langflow_url or not cfg.langflow_flow_id:
            raise LLMConfigError(
                "the langflow runner needs LANGFLOW_URL and LANGFLOW_FLOW_ID set "
                "(+ optional LANGFLOW_API_KEY). See docs/llm-framework.md."
            )

    def run(self, req: RunRequest) -> dict:
        cfg = self._config
        self.preflight()
        payload = {
            "input_value": f"{req.system_prompt}\n\n{req.instructions}",
            "input_type": "chat",
            "output_type": "chat",
        }
        raw = _langflow_post(cfg, payload)
        text = _langflow_extract_text(raw)
        findings, summary = _parse_findings_json(text)
        result = _findings.build_result(
            findings,
            runner=self.name,
            model=cfg.model,
            trace_id=None,
            target=req.target,
            reviewers=req.reviewers,
            summary=summary,
            reviewer_id=req.reviewers[0] if len(req.reviewers) == 1 else None,
        )
        _findings.resolve_citations(result, cfg.repo_path)
        return _findings.validate_result(result)


def _langflow_post(cfg: LLMConfig, payload: dict) -> dict:
    """POST to the Langflow run endpoint, returning the parsed JSON response."""
    import json
    import urllib.error
    import urllib.request

    url = f"{cfg.langflow_url.rstrip('/')}/api/v1/run/{cfg.langflow_flow_id}"
    headers = {"Content-Type": "application/json"}
    if cfg.langflow_api_key:
        headers["x-api-key"] = cfg.langflow_api_key
    request = urllib.request.Request(  # noqa: S310 (operator-configured URL)
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as exc:
        raise LLMRunnerError(f"Langflow request to {url} failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LLMRunnerError(f"Langflow returned non-JSON: {exc}") from exc


def _langflow_extract_text(raw: dict) -> str:
    """Pull the flow's final message text out of Langflow's deeply-nested run
    response. The shape varies by output component, so try the documented path
    then fall back to a recursive search for a message/text string."""
    try:
        outs = raw["outputs"][0]["outputs"][0]
        results = outs.get("results") or {}
        msg = results.get("message")
        if isinstance(msg, dict):
            txt = msg.get("text")
            if isinstance(txt, str) and txt.strip():
                return txt
            inner = msg.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner
    except (KeyError, IndexError, TypeError):
        pass
    # Fallback: search only the `outputs` subtree (never the top-level inputs/
    # session echo) and prefer the LAST message-like string — the flow's final
    # output component, not an echoed input or intermediate message earlier in the
    # tree, which a first-match walk would wrongly grab.
    subtree = raw.get("outputs", raw) if isinstance(raw, dict) else raw
    texts = _deep_find_texts(subtree)
    if texts:
        return texts[-1]
    raise StructuredOutputError("could not extract a message from the Langflow response")


def _deep_find_texts(obj, _depth: int = 0) -> list[str]:
    """Every non-empty string under a 'text'/'message' key, in depth-first order."""
    out: list[str] = []
    if _depth > 8:
        return out
    if isinstance(obj, dict):
        for key in ("text", "message"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                out.append(val)
        for val in obj.values():
            out.extend(_deep_find_texts(val, _depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_deep_find_texts(item, _depth + 1))
    return out


def _deep_find_text(obj) -> str | None:
    """First non-empty 'text'/'message' string (kept for callers/tests)."""
    texts = _deep_find_texts(obj)
    return texts[0] if texts else None


def _parse_findings_json(text: str) -> tuple[list, str | None]:
    """Parse the flow's findings JSON (tolerating ```json fences). Returns
    (findings, summary)."""
    import json

    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t[:4].lower() == "json":
            t = t[4:].strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"Langflow output was not valid findings JSON: {exc}") from exc
    if isinstance(obj, list):
        return obj, None
    if isinstance(obj, dict):
        return obj.get("findings") or [], obj.get("summary")
    raise StructuredOutputError("Langflow findings JSON had an unexpected shape")


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
        outcome, trace_id = _invoke(agent, cfg, req)
        return _finalize_review(outcome, cfg, req, self.name, trace_id)


# ── DeepAgents runner (opt-in harness) ────────────────────────────────────────
class DeepAgentsRunner:
    """Run the operation on LangChain's deepagents harness (planning, subagents,
    large-result eviction) instead of our bare create_agent loop.

    OPT-IN (``REBAR_LLM_RUNNER=deepagents``): the review default stays the
    ``langgraph`` runner with our own read-only, citation-disciplined file tools —
    this runner is the seam for future deepagents-based task types. It uses
    deepagents' native filesystem over a repo-rooted ``FilesystemBackend``, made
    **read-only** via a write-denying ``FilesystemPermission`` (confined to the
    repo root), plus **read-deny** rules over our state-dir deny-list
    (`.git`/`.tickets-tracker`/`.bridge_state`, incl. the TICKETS_TRACKER_DIR
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
        outcome, trace_id = _invoke(agent, cfg, req)
        return _finalize_review(outcome, cfg, req, self.name, trace_id)


def get_runner(config: LLMConfig, *, override: Runner | None = None) -> Runner:
    """Select the runner for ``config`` (or use an explicit ``override``, the test
    injection seam). ``langgraph`` (default) requires the ``agents`` extra."""
    if override is not None:
        return override
    if config.runner == "fake":
        return FakeRunner()
    if config.runner == "langflow":
        return LangflowRunner(config)
    if config.runner == "deepagents":
        return DeepAgentsRunner(config)
    return LangGraphRunner(config)


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
                    "REBAR_LLM_MAX_ITERS or narrow the task."
                ) from exc
            raise
    return outcome, trace_id


def _finalize_review(
    outcome: dict, cfg: LLMConfig, req: RunRequest, runner_name: str, trace_id: str | None
) -> dict:
    """Turn an agent outcome into a validated review_result. Shared by the
    langgraph + deepagents runners."""
    structured = outcome.get("structured_response")
    if structured is None:
        # A plain-text turn yields no structured payload. An empty review is
        # indistinguishable from a clean one, so fail loudly rather than silently
        # returning zero findings.
        raise StructuredOutputError(
            "the agent returned no structured findings (no structured_response). "
            "Treating this as a failed review rather than a clean one."
        )
    data = structured.model_dump() if hasattr(structured, "model_dump") else dict(structured)
    result = _findings.build_result(
        data.get("findings", []),
        runner=runner_name,
        model=cfg.model,
        trace_id=trace_id,
        target=req.target,
        reviewers=req.reviewers,
        summary=data.get("summary"),
        reviewer_id=req.reviewers[0] if len(req.reviewers) == 1 else None,
    )
    _findings.resolve_citations(result, cfg.repo_path)
    return _findings.validate_result(result)


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
_READ_MAX_LINES = 2000  # max lines returned by one read_file call
_READ_MAX_LINE_CHARS = 2000  # per-line cap (minified/generated lines)
_SCAN_MAX_FILES = 5000  # max files scanned by one search_files call
_SEARCH_MAX_LINE_CHARS = 500  # per-matched-line cap

# Vendored/generated dirs + binary/lock suffixes hidden from DISCOVERY
# (list_directory/search_files) so the agent isn't drowned on large projects.
# read_file is NOT filtered by these — an explicitly named file is always readable
# (only the security deny-list blocks it).
_NOISE_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        "target",
        ".gradle",
        ".idea",
        ".vscode",
        ".cache",
        "coverage",
        "htmlcov",
    }
)
_NOISE_SUFFIXES = (
    ".lock",
    ".min.js",
    ".min.css",
    ".map",
    ".pyc",
    ".pyo",
    ".so",
    ".o",
    ".a",
    ".class",
    ".jar",
    ".bin",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
)


def _git_tracked(root: str) -> set[str] | None:
    """Realpaths git considers part of the project (tracked + untracked but NOT
    gitignored), or None if not a git repo / git is unavailable. Lets discovery
    hide .gitignore'd build output — the `git ls-files` approach code-review-graph
    uses — without us reimplementing .gitignore parsing."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=15,
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


def _within_root(abs_path: str, root: str) -> bool:
    """True if a realpath stays inside the repo root — used by the discovery tools
    to reject symlinks pointing outside the root (read_file blocks these too, via
    _safe_path)."""
    return abs_path == root or abs_path.startswith(root + os.sep)


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
        try:
            target = _safe_path(root, path, denied)
        except ValueError as exc:
            return f"Error: {exc}"  # denied/escaping path — refused, agent recovers
        lo = max(1, line_start)
        hard_hi = lo + _READ_MAX_LINES - 1  # read at most _READ_MAX_LINES lines
        requested_end = line_end if line_end > 0 else None
        out: list[str] = []
        hit_cap = False
        # Stream the file; never read more than the returned window into memory,
        # so the cap holds even on a huge file (a narrow range stays cheap). A
        # missing/unreadable path (e.g. a file in the diff but not on disk, or a
        # directory) returns a recoverable message so the agent adapts — never an
        # uncaught OSError that aborts the whole run.
        try:
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
        except OSError as exc:
            return f"Error: cannot read '{path}': {exc.strerror or exc}"
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
        try:
            target = _safe_path(root, path, denied)
        except ValueError as exc:
            return f"Error: {exc}"  # denied/escaping path — refused, agent recovers
        entries: list[str] = []
        hidden = 0
        try:
            names = sorted(os.listdir(target))
        except OSError as exc:
            return f"Error: cannot list '{path}': {exc.strerror or exc}"
        for name in names:
            full = os.path.join(target, name)
            rp = os.path.realpath(full)
            if _is_denied(rp, denied) or not _within_root(rp, root):
                continue  # denied state path, or a symlink pointing outside the repo
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

        try:
            base = _safe_path(root, path, denied)
        except ValueError as exc:
            return f"Error: {exc}"  # denied/escaping path — refused, agent recovers
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"invalid regex: {exc}"
        hits: list[str] = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                d
                for d in dirnames
                if not skip_dir(d)
                and _within_root(os.path.realpath(os.path.join(dirpath, d)), root)
                and not _is_denied(os.path.realpath(os.path.join(dirpath, d)), denied)
            ]
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rp = os.path.realpath(full)
                # Skip denied state paths, symlinks pointing outside the repo, and noise.
                if _is_denied(rp, denied) or not _within_root(rp, root) or skip_file(rp, fn):
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
