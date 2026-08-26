"""Layering guard: ``rebar.reducer`` must not depend *up* on ``rebar._engine_support``
at module scope.

``reducer`` is the low-level event-replay layer; ``_engine_support`` is the higher
read-tooling layer that legitimately imports ``reducer`` at module scope (downward is
fine). The reverse — a module-scope ``reducer → _engine_support`` import — closes the
``reducer ↔ _engine_support`` package cycle at load time. The one intrinsic upward
touch (``reducer._processors`` needs ``resolve_ticket_id`` at replay) is kept
*function-local* on purpose; this test fails if any reducer module promotes such an
import to module scope (the regression that the ``rebar._alias`` extraction removed,
having previously been worked around with a fragile lazy-import pair).

Companion to ``test_core_optionality.py`` (same module-scope AST-walker shape).
"""

from __future__ import annotations

import ast
from pathlib import Path

from _tree_scan import parsed_python_files

import rebar

_SRC = Path(rebar.__file__).resolve().parent
_REDUCER = _SRC / "reducer"
_FORBIDDEN_PREFIX = "rebar._engine_support"


def _module_scope_imports(tree: ast.Module):
    """Yield Import/ImportFrom nodes at MODULE scope (recurse into module-level
    if/try/with/for/while/match, but NOT into function/class bodies — those are
    lazy by design)."""

    def walk(body):
        for node in body:
            if isinstance(node, ast.Import | ast.ImportFrom):
                yield node
            elif isinstance(node, ast.If | ast.Try | ast.With | ast.For | ast.While):
                yield from walk(node.body)
                yield from walk(getattr(node, "orelse", []))
                yield from walk(getattr(node, "finalbody", []))
                for h in getattr(node, "handlers", []):
                    yield from walk(h.body)
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    yield from walk(case.body)

    yield from walk(tree.body)


def _targets(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    return [node.module] if node.module else []


def test_reducer_has_no_module_scope_engine_support_import() -> None:
    offenders: list[str] = []
    for module in parsed_python_files(_REDUCER):
        if "__pycache__" in module.path.parts:
            continue
        for node in _module_scope_imports(module.tree):
            for name in _targets(node):
                if name == _FORBIDDEN_PREFIX or name.startswith(_FORBIDDEN_PREFIX + "."):
                    offenders.append(f"{module.path.relative_to(_SRC.parent)}: import {name}")
    assert not offenders, (
        "reducer is the low-level replay layer and must not import _engine_support at "
        "MODULE scope (it closes the reducer ↔ _engine_support cycle). Keep the import "
        "function-local:\n" + "\n".join(offenders)
    )
