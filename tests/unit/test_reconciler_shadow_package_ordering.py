"""Keep the test shadow package ahead of an engine-first import seed.

The engine provides a ``rebar_reconciler.classify`` module while the test tree
provides a package with that name. Collection requires the test package to win
even when another directory imports the engine package first. The test asserts
the resolved object kind and restores every affected ``sys.modules`` entry so
siblings retain their original import state.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

from rebar._lib_ops import _engine_module

pytestmark = pytest.mark.unit

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _load_bridge():
    """Load ``_bridge_reconciler_shadow_package`` from the unit conftest by path."""
    spec = importlib.util.spec_from_file_location("_unit_conftest_bridge_probe", _CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._bridge_reconciler_shadow_package


def _reconciler_keys() -> list[str]:
    return [k for k in sys.modules if k == "rebar_reconciler" or k.startswith("rebar_reconciler.")]


def test_shadow_bridge_resolves_classify_as_package_under_engine_first_seed() -> None:
    saved = {k: sys.modules[k] for k in _reconciler_keys()}
    try:
        # Force the adverse condition: a clean ENGINE-first seed (path == [engine_dir]),
        # exactly what another test directory importing the engine produces before the
        # unit-tree bridge runs.
        for k in _reconciler_keys():
            del sys.modules[k]
        _engine_module("rebar_reconciler.access_check")
        pkg = sys.modules["rebar_reconciler"]
        # Precondition of the bug: only the engine dir is on the path so far.
        assert all("tests" not in p for p in pkg.__path__)

        _load_bridge()()

        # The bridged package must resolve the classify NAME to the test PACKAGE (which
        # carries a ``__path__``), never the engine ``classify.py`` module.
        classify = importlib.import_module("rebar_reconciler.classify")
        assert hasattr(classify, "__path__"), (
            "rebar_reconciler.classify must resolve to the test PACKAGE, not the engine "
            "classify.py module"
        )
        # And the engine's own modules still resolve (the fix must not sever engine imports).
        assert importlib.import_module("rebar_reconciler.config") is not None
    finally:
        for k in _reconciler_keys():
            del sys.modules[k]
        sys.modules.update(saved)
