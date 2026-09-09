"""Enforce one ``rebar_reconciler`` engine root across the test suite.

A non-editable installation exposes both the checkout engine and a site-packages copy.
Because ``sys.modules`` keys modules by name, the first registration can bind later tests to
a module from the wrong root. Editable installations map both paths to one directory and
therefore hide this failure.

``test_no_test_module_resolves_the_installed_engine_root`` statically rejects test code that
selects the installed copy. The ``_one_engine_root`` autouse fixture in ``tests/conftest.py``
dynamically detects foreign module and package-path registrations, including forms that a
source scan does not recognize. The ``split_engine`` fixture keeps that detector effective in
editable installations. ``tests/_engine_path.py`` defines the canonical checkout root.
"""

from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

import pytest
from _engine_path import engine_dir, engine_root_of, foreign_engine_registrations
from _repo_root import REPO_ROOT
from _tree_scan import parsed_python_files

_TESTS_ROOT = REPO_ROOT / "tests"

# The value expressions that name the INSTALLED engine copy. Held as unparsed source so the
# rule reads the same way it is written in a test module.
_INSTALLED_ENGINE_OWNERS = frozenset({"rebar._engine", "_engine"})

# ``test_engine_dir.py`` tests the resolver without importing from its result or adding it to
# ``sys.path``, so it cannot register ``rebar_reconciler`` from the installed root.
_ALLOWED: dict[str, str] = {
    "tests/unit/test_engine_dir.py": "its subject is the production resolver itself",
}


def _installed_root_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """Every node in ``tree`` that derives an engine root from the INSTALLED package."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "rebar._engine":
            if any(alias.name == "engine_dir" for alias in node.names):
                found.append((node.lineno, "from rebar._engine import engine_dir"))
        elif isinstance(node, ast.Attribute) and node.attr == "engine_dir":
            if ast.unparse(node.value) in _INSTALLED_ENGINE_OWNERS:
                found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            right = node.right
            if isinstance(right, ast.Constant) and right.value == "_engine":
                if "rebar.__file__" in ast.unparse(node.left):
                    found.append((node.lineno, ast.unparse(node)))
    return found


def test_no_test_module_resolves_the_installed_engine_root() -> None:
    """Require checkout-root resolution and ignore resolver names found only in prose."""
    offenders: list[str] = []
    still_needed: set[str] = set()
    for module in parsed_python_files(_TESTS_ROOT):
        relative = module.relative.as_posix()
        sites = _installed_root_sites(module.tree)
        if relative in _ALLOWED:
            if sites:
                still_needed.add(relative)
            continue
        for line, code in sites:
            offenders.append(f"{relative}:{line}: {code}")

    assert still_needed == set(_ALLOWED), (
        "the allowlist has drifted: "
        f"{sorted(set(_ALLOWED) - still_needed)} no longer resolve the installed root, so "
        "their exemption is dead and should be deleted rather than left to cover a future "
        "reintroduction silently"
    )

    assert not offenders, (
        "these test modules resolve the engine from the INSTALLED rebar package:\n"
        + "\n".join(sorted(offenders))
        + "\n\nUnder a non-editable install that is a DIFFERENT copy from the checkout the "
        "tests tree owns, and a canonical `rebar_reconciler.*` sys.modules key can hold only "
        "one of the two — so whichever test runs first decides what every later test sees "
        "(bug bd2d-3e31-31d9-4a66; bug ae96-72a9-8145-4c85 is what it costs). Resolve it with "
        "`tests/_engine_path.py`'s `engine_dir()` instead. Under an editable install both "
        "spellings name the same directory, which is exactly why this has to be checked "
        "statically rather than waited for."
    )


def test_the_conftest_guard_is_wired_to_the_detector() -> None:
    """The dynamic half exists, is autouse, and delegates to the pinned detector."""
    source = (_TESTS_ROOT / "conftest.py").read_text(encoding="utf-8")
    definition = source.find("def _one_engine_root(")
    assert definition != -1, (
        "tests/conftest.py no longer defines the `_one_engine_root` guard; the static scan "
        "above cannot see a registration that reaches a foreign root by a spelling it does "
        "not know, so the state guard must stay"
    )
    assert "@pytest.fixture(autouse=True)" in source[:definition][-200:], (
        "`_one_engine_root` is no longer autouse, so it no longer runs for the ~146 sites "
        "that never name it"
    )
    assert "foreign_engine_registrations" in source, (
        "the conftest guard no longer calls the detector this file pins"
    )


@pytest.fixture
def split_engine(tmp_path: Path) -> Path:
    """Create a distinct engine root so the detector runs in editable installations."""
    other = tmp_path / "site-packages" / "rebar" / "_engine"
    other.parent.mkdir(parents=True)
    # Skip __pycache__: megabytes of bytecode this test never executes.
    shutil.copytree(engine_dir(), other, ignore=shutil.ignore_patterns("__pycache__"))
    assert (other / "rebar_reconciler" / "runtime.py").is_file()
    return other


def test_detector_names_a_key_registered_from_a_foreign_engine_root(split_engine: Path) -> None:
    foreign = split_engine / "rebar_reconciler" / "runtime.py"
    probe = type(sys)("rebar_reconciler.runtime")
    probe.__file__ = str(foreign)

    offenders = foreign_engine_registrations({"rebar_reconciler.runtime": probe})

    assert offenders == [("rebar_reconciler.runtime", str(foreign.resolve()))]


def test_detector_names_a_foreign_entry_on_the_package_search_path(split_engine: Path) -> None:
    """A foreign ``__path__`` entry is the same defect one step earlier: every later
    ``import rebar_reconciler.<sub>`` resolves out of the wrong copy."""
    foreign_pkg = split_engine / "rebar_reconciler"
    probe = type(sys)("rebar_reconciler")
    probe.__path__ = [str(engine_dir() / "rebar_reconciler"), str(foreign_pkg)]

    offenders = foreign_engine_registrations({"rebar_reconciler": probe})

    assert offenders == [("rebar_reconciler.__path__", str(foreign_pkg.resolve()))]


def test_detector_accepts_the_canonical_root_and_the_test_shadow_package() -> None:
    """The rule constrains ENGINE files only — the ``tests/unit/rebar_reconciler`` shadow
    package legitimately holds the top-level key and must never be reported."""
    canonical = type(sys)("rebar_reconciler.runtime")
    canonical.__file__ = str(engine_dir() / "rebar_reconciler" / "runtime.py")
    shadow = type(sys)("rebar_reconciler")
    shadow.__file__ = str(_TESTS_ROOT / "unit" / "rebar_reconciler" / "__init__.py")
    shadow.__path__ = [str(_TESTS_ROOT / "unit" / "rebar_reconciler")]

    assert (
        foreign_engine_registrations(
            {"rebar_reconciler": shadow, "rebar_reconciler.runtime": canonical}
        )
        == []
    )


def test_engine_root_of_ignores_paths_outside_any_engine_tree() -> None:
    assert engine_root_of(_TESTS_ROOT / "conftest.py") is None
    assert engine_root_of(engine_dir() / "rebar_reconciler" / "runtime.py") == engine_dir()
