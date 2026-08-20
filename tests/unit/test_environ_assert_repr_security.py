"""Regression oracles for ambient-secret exposure through pytest assertion reprs.

Sibling of ``test_fixture_env_repr_security.py`` (fixture-argument reprs) and
``test_subprocess_env_repr_security.py`` (subprocess environment dicts). This module
covers the third route by which a raw ``os.environ`` reaches pytest's output: naming it
directly inside an ``assert``. pytest's assertion rewriter reprs the operands of a failed
comparison, so ``assert "X" not in os.environ`` renders the WHOLE inherited environment —
every ambient API key, token and signing key with it — into the failure report and thus
into retained CI logs. Binding the membership test to a bool first
(``present = "X" in os.environ`` / ``assert not present``) renders only ``True``/``False``.

``os.environ.get("X")`` and ``os.environ["X"]`` assertions are SAFE: only the retrieved
value is an operand, so they are deliberately not findings.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"

_SENTINEL_NAME = "REBAR_ENVIRON_ASSERT_TRACE_SENTINEL"
_SENTINEL_VALUE = "synthetic-not-a-secret-9c66-3e2b"

# The op-cert startup oracle guards that the composed signer — not the ambient environment —
# is the key source. That guard runs with a live signer, so it is the single worst place for
# a whole-environment repr to survive; this module holds it to the bool-bound form.
_GUARDED_MODULE = _TESTS_ROOT / "unit" / "test_rp04_s6_opcert_startup_heldout.py"
_GUARDED_TEST = "test_both_chains_sign_from_composed_signer_without_env"
_GUARDED_VAR = "REBAR_OPCERT_KEY_PATH"


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _bare_environ_nodes(tree: ast.AST) -> list[ast.Assert]:
    """Asserts that name ``os.environ`` itself, rather than a value retrieved from it.

    A reference is CONSUMED — and so safe — when it is the receiver of an attribute access
    (``os.environ.get(...)``) or of a subscript (``os.environ["X"]``): those render only the
    retrieved value. Anything else leaves the mapping as a rendered operand.
    """
    hits: list[ast.Assert] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        operands = [node.test] + ([node.msg] if node.msg is not None else [])
        consumed: set[int] = set()
        bare: list[ast.AST] = []
        for operand in operands:
            for sub in ast.walk(operand):
                if isinstance(sub, (ast.Attribute, ast.Subscript)) and _is_os_environ(sub.value):
                    consumed.add(id(sub.value))
        for operand in operands:
            for sub in ast.walk(operand):
                if _is_os_environ(sub) and id(sub) not in consumed:
                    bare.append(sub)
        if bare:
            hits.append(node)
    return hits


def _bare_environ_sites() -> list[str]:
    offenders: list[str] = []
    # No self-exclusion: this module's offender shapes are string literals parsed at run
    # time, not live asserts, so the audit polices its own source too.
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(_REPO_ROOT)
        offenders.extend(f"{relative}:{node.lineno}" for node in _bare_environ_nodes(tree))
    return offenders


def _guarded_statements() -> list[ast.stmt]:
    """The REAL source statements of the op-cert startup guard, in source order.

    Extracted from the live module so the probe below exercises the shipped text of the
    guard rather than a restatement of it — a reworded assertion message cannot make the
    oracle pass.
    """
    tree = ast.parse(_GUARDED_MODULE.read_text(encoding="utf-8"), filename=str(_GUARDED_MODULE))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _GUARDED_TEST
    )
    candidates = sorted(
        (
            node
            for node in ast.walk(function)
            if isinstance(node, (ast.Assert, ast.Assign, ast.AnnAssign))
        ),
        key=lambda node: node.lineno,
    )
    # Follow the guard forward through whatever it binds, so the bool-bound form
    # (`present = ... in os.environ` / `assert not present`) is captured whole.
    selected: list[ast.stmt] = []
    tracked = {_GUARDED_VAR}
    for node in candidates:
        text = ast.unparse(node)
        if not any(name in text for name in tracked):
            continue
        selected.append(node)
        tracked.update(_assigned_names(node))
    return selected


def _assigned_names(node: ast.stmt) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else getattr(node, "target", None)
    targets = targets if isinstance(targets, list) else ([targets] if targets else [])
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _run_guard_probe(tmp_path: Path, traceback_style: str) -> subprocess.CompletedProcess[str]:
    body = "\n".join(f"    {line}" for line in _guarded_statements_source().splitlines())
    probe = tmp_path / "test_environ_assert_guard.py"
    probe.write_text(f"import os\n\n\ndef test_guard():\n{body}\n", encoding="utf-8")
    # A small, fully controlled child environment: the sentinel leads, so pytest's bounded
    # mapping repr cannot truncate it away and a miss is a real miss.
    child_env = {
        _SENTINEL_NAME: _SENTINEL_VALUE,
        _GUARDED_VAR: "/synthetic/path/that/forces/the/guard/to/fail",
        "PATH": os.environ.get("PATH", os.defpath),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / f"pytest-{traceback_style}"),
            f"--tb={traceback_style}",
            str(probe),
        ],
        cwd=_REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _guarded_statements_source() -> str:
    return "\n".join(ast.unparse(node) for node in _guarded_statements())


def test_no_assert_in_the_suite_renders_the_raw_environment() -> None:
    assert _bare_environ_sites() == []


def test_the_opcert_startup_guard_is_still_present() -> None:
    """A rename or deletion must not silently vacuum the probe below."""
    statements = _guarded_statements()

    assert any(isinstance(node, ast.Assert) for node in statements)


@pytest.mark.parametrize("traceback_style", ["long", "short"])
def test_opcert_startup_guard_failure_does_not_render_ambient_secret(
    tmp_path: Path, traceback_style: str
) -> None:
    result = _run_guard_probe(tmp_path, traceback_style)

    assert result.returncode == 1, result.stdout + result.stderr
    assert _SENTINEL_VALUE not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "source",
    [
        'assert "REBAR_OPCERT_KEY_PATH" not in os.environ',
        'assert "REBAR_OPCERT_KEY_PATH" in os.environ',
        "assert os.environ == {}",
        "assert flag, os.environ",
        'assert "X" not in dict(os.environ)',
    ],
)
def test_detector_flags_a_rendered_environment(source: str) -> None:
    assert len(_bare_environ_nodes(ast.parse(source))) == 1


@pytest.mark.parametrize(
    "source",
    [
        'assert os.environ.get("REBAR_OPCERT_KEY_PATH") is None',
        'assert os.environ["REBAR_OPCERT_KEY_PATH"] == "value"',
        'present = "REBAR_OPCERT_KEY_PATH" in os.environ\nassert not present',
    ],
)
def test_detector_sanctions_value_scoped_assertions(source: str) -> None:
    assert _bare_environ_nodes(ast.parse(source)) == []
