"""Status preflight warns without aborting on unmapped update status, names it without
assuming its side, and skips the scan when mapping is disabled or action is not update."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "reconciler"
PASS_SUPPORT_PATH = RECON_DIR / "pass_support.py"
CONFIG_PATH = RECON_DIR / "config.py"


@pytest.fixture
def pass_support_mod():
    return importlib.import_module("rebar.reconciler.pass_support")


def test_missing_status_warns_but_does_not_abort(pass_support_mod, capsys):
    """An unmapped update status warns with value and target, then remains available for
    per-mutation handling."""
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
    pass_support_mod.preflight_status_mapping(mutations)
    # The warning must still surface the offending status value and target key.
    err = capsys.readouterr().err
    assert "neither" in err
    assert "DIG-1" in err


def test_unmapped_jira_status_not_mislabeled_as_local(pass_support_mod, capsys):
    """An unmapped Jira workflow value warns without falsely calling it a local status."""
    unmapped = "Selected for Development"
    mutations = [
        {"action": "update", "key": "REB-716", "fields": {"status": unmapped}},
    ]
    # Must NOT raise (non-fatal now); the message is emitted to stderr instead.
    pass_support_mod.preflight_status_mapping(mutations)
    msg = capsys.readouterr().err
    # Still names the offending value + target.
    assert unmapped in msg
    assert "REB-716" in msg
    # Must NOT frame the VALUE as a local status (it is a Jira workflow status here);
    # the specific mislabel "local status '<value>'" is the bug.
    assert f"local status '{unmapped}'" not in msg
    # Must acknowledge the Jira-status side and name the map that lacks the entry.
    assert "Jira" in msg
    assert "local_to_jira_status" in msg


def test_present_status_does_not_raise(pass_support_mod):
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
    pass_support_mod.preflight_status_mapping(mutations)


def test_empty_mapping_kill_switch_does_not_raise(pass_support_mod, monkeypatch):
    """When local_to_jira_status is empty, the preflight is disabled and
    status-touching mutations pass through even with otherwise-unmapped
    values."""
    cfg = importlib.import_module("rebar.reconciler.config")
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
    pass_support_mod.preflight_status_mapping(mutations)


def test_non_update_action_ignored(pass_support_mod):
    """Create and delete mutations are not subject to the status scan."""
    mutations = [
        {"action": "create", "key": "DIG-4", "fields": {"status": "neither"}},
        {"action": "delete", "key": "DIG-5", "fields": {}},
    ]
    # Should not raise.
    pass_support_mod.preflight_status_mapping(mutations)
