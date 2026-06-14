"""Structural registry-coverage test for applier._LEAVES.

Iterates every (direction, action) entry in the _LEAVES dispatch table and
asserts each leaf has a real body. The test fails today (5 of 12 leaves are
no-op stubs returning ``ApplyResult(direction, action, {})``); it MUST pass
once story bd19-d744-b8c7-4079 is implemented.

A leaf "has a real body" iff EITHER:
  * the function body has ≥3 statements (counted from the AST, excluding the
    docstring — so the check is invariant to formatting / line-wrapping), OR
  * the source contains a regex match for an external-effect call
    (`client.`, `ticket.`, `.write_text`, `.append`, `subprocess.run`,
    `_call_with_retry`).

The combination is intentional: simple wrapper leaves (e.g. inbound
clean_label that just iterates and calls client.remove_label) pass the regex
arm; meatier delegating leaves pass the statement-count arm; pure no-op stubs
that just return an empty ApplyResult fail both.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("leaves_coverage_applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["leaves_coverage_applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


_EFFECT_RE = re.compile(r"client\.|ticket\.|\.write_text|\.append|subprocess\.run|_call_with_retry")


def _effective_statement_count(src: str) -> int:
    """Count the function's body statements (excluding the docstring) from the AST.

    AST-based so the check is invariant to formatting / line-wrapping: a formatter
    that compacts a multi-line call must not change whether a leaf 'has a real body'.
    """
    func = next(
        (
            n
            for n in ast.walk(ast.parse(textwrap.dedent(src)))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        None,
    )
    if func is None:
        return 0
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop the leading docstring
    return len(body)


def test_every_leaf_has_real_body(applier):
    """Every entry in _LEAVES must point at a non-stub function body."""
    leaves = applier._LEAVES
    assert leaves, "_LEAVES registry is empty"

    failures: list[str] = []
    for key, fn in leaves.items():
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError) as exc:
            failures.append(f"{key}: source unavailable ({exc})")
            continue
        stmt_count = _effective_statement_count(src)
        has_effect = bool(_EFFECT_RE.search(src))
        if stmt_count < 3 and not has_effect:
            failures.append(
                f"{key} -> {fn.__name__}: "
                f"body_statements={stmt_count}, has_effect_call={has_effect} — "
                "appears to be a no-op stub"
            )

    if failures:
        msg = "Leaves with no-op stub bodies:\n  " + "\n  ".join(failures)
        pytest.fail(msg)
