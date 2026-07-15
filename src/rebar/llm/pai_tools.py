"""Provider-agnostic agent tools for the Pydantic AI runtime (7d58).

Pydantic AI binds PLAIN PYTHON FUNCTIONS (and native MCP toolsets). This module
supplies the agent's capability surface in Pydantic AI's shape, with read-only
safety guarantees enforced by the shared ``fs_tools`` primitives:

  * **Filesystem** (read-only) — ``read_file`` / ``list_directory`` / ``search_files``,
    confined to the repo root and refusing traversal + the state-dir deny-list by
    realpath (reusing :func:`rebar.llm.fs_tools._safe_path` /
    :func:`rebar.llm.config.denied_paths`).
  * **Rebar ops** (least privilege) — ``show_ticket`` always; ``comment_ticket`` only
    when not under the read-only gate (the WS-D3 contract: read + comment, never
    mutate work state or sign).
  * **MCP** — native Pydantic AI ``MCPServerStdio`` / ``MCPServerStreamableHTTP``
    toolsets built from the configured servers.

All heavy imports (pydantic_ai) are lazy/at the call boundary so ``import
rebar.llm`` stays stdlib-only; the FS/rebar tools are plain functions with no
third-party import at all.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from rebar.llm.config import denied_paths as _denied_realpaths
from rebar.llm.config import is_denied as _is_denied
from rebar.llm.errors import LLMConfigError, LLMRunnerError
from rebar.llm.fs_tools import (
    _SCAN_MAX_FILES,
    _discovery_filter,
    _safe_path,
    _within_root,
)

_READ_MAX_LINES = 2000
_READ_MAX_LINE_CHARS = 2000
_SEARCH_MAX_HITS = 200


def filesystem_tools(repo_path: str | None) -> list[Callable]:
    """Read-only FS tools confined to ``repo_path`` (realpath-checked, deny-list
    enforced). Plain functions — Pydantic AI reads their signature + docstring."""
    root = os.path.realpath(repo_path or ".")
    denied = _denied_realpaths(root)

    def read_file(path: str, line_start: int = 1, line_end: int = 0) -> str:
        """Read a UTF-8 text file under the repo root. ``line_start``/``line_end`` are
        1-based and inclusive (``line_end=0`` reads to EOF, capped). Read-only."""
        try:
            target = _safe_path(root, path, denied)
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            return f"Error: {exc}"
        lo = max(1, line_start)
        hi = line_end if line_end and line_end >= lo else len(lines)
        hi = min(hi, lo + _READ_MAX_LINES - 1)
        out = []
        for i in range(lo - 1, min(hi, len(lines))):
            text = lines[i].rstrip("\n")
            if len(text) > _READ_MAX_LINE_CHARS:
                text = text[:_READ_MAX_LINE_CHARS] + " …(truncated)"
            out.append(f"{i + 1}\t{text}")
        return "\n".join(out) or "(empty range)"

    skip_dir, skip_file = _discovery_filter(root)

    def list_directory(path: str = ".") -> str:
        """List entries of a directory under the repo root (dirs marked with a trailing
        ``/``), hiding vendored/generated/gitignored noise. Read-only."""
        try:
            target = _safe_path(root, path, denied)
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            names = sorted(os.listdir(target))
        except OSError as exc:
            return f"Error: {exc}"
        out = []
        for n in names:
            abs_n = os.path.realpath(os.path.join(target, n))
            is_dir = os.path.isdir(os.path.join(target, n))
            if not _within_root(abs_n, root) or _is_denied(abs_n, denied):
                continue
            if is_dir and skip_dir(n):
                continue
            if not is_dir and skip_file(abs_n, n):
                continue
            out.append(n + ("/" if is_dir else ""))
        return "\n".join(out) or "(empty)"

    def search_files(query: str, path: str = ".") -> str:
        """Case-sensitive substring search across text files under ``path`` (repo-root
        confined), applying the SAME discovery filter + caps as the default runner so
        both present an identical file view. Returns ``file:line: text`` hits. Read-only."""
        try:
            base = _safe_path(root, path, denied)
        except ValueError as exc:
            return f"Error: {exc}"
        hits: list[str] = []
        scanned = 0
        for dirpath, dirs, files in os.walk(base):
            real_dir = os.path.realpath(dirpath)
            # Prune denied / outside-root / noise dirs IN PLACE (and don't descend).
            dirs[:] = [
                d
                for d in dirs
                if _within_root(os.path.realpath(os.path.join(dirpath, d)), root)
                and not _is_denied(os.path.realpath(os.path.join(dirpath, d)), denied)
                and not skip_dir(d)
            ]
            if _is_denied(real_dir, denied) or not _within_root(real_dir, root):
                continue
            for name in files:
                abs_path = os.path.realpath(os.path.join(dirpath, name))
                if not _within_root(abs_path, root) or skip_file(abs_path, name):
                    continue
                scanned += 1
                if scanned > _SCAN_MAX_FILES:
                    return (
                        "\n".join(hits)
                        + f"\n…(scan limit {_SCAN_MAX_FILES} reached; narrow `path`)"
                    )
                try:
                    with open(abs_path, encoding="utf-8", errors="strict") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if query in line:
                                rel = os.path.relpath(abs_path, root)
                                hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                                if len(hits) >= _SEARCH_MAX_HITS:
                                    return "\n".join(hits) + "\n…(more hits truncated)"
                except (OSError, UnicodeDecodeError):
                    continue
        return "\n".join(hits) or "(no matches)"

    return [read_file, list_directory, search_files]


def _environment_roots() -> tuple[str, ...]:
    """Realpaths of the installed Python environment's own trees (interpreter
    prefixes + stdlib + site-packages dirs). A module whose origin lives under
    one of these is third-party/stdlib even when the environment itself (e.g. a
    ``.venv``) sits INSIDE the repo root (bug c810): the venv is part of the
    installed environment, not repo code."""
    import site
    import sys
    import sysconfig

    candidates: list[str] = [
        sys.prefix,
        sys.exec_prefix,
        sys.base_prefix,
        sys.base_exec_prefix,
    ]
    try:
        candidates.extend(site.getsitepackages())
        candidates.append(site.getusersitepackages())
    except (AttributeError, OSError):  # pragma: no cover - absent in some embedded interpreters
        pass
    paths = sysconfig.get_paths()
    candidates.extend(
        paths.get(key) or "" for key in ("purelib", "platlib", "stdlib", "platstdlib")
    )
    return tuple({os.path.realpath(c) for c in candidates if c})


def grounding_tools(repo_path: str | None) -> list[Callable]:
    """Environment-aware symbol resolver (bug 406f). The finder's ``filesystem_tools``
    are repo-scoped and CANNOT see a third-party dependency that lives in
    site-packages, so a library symbol reads as "not found" and gets wrongly flagged
    hallucinated/non-existent. This tool consults the INSTALLED Python environment
    (the same deps the code runs against) so the agent can CONFIRM a symbol exists
    before asserting it is absent. Read-only (import-locating; a member bind imports
    the module) and fail-open (never raises into the agent loop)."""
    root = os.path.realpath(repo_path or ".")

    def resolve_symbol(name: str, module: str = "") -> str:
        """Check whether a Python symbol/module EXISTS in the installed environment
        (stdlib + third-party site-packages) that your repo-scoped file tools cannot
        see. Pass a bare module (``anthropic``), a dotted ``module.Symbol``
        (``anthropic.Anthropic``), or ``name`` plus ``module`` for a
        ``from module import name`` binding. Returns ``EXISTS`` (with origin +
        whether it is repo-local or third-party) when importable, else
        ``UNRESOLVED`` — which is NOT proof of non-existence (it may be an
        uninstalled optional dependency): do not flag a symbol hallucinated on an
        UNRESOLVED result alone."""
        from rebar.grounding import resolve as _resolve

        try:
            loc = _resolve.resolve_in_environment(name, container=module or None, language="python")
        except Exception as exc:  # noqa: BLE001 — agent-tool boundary: surface as a string, never crash the loop
            return f"UNRESOLVED ({name!r}: resolver error {exc})"
        if loc is None:
            hint = f" in module {module!r}" if module else ""
            return (
                f"UNRESOLVED: {name!r}{hint} is not importable in the installed "
                "environment. This is NOT proof it does not exist — do not assert "
                "non-existence on this basis alone."
            )
        origin = str(loc.get("origin") or "?")
        qualified = loc["module"] + (f".{loc['attr']}" if loc.get("attr") else "")
        real_origin = os.path.realpath(origin) if os.path.isabs(origin) else ""
        # The environment's own trees (a venv INSIDE the repo, e.g. ./.venv)
        # are third-party/stdlib, never repo-local — check them FIRST (bug c810).
        env_local = bool(real_origin) and any(
            _within_root(real_origin, env_root) for env_root in _environment_roots()
        )
        inside = bool(real_origin) and not env_local and _within_root(real_origin, root)
        scope = "repo-local" if inside else "third-party/stdlib (site-packages)"
        return (
            f"EXISTS: {qualified} resolves in the installed environment [{scope}, origin={origin}]."
        )

    return [resolve_symbol]


def rebar_tools(repo_path: str | None, *, allow_comment: bool) -> list[Callable]:
    """Least-privilege rebar ticket tools (WS-D3): ``show_ticket`` always;
    ``comment_ticket`` only when ``allow_comment``. Nothing else — no create/edit/
    transition/claim/sign."""

    def show_ticket(ticket_id: str) -> str:
        """Read a rebar ticket's compiled state (id, title, status, description,
        comments, deps) as JSON. Read-only."""
        import json

        from rebar import _reads

        try:
            return json.dumps(_reads.show_ticket(ticket_id, repo_root=repo_path))
        except Exception as exc:  # noqa: BLE001 — agent-tool boundary: surface the error to the LLM as a tool-result string, never crash the agent loop
            return f"Error: {exc}"

    tools: list[Callable] = [show_ticket]

    if allow_comment:

        def comment_ticket(ticket_id: str, body: str) -> str:
            """Append a comment to a rebar ticket — the ONLY write this agent may make.
            Cannot transition, edit, claim, or sign."""
            import rebar

            try:
                rebar.comment(ticket_id, body, repo_root=repo_path)
                return f"Commented on {ticket_id}."
            except Exception as exc:  # noqa: BLE001 — agent-tool boundary: surface the error to the LLM as a tool-result string, never crash the agent loop
                return f"Error: {exc}"

        tools.append(comment_ticket)

    return tools


def mcp_toolsets(servers: dict) -> list:
    """Build native Pydantic AI MCP toolsets from the configured servers (stdio when a
    ``command`` is given, streamable-HTTP when a ``url`` is given). Empty in -> empty
    out; a malformed entry is a clear error, never a silent tool-less run."""
    if not servers:
        return []
    try:
        from pydantic_ai.mcp import MCPServerStdio, MCPServerStreamableHTTP
    except ImportError as exc:  # pragma: no cover - mcp ships with pydantic-ai-slim
        raise LLMConfigError(
            "the pydantic_ai runner needs the 'agents' extra for MCP. "
            "Install it with: pip install 'nava-rebar[agents]'"
        ) from exc
    toolsets: list[Any] = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            raise LLMRunnerError(f"MCP server {name!r} config must be a mapping")
        if cfg.get("url"):
            toolsets.append(MCPServerStreamableHTTP(cfg["url"]))
        elif cfg.get("command"):
            toolsets.append(MCPServerStdio(cfg["command"], args=list(cfg.get("args") or [])))
        else:
            raise LLMRunnerError(
                f"MCP server {name!r} needs a 'command' (stdio) or 'url' (http) — got {sorted(cfg)}"
            )
    return toolsets
