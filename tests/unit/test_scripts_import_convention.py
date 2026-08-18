"""Guard for bug 291e-7b48-3f24-41c6: sibling imports under ``scripts/`` need a path insert.

Companion to ``test_tests_import_convention.py`` (bug a371), which AST-scans for
``tests.``-rooted imports **under** ``tests/``. That guard covers one half of the class; this
covers the other.

``scripts/`` is not a package and its modules are not installed, so when one script imports
another by bare name (``canary_bridge`` -> ``alert_dedup``) the import resolves only if
``scripts/`` already sits on ``sys.path``. It does under ``python scripts/<x>.py`` — the
script's own directory leads ``sys.path`` — and it does during a FULL test session, because
``tests/scripts/conftest.py`` inserts repo-root ``scripts/`` at collection time and that
process-wide side effect leaks into every module collected afterwards.

It does NOT under a subset run. ``tests/unit/test_reconcile_bridge_canary_class.py`` loads
``scripts/canary_bridge.py`` via ``importlib.util.spec_from_file_location``, which adds no
path entry; run that file alone and the bare import raises ``ModuleNotFoundError: No module
named 'alert_dedup'`` while the full suite stays green. The load happens in a module-scoped
fixture, so the error surfaces at test SETUP rather than at collection — which is why the
dynamic check below runs the module instead of merely collecting it. A test that passes in
CI and fails when run directly trains people to distrust the suite, so this is a standing
guard rather than a one-off repair.

The convention it enforces: any ``scripts/`` module importing an insert-dependent sibling
must first put that directory on ``sys.path``, derived from ``__file__`` so it holds under
every invocation style.
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
    ``ModuleNotFoundError``.
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
    """The reported reproduction PASSES when run entirely on its own (bug 291e).

    Runs the BARE ``pytest`` console script from a cwd outside the repository, so neither
    the repo root nor ``scripts/`` can reach ``sys.path`` by accident — the exact condition
    under which the reported ``ModuleNotFoundError: No module named 'alert_dedup'`` fired.
    This is the dynamic counterpart to the static scan above: the scan pins the convention,
    this pins the observable symptom.

    The module is EXECUTED, not merely collected. The reported failure surfaces when the
    module-scoped fixture loads ``canary_bridge.py`` by path, so ``--collect-only`` never
    reaches it and would report green against the live defect.
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
