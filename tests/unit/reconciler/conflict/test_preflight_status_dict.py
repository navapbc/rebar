"""Status preflight normalizes Jira dict values, skips inbound mutations, accepts both local
mapping keys and Jira-side values, and warns nonfatally for genuinely unmapped statuses."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PASS_SUPPORT_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "pass_support.py"


def _load_pass_support():
    return importlib.import_module("rebar.reconciler.pass_support")


@pytest.fixture(scope="module")
def pass_support():
    if not PASS_SUPPORT_PATH.exists():
        pytest.fail(f"pass_support.py not found at {PASS_SUPPORT_PATH}")
    return _load_pass_support()


def _patch_mapping(pass_support, monkeypatch, mapping):
    """Patch the config module that preflight loads via _load."""
    cfg = pass_support._load("reconcile_config", "config.py")
    monkeypatch.setattr(cfg, "local_to_jira_status", mapping, raising=False)


def test_preflight_accepts_dict_status_with_known_name(pass_support, monkeypatch):
    """When mutation.fields.status is a Jira dict whose .name is in mapping, no raise."""
    _patch_mapping(pass_support, monkeypatch, {"To Do": "To Do", "In Progress": "In Progress"})
    mutations = [
        {
            "action": "update",
            "key": "DIG-1",
            "fields": {"status": {"name": "To Do", "id": "21057"}},
        }
    ]
    pass_support.preflight_status_mapping(mutations)


def test_preflight_does_not_crash_on_dict_status(pass_support, monkeypatch, capsys):
    """When the .name isn't in mapping, the dict is normalized (no TypeError) and
    the unmapped status is WARNED, not raised (Facet 3: preflight is non-fatal)."""
    _patch_mapping(pass_support, monkeypatch, {"To Do": "To Do"})
    mutations = [
        {
            "action": "update",
            "key": "DIG-1",
            "fields": {"status": {"name": "Bogus Status", "id": "999"}},
        }
    ]
    # Must NOT raise (neither StatusMappingError nor TypeError) — dict-status is
    # normalized to its .name and the unmapped value is warned, not aborted.
    pass_support.preflight_status_mapping(mutations)
    assert "Bogus Status" in capsys.readouterr().err


def test_preflight_string_status_unchanged_behavior(pass_support, monkeypatch):
    """String status (outbound shape) still works."""
    _patch_mapping(pass_support, monkeypatch, {"open": "To Do", "in_progress": "In Progress"})
    mutations = [
        {
            "action": "update",
            "key": "DIG-2",
            "fields": {"status": "open"},
        }
    ]
    pass_support.preflight_status_mapping(mutations)


def test_preflight_skips_dict_without_name(pass_support, monkeypatch):
    """Dict without name normalizes to '' which is falsy — skip the check."""
    _patch_mapping(pass_support, monkeypatch, {"To Do": "To Do"})
    mutations = [
        {
            "action": "update",
            "key": "DIG-3",
            "fields": {"status": {"id": "999"}},  # no name key
        }
    ]
    pass_support.preflight_status_mapping(mutations)


def test_preflight_skips_inbound_mutations(pass_support, monkeypatch):
    """Inbound mutations carry Jira-side status names and must bypass outbound mapping
    preflight."""
    _patch_mapping(pass_support, monkeypatch, {"open": "To Do", "in_progress": "In Progress"})
    mutations = [
        {
            "action": "update",
            "direction": "inbound",
            "key": "DIG-4",
            "fields": {"status": "To Do"},  # Jira-side status, would fail key lookup
        }
    ]
    # Must NOT raise — direction='inbound' skips this entry.
    pass_support.preflight_status_mapping(mutations)


def test_preflight_skips_inbound_with_dict_status(pass_support, monkeypatch):
    """Inbound with raw Jira dict-status also skipped."""
    _patch_mapping(pass_support, monkeypatch, {"open": "To Do"})
    mutations = [
        {
            "action": "update",
            "direction": "inbound",
            "key": "DIG-5",
            "fields": {"status": {"name": "Bogus", "id": "999"}},
        }
    ]
    pass_support.preflight_status_mapping(mutations)


def test_preflight_accepts_outbound_jira_side_status_name(pass_support, monkeypatch):
    """An outbound differ may already map local status to a Jira-side value; preflight
    accepts that value."""
    _patch_mapping(pass_support, monkeypatch, {"open": "To Do", "in_progress": "In Progress"})
    mutations = [
        {
            "action": "update",
            "direction": "outbound",
            "key": "DIG-6",
            "fields": {"status": "In Progress"},  # Jira-side name (post-mapping)
        }
    ]
    pass_support.preflight_status_mapping(mutations)


def test_preflight_warns_on_truly_unmapped_status(pass_support, monkeypatch, capsys):
    """A status in neither mapping keys nor values warns with its target but does not abort
    the batch."""
    _patch_mapping(pass_support, monkeypatch, {"open": "To Do", "in_progress": "In Progress"})
    mutations = [
        {
            "action": "update",
            "direction": "outbound",
            "key": "DIG-7",
            "fields": {"status": "Nonsense"},
        }
    ]
    # Must NOT raise — the scan is now non-fatal.
    pass_support.preflight_status_mapping(mutations)
    err = capsys.readouterr().err
    assert "Nonsense" in err
    assert "DIG-7" in err
