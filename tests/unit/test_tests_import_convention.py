"""Guard for bug a371: no test module may use a ``tests.``-rooted absolute import.

Neither ``tests/__init__.py`` nor any ``tests/*/__init__.py`` exists, so a ``tests.``-rooted
import can only resolve as a PEP-420 namespace package -- which needs the **repository root**
on ``sys.path``. Nothing in the harness puts it there: pytest's prepend import mode inserts
only the first ancestor without an ``__init__.py`` (e.g. ``tests/unit``), and
``tests/conftest.py`` deliberately inserts only ``tests/``. The repo root arrives solely as a
side effect of ``python -m pytest`` inserting the cwd, so such an import works under
``python -m pytest`` and dies at collection under the BARE ``pytest`` console script -- which
is the project's canonical invocation (``Makefile`` ``test`` target;
``.github/workflows/_build-and-test.yml``).

The failure is invisible in the common ``python -m pytest`` invocation, and on the two
audit-page modules it was further masked by ``pytest.importorskip`` calls sitting *above* the
broken import. Hence a standing guard rather than a one-off repair.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"


def _tests_rooted_imports(tree: ast.AST, rel: Path) -> list[str]:
    """Return ``"<file>:<line>: <module>"`` for every ``tests``-rooted import in *tree*.

    AST-based (not textual) so prose in docstrings and comments cannot produce a false hit.
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tests" or alias.name.startswith("tests."):
                    hits.append(f"{rel}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is an explicit relative import, which is a different mechanism.
            if (
                node.level == 0
                and node.module
                and (node.module == "tests" or node.module.startswith("tests."))
            ):
                hits.append(f"{rel}:{node.lineno}: from {node.module} import ...")
    return hits


@pytest.mark.repo_policy
def test_no_tests_rooted_imports_anywhere_under_tests() -> None:
    """No module under ``tests/`` imports through a ``tests.``-rooted absolute path.

    Same-directory helpers are imported by bare name (pytest's prepend mode puts the test's
    own directory on ``sys.path``); cross-directory helpers live directly under ``tests/``,
    which ``tests/conftest.py`` guarantees is importable.
    """
    offenders: list[str] = []
    for module in parsed_python_files(_TESTS_DIR):
        offenders.extend(_tests_rooted_imports(module.tree, module.relative))

    assert offenders == [], (
        "`tests.`-rooted imports resolve only when the repository root is on sys.path, "
        "which the bare `pytest` console script (the invocation used by `make test` and CI) "
        "does not provide. Import same-directory helpers by bare name instead:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "module",
    ["test_audit_ticket_page.py", "test_audit_ticket_page_heldout.py"],
)
def test_audit_page_modules_collect_under_bare_pytest(module: str, tmp_path: Path) -> None:
    """The audit-page modules collect under the BARE ``pytest`` console script (bug a371).

    Run from a cwd outside the repository so the repo root cannot reach ``sys.path`` by
    accident -- the exact condition under which the reported ``ModuleNotFoundError: No
    module named 'tests'`` fired.
    """
    pytest.importorskip("fastapi")  # the [ui] extra; absent from the lean CI suite
    pytest.importorskip("httpx")  # starlette TestClient's HTTP backend

    console_script = Path(sys.executable).parent / "pytest"
    if not console_script.exists():  # pragma: no cover - environment without the script
        pytest.skip("no `pytest` console script next to the running interpreter")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [
            str(console_script),
            str(_TESTS_DIR / "unit" / module),
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, (
        f"bare `pytest --collect-only` failed for {module}:\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "ModuleNotFoundError" not in proc.stdout + proc.stderr
