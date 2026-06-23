"""Guard (ticket 3e28): the init code path must not re-introduce a hardcoded
``.tickets-tracker`` path literal — the tracker dir is now resolved through
``config.tracker_dir()`` (the single source). Prose may still mention the default
name: module/function docstrings are excluded, and so are multi-line content
templates (the embedded ``.pre-commit-config.yaml`` / ``.gitignore`` whose comments
reference the dir). Only a SINGLE-LINE string literal — the shape a path-construction
literal like ``os.path.join(repo, ".tickets-tracker")`` takes — is forbidden, so a
future edit that hardcodes the dir again fails this test.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import rebar._commands.init as init_mod

pytestmark = pytest.mark.unit


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """ids() of the Constant nodes that are docstrings (first stmt of a module /
    function / class), which are allowed to mention the default dir name."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def test_no_hardcoded_tracker_dir_literal_in_init_code() -> None:
    src = pathlib.Path(init_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstrings = _docstring_constant_ids(tree)
    offenders = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ".tickets-tracker" in node.value
        and "\n" not in node.value  # single-line → a path token, not an embedded template
        and id(node) not in docstrings
    ]
    assert not offenders, (
        "hardcoded '.tickets-tracker' string literal(s) in init executable code "
        f"(use config.tracker_dir() instead): {offenders}"
    )
