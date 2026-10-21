"""Exercise the alert-store loader without a ``plugins`` namespace.

The lazy load must expose ``append`` and cache one module at
``rebar.reconciler.alert_store``.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
FETCHER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "fetcher.py"
_CANONICAL_KEY = "rebar.reconciler.alert_store"


@pytest.fixture
def fetcher_mod():
    """The shared ``rebar.reconciler.fetcher`` module, with `plugins.*` stubs hidden
    so the test environment mirrors production CI.
    """
    plugins_keys = [k for k in list(sys.modules.keys()) if k.startswith("plugins")]
    saved = {k: sys.modules.pop(k) for k in plugins_keys}
    try:
        yield importlib.import_module("rebar.reconciler.fetcher")
    finally:
        for k, v in saved.items():
            sys.modules[k] = v


def test_load_alert_store_registers_under_canonical_key(fetcher_mod):
    """Cache one module at the canonical key shared by every loader."""
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
