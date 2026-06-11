"""Unit tests for rebar_reconciler/config.py — EXCLUDED_FIELDS constant and
local_to_jira_status mapping.

Tests cover:
  - test_excluded_fields_is_tuple: EXCLUDED_FIELDS is a tuple.
  - test_excluded_fields_has_exactly_two_elements: EXCLUDED_FIELDS has exactly 2 elements.
  - test_excluded_fields_contains_local_id: EXCLUDED_FIELDS contains 'local_id'.
  - test_excluded_fields_contains_rebar_id: EXCLUDED_FIELDS contains 'rebar-id'.
  - test_local_to_jira_status_is_nonempty_dict: default mapping is a non-empty
    dict of str->str.
  - test_local_to_jira_status_keys_are_known_local_statuses: keys cover the
    canonical local-side statuses used by outbound_update v1.
  - test_empty_mapping_kill_switch_safe: an empty mapping is a valid
    kill-switch configuration — module re-import with an empty dict assigned
    must not raise, and the default re-loaded value remains non-empty.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "config.py"
)


def _load_config() -> ModuleType:
    spec = importlib.util.spec_from_file_location("config", CONFIG_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def config() -> ModuleType:
    return _load_config()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_excluded_fields_is_tuple(config: ModuleType) -> None:
    assert isinstance(config.EXCLUDED_FIELDS, tuple)


def test_excluded_fields_has_exactly_two_elements(config: ModuleType) -> None:
    assert len(config.EXCLUDED_FIELDS) == 2


def test_excluded_fields_contains_local_id(config: ModuleType) -> None:
    assert 'local_id' in config.EXCLUDED_FIELDS


def test_excluded_fields_contains_rebar_id(config: ModuleType) -> None:
    assert 'rebar-id' in config.EXCLUDED_FIELDS


# ---------------------------------------------------------------------------
# local_to_jira_status mapping
# ---------------------------------------------------------------------------


def test_local_to_jira_status_is_nonempty_dict(config: ModuleType) -> None:
    """Default mapping is a non-empty dict of str->str."""
    assert isinstance(config.local_to_jira_status, dict)
    assert len(config.local_to_jira_status) > 0
    for k, v in config.local_to_jira_status.items():
        assert isinstance(k, str)
        assert isinstance(v, str)


def test_local_to_jira_status_keys_are_known_local_statuses(
    config: ModuleType,
) -> None:
    """Keys cover the canonical local-side statuses used by outbound_update v1."""
    expected_keys = {"open", "in_progress", "blocked", "closed", "cancelled"}
    assert expected_keys.issubset(set(config.local_to_jira_status.keys()))


def test_empty_mapping_kill_switch_safe() -> None:
    """An empty local_to_jira_status mapping is a valid kill-switch
    configuration — assigning {} must not raise, and the default-loaded
    mapping (fresh import) remains non-empty so preflight's no-status-update
    path is the documented safe fallthrough."""
    fresh = _load_config()
    # Empty assignment must be tolerated at the module-attribute level.
    fresh.local_to_jira_status = {}
    assert fresh.local_to_jira_status == {}
    # A fresh import must restore the documented non-empty default.
    reloaded = _load_config()
    assert isinstance(reloaded.local_to_jira_status, dict)
    assert len(reloaded.local_to_jira_status) > 0


# ---------------------------------------------------------------------------
# jira_to_local_status — canonical reverse map (ticket robe-creek-zealot)
# ---------------------------------------------------------------------------

INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_differ.py"
)


def test_jira_to_local_status_is_nonempty_str_dict(config: ModuleType) -> None:
    mapping = config.jira_to_local_status
    assert isinstance(mapping, dict) and mapping
    assert all(
        isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
    )


def test_jira_to_local_status_canonical_preimages(config: ModuleType) -> None:
    """The non-injective forward map's canonical preimages: the UNANNOTATED
    local statuses. (Pre-fix, a lexicographic inversion imported
    'In Progress' as blocked and 'Done' as cancelled.)"""
    mapping = config.jira_to_local_status
    assert mapping["To Do"] == "open"
    assert mapping["In Progress"] == "in_progress"
    assert mapping["In Review"] == "in_progress"
    assert mapping["Done"] == "closed"


def test_jira_to_local_status_round_trips_through_forward_map(
    config: ModuleType,
) -> None:
    """Every reverse-mapped local status forward-maps to a live Jira status,
    and Jira statuses that exist in the forward map round-trip exactly
    (To Do/In Progress/Done are fixed points of forward∘reverse)."""
    fwd = config.local_to_jira_status
    rev = config.jira_to_local_status
    for jira_status, local_status in rev.items():
        assert local_status in fwd, (
            f"reverse-mapped local status {local_status!r} missing from "
            "local_to_jira_status — preflight would abort on it"
        )
    for jira_status in ("To Do", "In Progress", "Done"):
        assert fwd[rev[jira_status]] == jira_status


def test_jira_to_local_status_parity_with_inbound_differ(
    config: ModuleType,
) -> None:
    """config.jira_to_local_status must stay in lock-step with
    inbound_differ._JIRA_TO_LOCAL_STATUS: _apply_inbound_create maps the
    import's status through config, and the bound-ticket inbound differ maps
    through its module constant — any drift re-opens the pass-2 churn this
    map was added to fix (ticket robe-creek-zealot)."""
    import sys

    spec = importlib.util.spec_from_file_location(
        "inbound_differ_for_config_parity", INBOUND_DIFFER_PATH
    )
    assert spec is not None and spec.loader is not None
    inbound_differ = importlib.util.module_from_spec(spec)
    # Register before exec: the module defines dataclasses, which resolve
    # their namespace via sys.modules[cls.__module__] at class-creation time.
    sys.modules["inbound_differ_for_config_parity"] = inbound_differ
    spec.loader.exec_module(inbound_differ)  # type: ignore[union-attr]
    assert config.jira_to_local_status == inbound_differ._JIRA_TO_LOCAL_STATUS
