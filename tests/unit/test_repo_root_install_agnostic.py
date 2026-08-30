"""Structural guard: tests must not reach repo-root-only artifacts through an
installed package's ``__file__``.

Deriving the repository root from an imported package
(``Path(rebar.__file__).resolve().parents[2]``) is correct only under an
*editable* install. Under a wheel/site-packages install that climb lands in the
virtualenv, so any repo-root-only artifact — ``scripts/``, ``docs/``,
``.github/``, ``hatch_build.py``, ``pyproject.toml``, ``LICENSE``, ``Makefile``,
``tests/`` — is not found and the "Test Suite (mirror)" non-editable sweep legs
go red (ticket e3e0-2e7c-b280-4b4a).

Tests must instead take the repo root from :mod:`tests._repo_root` (which walks
up from a test file's own on-disk location and is therefore correct under BOTH
layouts), or derive it from a bare ``Path(__file__)`` — never from an imported
package module's ``__file__``.

This guard flags the two shapes that reach *out of* the installed package:

* joining a repo-root-only path segment onto a package-``__file__`` climb, and
* passing a package-``__file__`` climb to a hatchling distribution builder
  (``WheelBuilder`` / ``SdistBuilder``), i.e. treating it as the checkout to
  build.

Reaches that stay *inside* the installed package (``… / "reviewers"``,
``… / "schemas"``, ``… / "rebar" / …``) are layout-agnostic and are not flagged.
"""

from __future__ import annotations

import ast
from pathlib import Path

from _repo_root import REPO_ROOT

# Path segments that ship only in the checkout, never inside the installed wheel.
FORBIDDEN_SEGMENTS = frozenset(
    {
        "scripts",
        "docs",
        ".github",
        "hatch_build.py",
        "pyproject.toml",
        "LICENSE",
        "Makefile",
        "tests",
        "server.json",
        "CONTRIBUTING.md",
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".pre-commit-config.yaml",
    }
)
_BUILDER_NAMES = frozenset({"WheelBuilder", "SdistBuilder"})
_OK_MARKER = "install-agnostic-ok"

_TESTS_DIR = REPO_ROOT / "tests"
_SELF = Path(__file__).resolve()
_HELPER = (_TESTS_DIR / "_repo_root.py").resolve()


def _unwrap_str(node: ast.expr) -> ast.expr:
    """Peel a single ``str(x)`` wrapper so a builder's ``str(root)`` arg is seen."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
    ):
        return node.args[0]
    return node


def _package_file_climb(node: ast.expr) -> bool:
    """True if ``node`` is ``Path(<name>.__file__)`` climbed by any of
    ``.resolve()`` / ``.parent`` / ``.parents[k]``."""
    cur = node
    while True:
        if isinstance(cur, ast.Subscript):  # ...parents[k]
            cur = cur.value
        elif isinstance(cur, ast.Attribute):  # ...parent / ...parents
            cur = cur.value
        elif isinstance(cur, ast.Call):
            func = cur.func
            if isinstance(func, ast.Attribute) and func.attr == "resolve":
                cur = func.value  # ...resolve()
            elif isinstance(func, ast.Name) and func.id == "Path" and cur.args:
                arg = cur.args[0]  # Path(<...>)
                return (
                    isinstance(arg, ast.Attribute)
                    and arg.attr == "__file__"
                    and isinstance(arg.value, ast.Name)
                )
            else:
                return False
        else:
            return False


def _leftmost(node: ast.expr) -> ast.expr:
    """The leftmost operand of a ``a / b / c`` division chain."""
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        node = node.left
    return node


def _forbidden_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split("/", 1)[0] in FORBIDDEN_SEGMENTS
    return False


class _Scanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.roots: set[str] = set()  # names assigned from a package-file climb
        self.violations: list[int] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        if _package_file_climb(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.roots.add(target.id)
        self.generic_visit(node)

    def _is_root(self, node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id in self.roots) or _package_file_climb(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div) and self._is_root(_leftmost(node)):
            operand = node
            while isinstance(operand, ast.BinOp) and isinstance(operand.op, ast.Div):
                if _forbidden_literal(operand.right):
                    self.violations.append(node.lineno)
                    break
                operand = operand.left
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _BUILDER_NAMES:
            for arg in node.args:
                if self._is_root(_unwrap_str(arg)):
                    self.violations.append(node.lineno)
                    break
        self.generic_visit(node)


def _scan(path: Path) -> list[int]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    scanner = _Scanner()
    scanner.visit(ast.parse(source))
    return sorted({n for n in scanner.violations if _OK_MARKER not in lines[n - 1]})


def test_tests_do_not_reach_repo_root_via_package_file() -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        resolved = path.resolve()
        if resolved in (_SELF, _HELPER):
            continue
        for lineno in _scan(path):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "these tests reach a repo-root-only artifact through an installed package's "
        "__file__ (breaks on non-editable installs) — take the repo root from "
        "`from _repo_root import REPO_ROOT` instead:\n  " + "\n  ".join(offenders)
    )
