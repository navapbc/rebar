"""A scripts module that imports a sibling by bare name must first insert its
own directory into sys.path. Full-suite collection can hide a missing insert
because tests/scripts/conftest.py adds that path globally. This guard also runs
the affected unit test alone, where importlib's path loader does not add it.
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
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_TESTS_DIR = _REPO_ROOT / "tests"

# The test module whose subset run is the reported reproduction.
_REPRO_MODULE = "test_reconcile_bridge_canary_class.py"


def _insert_dependent_modules() -> set[str]:
    """Top-level module names that exist only as a loose file needing a ``sys.path`` entry.

    A name qualifies when a ``<name>.py`` sits under ``scripts/`` or anywhere under
    ``tests/`` — neither directory is a package, so such a name is importable only when its
    directory is on ``sys.path``. Standard-library names are excluded so a test helper that
    shadows a stdlib module name cannot produce a false hit.
    """
    names = {p.stem for p in _SCRIPTS_DIR.glob("*.py")}
    names |= {p.stem for p in _TESTS_DIR.rglob("*.py")}
    return names - set(sys.stdlib_module_names)


def _sibling_imports(
    tree: ast.AST, own_stem: str, insert_dependent: set[str]
) -> list[tuple[int, str]]:
    """Return ``(lineno, module)`` for every insert-dependent bare import in *tree*."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is an explicit relative import — a different mechanism entirely.
            modules = [node.module] if node.level == 0 and node.module else []
        else:
            continue
        for module in modules:
            root = module.split(".")[0]
            if root != own_stem and root in insert_dependent:
                hits.append((node.lineno, module))
    return hits


def _syspath_insert_linenos(tree: ast.AST) -> list[int]:
    """Line numbers of every ``sys.path.insert(...)`` call in *tree*."""
    linenos: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "insert"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            linenos.append(node.lineno)
    return linenos


def test_every_sibling_import_under_scripts_is_preceded_by_a_path_insert() -> None:
    """No ``scripts/`` module imports an insert-dependent sibling on ambient ``sys.path``.

    RED against the defect: with the insert removed from ``scripts/canary_bridge.py`` this
    fails naming that file and line, which is exactly the state that produced the reported
    ``ModuleNotFoundError``. Run with bare ``pytest``; ``python -m pytest`` can false-green
    by adding the checkout root to ``sys.path``.
    """
    insert_dependent = _insert_dependent_modules()
    offenders: list[str] = []

    for path in sorted(_SCRIPTS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        hits = _sibling_imports(tree, path.stem, insert_dependent)
        if not hits:
            continue
        insert_lines = _syspath_insert_linenos(tree)
        lines = source.splitlines()
        for lineno, module in hits:
            preceding = "\n".join(lines[: lineno - 1])
            has_insert = any(insert_line < lineno for insert_line in insert_lines)
            # `__file__` is what makes the insert invocation-independent: a hard-coded or
            # cwd-relative path would resolve differently depending on how the process
            # was started, which is the very failure mode being closed.
            derives_from_file = "__file__" in preceding
            if not (has_insert and derives_from_file):
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}:{lineno}: import {module} "
                    f"(sys.path.insert above: {has_insert}; derives from __file__: "
                    f"{derives_from_file})"
                )

    assert offenders == [], (
        "these `scripts/` modules import a sibling that is importable only when its "
        "directory is on sys.path, without putting it there first — so the import resolves "
        "under a full test session (tests/scripts/conftest.py leaks the entry) and dies "
        "under a subset run. Insert the directory, derived from `__file__`, above the "
        "import:\n  " + "\n  ".join(offenders)
    )


def test_repro_module_passes_standalone(tmp_path: Path) -> None:
    """Run the reproduced unit module with bare pytest from outside the repository.
    Execute its fixture, since collecting the module alone would not reach the
    path-loaded canary_bridge import that exposed the missing sibling path.
    """
    console_script = Path(sys.executable).parent / "pytest"
    if not console_script.exists():  # pragma: no cover - environment without the script
        pytest.skip("no `pytest` console script next to the running interpreter")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    child_basetemp = tmp_path / "standalone-pytest"
    proc = subprocess.run(
        [
            str(console_script),
            str(_TESTS_DIR / "unit" / _REPRO_MODULE),
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(child_basetemp),
        ],
        cwd=Path(os.environ.get("TMPDIR", "/tmp")).resolve(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, (
        f"a standalone bare `pytest` run of {_REPRO_MODULE} failed:\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "ModuleNotFoundError" not in proc.stdout + proc.stderr
    assert child_basetemp.is_dir(), "nested pytest did not use its parent-owned basetemp"
