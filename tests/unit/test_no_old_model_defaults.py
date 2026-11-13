"""No default in ``src/rebar`` names a pre-5.5 Claude model (bug negligent-paranoid-raccoon).

The configured models moved to the 5.5 generation (epic pompous-vitriolic-macaw); a hard-coded
default that still names an older id silently sizes, stamps or routes for the wrong model. The
scan covers function defaults and string constants assigned at module or class level. The
per-model capability and window tables are dict or tuple literals, so they are out of scope by
construction.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src" / "rebar"
_OLD_ID = re.compile(r"claude-(?:opus|sonnet|haiku)-[1-4](?![0-9])|claude-[1-3](?:-[0-9]+)?-")


def _old(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if _OLD_ID.search(node.value):
            return node.value
    return None


def _assigned(body: list[ast.stmt]) -> list[ast.expr | None]:
    return [s.value for s in body if isinstance(s, (ast.Assign, ast.AnnAssign))]


def _hits(path: Path, root: Path = _SRC) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    candidates: list[ast.expr | None] = _assigned(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            candidates += [*node.args.defaults, *node.args.kw_defaults]
        elif isinstance(node, ast.ClassDef):
            candidates += _assigned(node.body)
    rel = path.relative_to(root.parents[1])
    return [f"{rel}:{n.lineno} {v}" for n in candidates if n is not None and (v := _old(n))]


def test_no_function_or_constant_default_names_a_pre_5_5_claude_model() -> None:
    hits = [hit for path in sorted(_SRC.rglob("*.py")) for hit in _hits(path)]
    assert not hits, "pre-5.5 Claude ids in defaults:\n" + "\n".join(hits)


def test_the_scan_catches_each_shape(tmp_path: Path) -> None:
    sample = (
        'A = "claude-sonnet-4-6"\n'
        'def f(model="anthropic:claude-opus-4-8", *, m2="claude-haiku-4-5"): ...\n'
        'class C:\n    model: str = "claude-3-5-sonnet"\n'
        'OK = "claude-sonnet-5-5"\nTABLE = {"claude-opus-4-8": 1}\n'
    )
    probe_path = tmp_path / "src" / "rebar" / "probe.py"
    probe_path.parent.mkdir(parents=True)
    probe_path.write_text(sample)
    found = _hits(probe_path, root=tmp_path / "src" / "rebar")
    assert [h.split(" ", 1)[1] for h in found] == [
        "claude-sonnet-4-6",
        "anthropic:claude-opus-4-8",
        "claude-haiku-4-5",
        "claude-3-5-sonnet",
    ]


def test_fixture_emit_model_follows_the_standard_class_default() -> None:
    from rebar.llm.config import VERIFIER_DEFAULT_MODEL
    from rebar.llm.evals import fixture_emit

    assert fixture_emit._MODEL == f"anthropic:{VERIFIER_DEFAULT_MODEL}"
