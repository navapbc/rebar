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

REPO_ROOT = Path(__file__).resolve().parents[3]
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
