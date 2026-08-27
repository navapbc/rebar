"""Regression: the ``rebar_reconciler`` shadow bridge must survive an engine-first seed.

``tests/unit/conftest.py`` bridges the collision between the engine package
``rebar_reconciler`` (which ships a ``classify.py`` MODULE) and the test tree
``tests/unit/rebar_reconciler/`` (whose ``classify/`` is a PACKAGE sharing the name).
Collecting a ``rebar_reconciler.classify.test_*`` module needs ``rebar_reconciler.classify``
to resolve to the test PACKAGE; if the engine ``classify.py`` module wins that name the
collection dies with ``'rebar_reconciler.classify' is not a package``.

Bug b900-3cc1: when another test directory seeds the ENGINE package first (its
``__path__`` is ``[engine_dir]``), the bridge appended the shadow dir AFTER the engine
dir, so ``rebar_reconciler.classify`` resolved to the engine MODULE — a non-deterministic
xdist-ordering failure that surfaced on CI. The bridge must order the shadow dir BEFORE
the engine dir unconditionally, so the test package always wins.

This asserts the OBSERVABLE contract (the resolved kind of ``rebar_reconciler.classify``
under an adverse seed), not the bridge's internals. It snapshots and restores every
``rebar_reconciler*`` ``sys.modules`` entry so it cannot perturb sibling tests.
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
