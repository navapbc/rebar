"""Tests for reconcile.preflight_status_mapping.

Verifies the preflight status-mapping scan raises ``StatusMappingError`` for
any update mutation whose status field is absent from
``config.local_to_jira_status``, and that the scan runs before the applier
dispatches anything. An empty mapping acts as a kill-switch.

Follows the module-loading convention documented in
``tests/unit/rebar_reconciler/conftest.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
RECONCILE_PATH = RECON_DIR / "reconcile.py"
CONFIG_PATH = RECON_DIR / "config.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def reconcile_mod():
    # Pre-register config under the name reconcile._load() uses so the
    # preflight function picks up the live config module rather than
    # re-loading a fresh copy that would shadow monkeypatches.
    _load_module("reconcile_config", CONFIG_PATH)
    return _load_module("reconcile_under_test", RECONCILE_PATH)


def test_missing_status_raises_before_applier(reconcile_mod):
    """An update mutation with an unmapped status raises StatusMappingError
    before any mutations are applied (preflight is a pure scan that raises
    on the first offending mutation)."""
    mutations = [
        {
            "action": "update",
            "key": "DIG-1",
            "local_id": "abc-123",
            "fields": {"status": "neither"},
        },
        {
            "action": "update",
            "key": "DIG-2",
            "local_id": "def-456",
            "fields": {"status": "open"},
        },
    ]
    with pytest.raises(reconcile_mod.StatusMappingError) as exc:
        reconcile_mod.preflight_status_mapping(mutations)
    # Error must mention the offending status value and target key.
    assert "neither" in str(exc.value)
    assert "DIG-1" in str(exc.value)


def test_present_status_does_not_raise(reconcile_mod):
    """An update mutation whose status is in local_to_jira_status passes
    cleanly through the preflight scan."""
    mutations = [
        {
            "action": "update",
            "key": "DIG-2",
            "local_id": "def-456",
            "fields": {"status": "open"},
        }
    ]
    # Should not raise.
    reconcile_mod.preflight_status_mapping(mutations)


def test_empty_mapping_kill_switch_does_not_raise(reconcile_mod, monkeypatch):
    """When local_to_jira_status is empty, the preflight is disabled and
    status-touching mutations pass through even with otherwise-unmapped
    values."""
    cfg = sys.modules["reconcile_config"]
    monkeypatch.setattr(cfg, "local_to_jira_status", {})
    mutations = [
        {
            "action": "update",
            "key": "DIG-3",
            "local_id": "ghi-789",
            "fields": {"status": "any-unmapped-value"},
        }
    ]
    # Should not raise — kill-switch engaged.
    reconcile_mod.preflight_status_mapping(mutations)


def test_non_update_action_ignored(reconcile_mod):
    """Create and delete mutations are not subject to the status scan."""
    mutations = [
        {"action": "create", "key": "DIG-4", "fields": {"status": "neither"}},
        {"action": "delete", "key": "DIG-5", "fields": {}},
    ]
    # Should not raise.
    reconcile_mod.preflight_status_mapping(mutations)
