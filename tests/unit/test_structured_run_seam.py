"""The structured-output execution cluster is a LEAF module (task 2682).

``runner.py`` sat at exactly the 800-LOC hard cap, so the next line added to it would have
failed `Verified` for everyone — and two stories in the provider-seam epic must add lines to
it. This pins the split that bought the headroom back.

A relocation has no new behaviour, so the real regression net is the existing suite passing
unchanged. What CAN be asserted here is the structure the split has to preserve: the new
module is a genuine leaf, the split did not fragment into a stub, and nothing in the move
dragged a heavy import into the stdlib-only ``import rebar.llm`` path.

The upper LOC ceiling is NOT asserted here: `.github/module-size-limit.txt` is the single
authoritative bound (ADR 0058), enforced by the CI module-size gate and mirrored in
`tests/unit/test_module_size_contract.py`.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "rebar" / "llm"
_RUNNER = _SRC / "runner.py"
_STRUCTURED_RUN = _SRC / "structured_run.py"

# AGENTS.md: never create a file under 100 LOC by splitting. This is the ONLY LOC bound this
# test asserts — the upper ceiling is `.github/module-size-limit.txt`, enforced by the CI
# module-size gate and mirrored in `test_module_size_contract.py` (ADR 0058).
_SPLIT_FLOOR = 100


def _loc(path: pathlib.Path) -> int:
    return len(path.read_text().splitlines())


# ── §A given: the split happened and did not fragment into a stub ────────────────────────


def test_structured_run_module_exists():
    """The extracted cluster has a home."""
    assert _STRUCTURED_RUN.is_file(), "src/rebar/llm/structured_run.py was not created"


def test_structured_run_clears_the_split_floor():
    """AGENTS.md forbids creating a sub-100-LOC file by splitting — a split that small is a
    sign the seam was mechanical rather than real."""
    loc = _loc(_STRUCTURED_RUN)
    assert loc >= _SPLIT_FLOOR, (
        f"structured_run.py is {loc} LOC; splitting must not create a file under {_SPLIT_FLOOR} LOC"
    )


# ── §B held out from the implementer ────────────────────────────────────────────────────


def test_structured_run_has_no_runtime_import_from_runner():
    """The leaf rule, asserted on the AST rather than on a grep.

    A ``TYPE_CHECKING``-guarded import of ``RunRequest`` is permitted — it is a string
    annotation at runtime and creates no cycle. A module-level runtime import of ``runner``
    is not: it would make the dependency bidirectional and defeat the extraction."""
    tree = ast.parse(_STRUCTURED_RUN.read_text())

    def _guarded_by_type_checking(node: ast.AST) -> bool:
        for parent in ast.walk(tree):
            if isinstance(parent, ast.If):
                test = parent.test
                name = getattr(test, "id", None) or getattr(test, "attr", None)
                if name == "TYPE_CHECKING" and node in ast.walk(parent):
                    return True
        return False

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("llm.runner"):
            if not _guarded_by_type_checking(node):
                offenders.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("llm.runner") and not _guarded_by_type_checking(node):
                    offenders.append(f"line {node.lineno}: import {alias.name}")

    assert not offenders, (
        "structured_run.py must not import `runner` at RUNTIME (a TYPE_CHECKING-guarded "
        f"annotation import is fine): {offenders}"
    )


def test_import_rebar_llm_stays_stdlib_only():
    """The moved cluster must not drag a heavy dependency into the lean import path.

    Run in a SUBPROCESS: this test file's own imports, and any earlier test in the session,
    would otherwise have already put these modules in sys.modules and mask a regression."""
    code = (
        "import sys;"
        "before=set(sys.modules);"
        "import rebar.llm;"
        "heavy={m for m in set(sys.modules)-before "
        "if m.split('.')[0] in {'httpx','anthropic','pydantic_ai','openai','boto3','botocore'}};"
        "print(sorted(heavy))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]", f"`import rebar.llm` pulled in heavy modules after the split: {out}"


def test_moved_symbols_are_importable_from_the_new_module():
    """The cluster moved WHOLE. A partial move (leaving one member behind) would still pass
    the size checks while splitting a call-graph seam down the middle."""
    from rebar.llm import structured_run

    for name in (
        "_pai_structured",
        "_extract_usage",
        "_warn_if_zeroed_usage",
        "effective_max_iterations",
        "effective_max_tokens",
    ):
        assert hasattr(structured_run, name), f"{name} did not move to structured_run.py"


def test_runner_no_longer_defines_the_moved_symbols():
    """Contrast case: the symbols must have MOVED, not been copied. A duplicate definition
    would drift silently — the runner's copy would keep being used while the new module's
    diverged."""
    tree = ast.parse(_RUNNER.read_text())
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    leftovers = defined & {
        "_pai_structured",
        "_extract_usage",
        "_warn_if_zeroed_usage",
        "effective_max_iterations",
        "effective_max_tokens",
    }
    assert not leftovers, f"runner.py still DEFINES moved symbols (copied, not moved): {leftovers}"
