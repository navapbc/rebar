"""Read-only, sandboxed filesystem/repo tools for agent runners (WS-A).

Extracted verbatim from ``rebar.llm.runner`` (which had grown past the 800-LOC
soft cap) — this is the fs/repo cluster the runners hand to an agent so it can read
and search the repository safely, and the place the workflow engine's git-ref
snapshot code (WS-D) will build on. No behavior change: the runner imports
``_filesystem_tools`` from here.

The tools are deliberately read-only (no write/edit/bash), rooted at a single repo
path, and hardened three ways: ``_safe_path`` refuses traversal and any denied
state path (by realpath); discovery (list/search) hides vendored/generated/
gitignored noise and symlinks escaping the root; and per-call caps bound
latency/cost/context. Output is line-numbered so the agent can cite ``path:line``.

``langchain_core`` is imported INSIDE ``_filesystem_tools`` (the ``agents`` extra),
never at module top, so importing this module stays stdlib-only.
"""

from __future__ import annotations

import os
import subprocess

from rebar.llm.config import denied_paths as _denied_realpaths
from rebar.llm.config import is_denied as _is_denied


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
