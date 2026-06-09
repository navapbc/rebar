"""Regression tests for bug ec9a-be6b-f50a-47b4 — fetcher's alert_store lazy load.

Pre-fix: fetcher.py:160 used `from rebar_reconciler import alert_store`,
which only resolves when `plugins` is importable as a Python package. Production
CI does not pre-seed the namespace; the import raised `ModuleNotFoundError`
inside fetch_snapshot's dedup-alert path.

These tests exercise the production helper `fetcher._load_alert_store()` directly
in an environment with NO `plugins.*` namespace stubs, verifying:
  1. Successful load returns a module exposing the .append API.
  2. Repeat calls return the SAME module object (sys.modules cache hit).
  3. exec_module failure does NOT leave a partially-initialised module in
     sys.modules under the canonical key.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FETCHER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "fetcher.py"
)
_CANONICAL_KEY = "rebar_reconciler.alert_store"


@pytest.fixture
def fetcher_mod():
    """Load fetcher.py freshly under the canonical dotted key so production
    code and tests see the same module object. Clears `plugins.*` stubs so
    the test environment mirrors production CI.
    """
    plugins_keys = [k for k in list(sys.modules.keys()) if k.startswith("plugins")]
    saved = {k: sys.modules.pop(k) for k in plugins_keys}
    sys.modules.pop(_CANONICAL_KEY, None)
    try:
        spec = importlib.util.spec_from_file_location(
            "rebar_reconciler.fetcher_ec9a_test", FETCHER_PATH
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(_CANONICAL_KEY, None)
        sys.modules.pop("rebar_reconciler.fetcher_ec9a_test", None)
        for k, v in saved.items():
            sys.modules[k] = v


def test_load_alert_store_registers_under_canonical_key(fetcher_mod):
    """_load_alert_store must register alert_store under the canonical
    `rebar_reconciler.alert_store` key — same key used by
    __main__'s _ADVISORY_LOCK_KEY and applier's _MUTATION_KEY conventions.
    Tests that patch this key (test_fetcher_dedup_observable.py) depend on
    a single module object shared across loaders.
    """
    assert fetcher_mod._ALERT_STORE_KEY == _CANONICAL_KEY, (
        f"Canonical key drift: fetcher uses {fetcher_mod._ALERT_STORE_KEY!r}, "
        f"convention is {_CANONICAL_KEY!r}"
    )
    mod = fetcher_mod._load_alert_store()
    assert sys.modules.get(_CANONICAL_KEY) is mod, (
        "alert_store loaded but not cached under canonical key"
    )
    assert hasattr(mod, "append"), (
        f"alert_store loaded but missing .append API "
        f"(public attrs: {sorted(a for a in dir(mod) if not a.startswith('_'))})"
    )


def test_load_alert_store_is_idempotent(fetcher_mod):
    """Repeat calls must return the SAME module object (sys.modules cache hit)
    so consumers do not see divergent class identities across calls.
    """
    first = fetcher_mod._load_alert_store()
    second = fetcher_mod._load_alert_store()
    assert first is second, (
        "_load_alert_store returned a different module on second call — "
        "would create dual-class identity (Cluster A pattern)"
    )


def test_load_alert_store_does_not_leave_partial_module_on_exec_failure(fetcher_mod):
    """If exec_module raises during load (e.g. SyntaxError in alert_store.py),
    sys.modules must NOT retain a partially-initialised module under the
    canonical key — a subsequent call must be able to retry cleanly.
    """
    # Capture a real spec by calling the unpatched function first, then
    # swap its loader. The patched spec_from_file_location simply returns
    # this prepared spec — no recursion into the wrapped original.
    real_spec = importlib.util.spec_from_file_location(
        _CANONICAL_KEY, FETCHER_PATH.parent / "alert_store.py"
    )
    assert real_spec is not None

    class _FailingLoader:
        def create_module(self, spec):
            return None

        def exec_module(self, mod):
            raise RuntimeError("intentional test exec_module failure")

    real_spec.loader = _FailingLoader()

    with patch.object(
        fetcher_mod.importlib.util,
        "spec_from_file_location",
        return_value=real_spec,
    ):
        with pytest.raises(RuntimeError, match="intentional test exec_module failure"):
            fetcher_mod._load_alert_store()

    assert _CANONICAL_KEY not in sys.modules, (
        "Partial module remained in sys.modules after exec_module failure — "
        "subsequent calls would reuse the broken module"
    )
