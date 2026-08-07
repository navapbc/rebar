"""Pytest configuration for unit tests.

Adds the engine directory (``src/rebar/_engine``) to ``sys.path`` so engine unit
tests can import the bundled helpers by their on-disk names without each test
file manipulating ``sys.path`` itself. After the ``fare-rant-clasp`` repackage the
old top-level names (``ticket_reducer`` / ``ticket_graph`` / ``ticket_reads`` …)
resolve here to thin compat shims that re-export the real ``rebar.*`` subpackages,
so these imports keep working while exercising the same code the library loads.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "src" / "rebar" / "_engine")

# ── Bridge the ``rebar_reconciler`` shadow package ────────────────────────────
# Under pytest's default ``prepend`` import mode, collecting ANY module under
# ``tests/unit/`` puts ``tests/unit`` at ``sys.path[0]`` (it is the first ancestor
# without an ``__init__.py``). ``tests/unit/rebar_reconciler/__init__.py`` then
# makes THAT directory an importable top-level package named ``rebar_reconciler``,
# shadowing the engine package of the same name for the whole session.
#
# Engine modules exec'd standalone via ``spec_from_file_location`` (the loader
# convention across the reconciler test tree) carry no package context, so their
# module-level ``from rebar_reconciler.<sub> import ...`` statements resolve
# through that shadow and fail with ModuleNotFoundError.
#
# ``tests/unit/rebar_reconciler/conftest.py`` already compensates by appending the
# engine package dir to the shadow's ``__path__`` — but it is installed one
# directory too deep, so it only fires when ``tests/unit/rebar_reconciler/**`` is
# itself collected. The shadow, however, is created by ANY ``tests/unit/**``
# collection. Hoist the compensation here so it runs whenever the shadow can exist.
#
# Ordering is load-bearing, twice over:
#   * This must run BEFORE ``_SCRIPTS_DIR`` goes on ``sys.path``. Otherwise the
#     engine dir is ``sys.path[0]`` and a plain ``import rebar_reconciler`` binds
#     the ENGINE package into ``sys.modules`` first; the shadow is then never
#     created, the ``__path__`` extension is a no-op, and pytest's later import of
#     ``rebar_reconciler.conftest`` fails outright.
#   * We therefore never rely on ``sys.path`` at all: the shadow package is seeded
#     explicitly from its own ``__init__.py`` with ``submodule_search_locations``,
#     which is deterministic regardless of what ``sys.path[0]`` happens to be.
#
# The engine dir is appended (not prepended), so the shadow's own modules — and
# any already-seeded flat ``rebar_reconciler.<name>`` ``sys.modules`` keys — keep
# winning, preserving their object identity.
_ENGINE_PKG_DIR = Path(_SCRIPTS_DIR) / "rebar_reconciler"
_SHADOW_PKG_DIR = Path(__file__).resolve().parent / "rebar_reconciler"


def _bridge_reconciler_shadow_package() -> None:
    pkg = sys.modules.get("rebar_reconciler")
    if pkg is None:
        init = _SHADOW_PKG_DIR / "__init__.py"
        if not init.is_file():  # pragma: no cover - shadow dir removed
            return
        spec = importlib.util.spec_from_file_location(
            "rebar_reconciler",
            init,
            submodule_search_locations=[str(_SHADOW_PKG_DIR)],
        )
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["rebar_reconciler"] = pkg
        try:
            spec.loader.exec_module(pkg)
        except BaseException:  # pragma: no cover - defensive
            del sys.modules["rebar_reconciler"]
            raise
    for extra in (str(_SHADOW_PKG_DIR), str(_ENGINE_PKG_DIR)):
        if extra not in pkg.__path__:
            pkg.__path__.append(extra)


_bridge_reconciler_shadow_package()

if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


@pytest.fixture(autouse=True)
def _no_real_session_log_writes(monkeypatch):
    """Unit tests must never perform a REAL session-log store write.

    ``rebar.append_session_log`` commits a ``session_log`` ticket to the shared
    ``tickets`` branch AND writes the ``.rebar/current_session_log`` pointer into the
    repo root. Several best-effort telemetry paths reach it WITHOUT the caller opting
    in — notably the degraded-gate verdicts
    (``llm.workflow.gate_dispatch._degraded_plan_review_verdict`` /
    ``_degraded_code_review_verdict`` -> ``llm.failure.log_degrade`` ->
    ``append_session_log``). So a unit test that merely exercises a degraded verdict
    silently pollutes the shared store and, in a fresh worktree, trips the
    ``_no_repo_root_leaks`` guard on the leaked ``.rebar`` pointer (bug d9aa,
    misty-creatable-mallard).

    Neutralize the write seam for the WHOLE unit tier so no unit test — present or
    future — can leak through any degrade path. This is test-only (production code is
    untouched) and mirrors the tier's existing "never touch the real store" contract.
    A test that specifically exercises the real helper re-monkeypatches it in its body
    (a function-scoped ``monkeypatch.setattr`` applied after this fixture wins), e.g.
    ``test_log_degrade_never_raises``.
    """
    import rebar

    def _noop_append_session_log(*_args, **_kwargs):
        return {"id": None, "alias": None, "created": False}

    monkeypatch.setattr(rebar, "append_session_log", _noop_append_session_log)
