"""RED tests for preflight_status_mapping dict-status normalization.

Historical bug (bug 85a1-f581-2252-4a21): the outbound apply pipeline runs
``preflight_status_mapping`` over all mutations. Outbound mutations carry
the local status STRING ("open", "in_progress", ...); inbound (and any
mutation whose ``fields`` were taken verbatim from a Jira snapshot) carry
Jira's raw REST status DICT (``{"name": "To Do", "id": ..., ...}``).

Before the fix, ``status not in mapping`` raised ``TypeError: unhashable
type: 'dict'`` on the dict-shaped status — the probe Phase 3+ reconciler
crashed with ``cannot use 'dict' as a dict key`` and the full inbound +
outbound pipeline aborted.

The fix normalizes ``raw_status`` to a string via ``.get("name")`` when it
is a dict, so the preflight lookup is shape-tolerant.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PASS_SUPPORT_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "pass_support.py"


def _load_pass_support():
    spec = importlib.util.spec_from_file_location(
        "pass_support_status_preflight_test", PASS_SUPPORT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pass_support_status_preflight_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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
    """Inbound mutations carry Jira status names — preflight is for outbound only.

    The probe Phase 3 fed inbound mutations through preflight and produced
    spurious ``local status 'To Do' not in local_to_jira_status mapping``
    errors (Jira names are mapping VALUES, not keys). Inbound must skip.
    """
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
    """Outbound differ pre-maps local→Jira; the resulting Jira name must pass.

    ``outbound_differ._map_local_to_jira_fields`` translates local status
    (e.g. "in_progress") to the Jira name ("In Progress") BEFORE emitting
    the mutation. The preflight then sees the VALUE side of the mapping,
    not the KEY side — both shapes must be accepted.
    """
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
    """A status that is neither a key nor a value in the mapping is unmapped — it
    is WARNED (naming the target), not raised (Facet 3: preflight is non-fatal so
    the mutation flows to the applier and is recorded as a per-mutation failure)."""
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
