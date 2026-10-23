"""Cache parsed Python source trees once per worker for policy oracles.

Tests and source roots are scanned on demand, then shared as immutable results so
whole-tree checks do not repeat the same parse.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


class ParsedModule:
    """A single parsed Python source file from a scanned tree."""

    __slots__ = ("path", "relative", "source", "tree")

    def __init__(self, path: Path, source: str, tree: ast.AST) -> None:
        self.path = path
        self.relative = path.relative_to(_REPO_ROOT)
        self.source = source
        self.tree = tree


@functools.cache
def parsed_python_files(root: Path) -> tuple[ParsedModule, ...]:
    """Return every ``*.py`` under ``root`` parsed once (cached per process).

    The result is sorted by path and cached, so repeated calls with the same
    ``root`` return the identical tuple without re-reading or re-parsing. The
    ``root`` is resolved before caching so callers that spell the same directory
    differently (``_SRC`` vs ``REPO_ROOT / "src" / "rebar"``) share one parse.
    """
    root = root.resolve()
    modules: list[ParsedModule] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        modules.append(ParsedModule(path, source, ast.parse(source, filename=str(path))))
    return tuple(modules)
