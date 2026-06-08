"""Shared pytest fixtures for dso_reconciler unit tests.

Divergence scenarios for all 3 resolution classes in conflict_resolver.py.

Test-loading convention
-----------------------
Tests in this directory load modules under test via
``importlib.util.spec_from_file_location`` rather than ordinary ``import``
statements. This is the established pattern across the wider reconciler test
tree (see ``tests/scripts/test_pre_cutover.py``,
``tests/scripts/test_capability_check.py``,
``tests/scripts/test_forward_compat_probe.py``,
``tests/scripts/test_cursor_snapshot.py`` — all already on main).

Rationale:

* It works for module files whose path contains hyphens (e.g.
  ``acli-integration.py``), which Python's import system cannot resolve as a
  regular module name.
* It avoids implicit ``sys.path`` requirements — no conftest-level path
  manipulation is needed for tests to find the modules under test.
* It keeps each test self-contained: the exact file under test is named at
  the call site, so a moved or renamed module surfaces a clear loader error
  rather than a confusing ``ImportError``.

Rewriting these tests to use idiomatic ``import`` would diverge from the
established convention across the test tree; new tests in this directory
should follow the same loader pattern.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# ── Engine path + canonical dotted-key seeding ────────────────────────────────
# The engine ships at <repo>/src/rebar/_engine. Put it on sys.path so the
# stdlib-only reconciler packages import directly, and pre-seed the canonical
# ``plugins.dso.scripts.dso_reconciler`` sys.modules keys the reconciler source
# uses (cosmetic keys loaded via spec_from_file_location) so the few tests that
# do ``from plugins.dso.scripts.dso_reconciler import alert_store`` resolve at
# collection time.
_ENGINE_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))


def _seed_dotted_namespace() -> None:
    import dso_reconciler  # real package (engine dir on sys.path)

    for name in ("plugins", "plugins.dso", "plugins.dso.scripts"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = []  # mark as namespace package
            sys.modules[name] = mod
    sys.modules["plugins.dso.scripts.dso_reconciler"] = dso_reconciler
    sys.modules["plugins.dso.scripts"].dso_reconciler = dso_reconciler  # type: ignore[attr-defined]
    sys.modules["plugins.dso"].scripts = sys.modules["plugins.dso.scripts"]  # type: ignore[attr-defined]
    sys.modules["plugins"].dso = sys.modules["plugins.dso"]  # type: ignore[attr-defined]

    key = "plugins.dso.scripts.dso_reconciler.alert_store"
    if key not in sys.modules:
        asp = _ENGINE_DIR / "dso_reconciler" / "alert_store.py"
        spec = importlib.util.spec_from_file_location(key, asp)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    dso_reconciler.alert_store = sys.modules[key]  # type: ignore[attr-defined]


_seed_dotted_namespace()


@pytest.fixture(autouse=True)
def _sandbox_repo_root(tmp_path, monkeypatch):
    """Redirect the reconciler's repo-root fallback to a per-test temp dir.

    Without REBAR_ROOT set, the source falls back to ``Path(__file__).parents[4]``
    which resolves to the rebar repo root — so any test that exercises a real
    dispatch/CLI path would create ``.tickets-tracker`` in the working tree and
    trip the repo-root leak guard. Tests that pass an explicit ``repo_root``
    argument are unaffected (the source prefers the argument over the env).
    """
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    yield


@pytest.fixture
def state_divergence():
    """State-class divergence: local 'In Progress', remote 'Done'."""
    return {"field": "status", "local": "In Progress", "remote": "Done"}


@pytest.fixture
def additive_divergence():
    """Additive-class divergence: local description A, remote description B."""
    return {
        "field": "description",
        "local": "Local description content A",
        "remote": "Remote description content B",
    }


@pytest.fixture
def set_divergence():
    """Set-class divergence: local {X,Y}, remote {Y,Z}."""
    return {
        "field": "labels",
        "local": ["X", "Y"],
        "remote": ["Y", "Z"],
        "expected_union": {"X", "Y", "Z"},
    }


@pytest.fixture
def paginating_acli_stub():
    """Return a factory that produces a callable simulating ACLI paginated JQL fetch.

    The factory accepts:
      - pages: a list of issue-dicts (the full working set, in canonical order)
      - max_results_cap: integer; the ACLI behaviour at boundaries (default 100)

    Returns a callable stub(jql, start_at, max_results) -> dict with shape:
        {
          "issues": [...],          # the slice of `pages` between start_at and start_at+max_results
          "startAt": start_at,
          "maxResults": effective_max,  # min(max_results, max_results_cap)
          "total": len(pages),
        }
    Slicing follows real ACLI: start_at out-of-range returns empty issues list with total=len(pages).
    """
    def _factory(pages, max_results_cap=100):
        def _stub(jql, start_at=0, max_results=100):
            effective_max = min(max_results, max_results_cap)
            slice_ = pages[start_at : start_at + effective_max]
            return {
                "issues": slice_,
                "startAt": start_at,
                "maxResults": effective_max,
                "total": len(pages),
            }
        return _stub
    return _factory
