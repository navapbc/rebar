"""Read-only, sandboxed filesystem/repo helpers for agent runners (WS-A).

The shared fs/repo safety cluster the runner's tools build on so an agent can read
and search the repository safely. These are the runner-agnostic primitives
(``_safe_path``, the discovery filter, the noise/cap constants) consumed by
``rebar.llm.pai_tools`` to construct the provider-agnostic Pydantic AI file tools.

The helpers are deliberately read-only (no write/edit/bash), rooted at a single
repo path, and hardened three ways: ``_safe_path`` refuses traversal and any denied
state path (by realpath); discovery (list/search) hides vendored/generated/
gitignored noise and symlinks escaping the root; and per-call caps bound
latency/cost/context. This module is stdlib-only — it imports no third-party
dependency.
"""

from __future__ import annotations

import os
import subprocess

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


# Per-call cap so an agent loop can't blow up latency/cost/context scanning a huge
# tree (windowing/long-line caps for read_file live in pai_tools, which owns the
# tool bodies). Consumed by pai_tools.search_files.
_SCAN_MAX_FILES = 5000  # max files scanned by one search_files call

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


# Path components that mark an INSTALLED-DEPENDENCY location rather than first-party
# source. A repo-local virtualenv (e.g. `<repo>/.venv/lib/pythonX/site-packages/…`)
# lives *under* the repo root, so `_within_root` alone would wrongly call a
# third-party symbol "repo-local"; excluding these roots keeps the first-party vs
# third-party classification honest. `site-packages`/`dist-packages` are the
# universal install dirs; the venv names mirror `_NOISE_DIRS` for defence in depth.
_DEPENDENCY_PATH_PARTS = frozenset({"site-packages", "dist-packages", ".venv", "venv"})


def _is_dependency_path(abs_path: str) -> bool:
    """True if ``abs_path`` lives inside an installed-dependency / virtualenv root
    (site-packages, dist-packages, or a `.venv`/`venv` dir), even when that root is
    nested inside the repo. Lets callers keep a repo-local `.venv` from masquerading
    as first-party source."""
    return not _DEPENDENCY_PATH_PARTS.isdisjoint(abs_path.split(os.sep))
