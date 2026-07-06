"""Tests for reconcile.preflight_status_mapping.

Verifies the preflight status-mapping scan WARNS (non-fatally) for any update
mutation whose status field is absent from ``config.local_to_jira_status``,
without aborting the pass (Facet 3, reconciler-abort-isolation: the scan used
to raise ``StatusMappingError`` and abort every later mutation; it now logs to
stderr and returns so the offending mutation flows to the applier and is
recorded there as a per-mutation failure). An empty mapping acts as a
kill-switch.

Follows the module-loading convention documented in
``tests/unit/rebar_reconciler/conftest.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
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


def test_missing_status_warns_but_does_not_abort(reconcile_mod, capsys):
    """An update mutation with an unmapped status WARNS to stderr but does NOT
    raise (Facet 3): the preflight no longer aborts the pass, so the offending
    mutation can flow to the applier and be recorded as a per-mutation failure.
    The warning must still name the offending status value and target key."""
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
    # Must NOT raise — the scan is now non-fatal.
    reconcile_mod.preflight_status_mapping(mutations)
    # The warning must still surface the offending status value and target key.
    err = capsys.readouterr().err
    assert "neither" in err
    assert "DIG-1" in err


def test_unmapped_jira_status_not_mislabeled_as_local(reconcile_mod, capsys):
    """An unmapped JIRA workflow status (e.g. ``IDEA`` added Jira-side) must not be
    reported as a "local status" (bug c672). The preflight accepts both local-status
    keys and Jira-status values and fails only when the value is in neither, so the
    message must describe an unmapped status of indeterminate side and acknowledge the
    Jira-status possibility — otherwise it misdirects debugging toward a non-existent
    local ticket.

    Facet 3 (reconciler-abort-isolation): the preflight now WARNS non-fatally instead
    of raising, so the message is asserted against stderr rather than an exception. The
    bug-c672 intent (do not mislabel a Jira status as local) is unchanged."""
    mutations = [
        {"action": "update", "key": "REB-716", "fields": {"status": "IDEA"}},
    ]
    # Must NOT raise (non-fatal now); the message is emitted to stderr instead.
    reconcile_mod.preflight_status_mapping(mutations)
    msg = capsys.readouterr().err
    # Still names the offending value + target.
    assert "IDEA" in msg
    assert "REB-716" in msg
    # Must NOT frame the VALUE as a local status (it is a Jira workflow status here);
    # the specific mislabel "local status 'IDEA'" is the bug.
    assert "local status 'IDEA'" not in msg
    # Must acknowledge the Jira-status side and name the map that lacks the entry.
    assert "Jira" in msg
    assert "local_to_jira_status" in msg


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
