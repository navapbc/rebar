"""The pinned pytest keeps a package's conftest fixtures across interleaved path args.

Bug 81da-a1b4-bf29-4ad2 (releasable-cankerous-hammerkop): pytest 9.1.0 and 9.1.1 bind a
conftest's fixtures to the first ``Package`` node collected for its directory. A later
file argument that sits directly in the parent directory re-collects that parent and
creates a second ``Package`` node for the same path, so tests collected through it lose
the conftest's autouse fixtures without any error (pytest-dev/pytest#14635, fixed in
9.2.0). In this repository that dropped ``tests/unit/reconciler/conftest.py``'s
``JIRA_PROJECT=DIG`` pin for ``tests/unit/reconciler/orchestrate`` whenever a run listed a
``tests/unit/reconciler`` path, then ``tests/unit/test_raw_git_write_lint.py``, then the
orchestrate directory, and 22 reconcile tests failed on an empty Jira project.

This test rebuilds that argument shape in a throwaway project and runs the installed
pytest on it, so a lock that resolves to an affected pytest fails here.
"""

from __future__ import annotations

from pathlib import Path

from _nested_pytest import run_nested_pytest
from _subprocess_env import subprocess_env

_CONFTEST = """\
import pytest


@pytest.fixture(autouse=True)
def _package_pin():
    return "pinned"
"""

_NEEDS_PIN = """\
def test_package_autouse_fixture_is_active(request):
    assert "_package_pin" in request.fixturenames
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_project(root: Path) -> None:
    """Mirror ``tests/unit`` (no ``__init__``): a package with a conftest and a subpackage."""
    _write(root / "pytest.ini", "[pytest]\n")
    _write(root / "tests" / "unit" / "test_top.py", "def test_top():\n    pass\n")
    pkg = root / "tests" / "unit" / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "conftest.py", _CONFTEST)
    _write(pkg / "test_first.py", _NEEDS_PIN)
    _write(pkg / "sub" / "__init__.py", "")
    _write(pkg / "sub" / "test_second.py", _NEEDS_PIN)


def test_conftest_fixtures_survive_a_parent_dir_file_arg_between_package_args(
    tmp_path: Path,
) -> None:
    """Leaving the package for a parent-level file and re-entering must keep its fixtures."""
    project = tmp_path / "project"
    _build_project(project)
    env = subprocess_env()
    env.pop("PYTEST_ADDOPTS", None)
    # Only pytest's own collection is under test; third-party plugins stay out.
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    result = run_nested_pytest(
        tmp_path / "run",
        "-q",
        "tests/unit/pkg/test_first.py",
        "tests/unit/test_top.py",
        "tests/unit/pkg/sub",
        env=env,
        timeout=120,
        cwd=project,
    )

    assert result.returncode == 0, (
        "The installed pytest dropped tests/unit/pkg/conftest.py's autouse fixture for "
        "tests collected after a parent-directory file argument (pytest-dev/pytest#14635; "
        f"affects 9.1.0 and 9.1.1).\n{result.stdout}\n{result.stderr}"
    )
    assert "3 passed" in result.stdout, result.stdout
