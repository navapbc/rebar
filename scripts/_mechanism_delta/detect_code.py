"""Python-AST mechanism detectors: ``lock`` and ``autouse_fixture``.

Split from the other detectors by INPUT SURFACE — everything here is answered by parsing
Python source with :mod:`ast`, so the whole module shares one file walk, one parse cache and
one failure mode (an unparseable file is skipped, never fatal: a syntax error is the type
checker's and the linter's finding, not this gate's).

``lock``
    A concurrency mechanism is either a lock CLASS (``ast.ClassDef`` whose name matches
    ``.*Lock.*``) or a lock FILE (a string literal ending ``.lock``). Both are counted
    because both are the thing a future defect is filed against: a new lock class is new
    in-process serialisation, a new ``.lock`` filename is new on-disk serialisation, and
    each one adds an ordering that some later code path can violate. The scan covers the
    shipped package plus the gate tooling (``src/`` and ``scripts/``) — not ``tests/``,
    whose lock doubles are fixtures of the mechanisms already counted here, not new surface.

``autouse_fixture``
    A ``pytest`` fixture with ``autouse=True`` applies to every test in its scope WITHOUT
    being named by any of them, so it is invisible mechanism: it changes what the suite
    proves while no test mentions it. Names are site-qualified (``<path>::<fixture>``)
    because the partition rule is per definition site and fixture names repeat freely
    across ``conftest.py`` files.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .markers import Site

LOCK_CLASS_RE = re.compile(r".*Lock.*")
LOCK_FILE_RE = re.compile(r"\.lock$")

# Roots scanned for locks: the shipped package and the gate tooling that runs beside it.
LOCK_ROOTS: tuple[str, ...] = ("src", "scripts")

# Root scanned for autouse fixtures.
FIXTURE_ROOT = "tests"

_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _iter_python(repo_root: Path, relative_root: str):
    """Yield ``(path, parsed_tree)`` for every parseable ``.py`` file under a root."""
    root = repo_root / relative_root
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        yield path, tree


def detect_locks(repo_root: Path) -> list[Site]:
    """Every lock class definition and ``*.lock`` filename literal, with its site."""
    sites: list[Site] = []
    for relative_root in LOCK_ROOTS:
        for path, tree in _iter_python(repo_root, relative_root):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and LOCK_CLASS_RE.match(node.name):
                    sites.append((node.name, path, node.lineno))
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and LOCK_FILE_RE.search(node.value)
                ):
                    sites.append((node.value, path, node.lineno))
    return sites


def _is_autouse_fixture(decorator: ast.expr) -> bool:
    """True for ``@…fixture(…, autouse=True)`` in any of its spellings.

    Resolution is by the callee's terminal name (``pytest.fixture``, a bare imported
    ``fixture``, ``pytest_asyncio.fixture``) because the LSP-free AST cannot bind the import;
    the ``autouse=True`` keyword is what actually makes it a mechanism, and it must be the
    literal ``True`` — a computed flag is not something this gate can read.
    """
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    terminal = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if terminal != "fixture":
        return False
    return any(
        kw.arg == "autouse" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in decorator.keywords
    )


def detect_autouse_fixtures(repo_root: Path) -> list[Site]:
    """Every ``autouse=True`` pytest fixture under ``tests/``, site-qualified."""
    sites: list[Site] = []
    for path, tree in _iter_python(repo_root, FIXTURE_ROOT):
        rel = path.relative_to(repo_root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, _FUNC_DEFS):
                continue
            if any(_is_autouse_fixture(dec) for dec in node.decorator_list):
                sites.append((f"{rel}::{node.name}", path, node.lineno))
    return sites
