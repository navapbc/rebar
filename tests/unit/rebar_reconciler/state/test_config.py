"""Status mapping and excluded-field contracts for the reconciler.

The tests pin excluded identifiers, outbound statuses, canonical inbound
preimages, and adapter-map parity. Empty outbound mappings remain a supported
status-update kill switch.
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
CONFIG_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "config.py"


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
    assert "local_id" in config.EXCLUDED_FIELDS


def test_excluded_fields_contains_rebar_id(config: ModuleType) -> None:
    assert "rebar-id" in config.EXCLUDED_FIELDS


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
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())


def test_jira_to_local_status_canonical_preimages(config: ModuleType) -> None:
    """Canonical inbound preimages avoid ambiguous blocked and cancelled mappings."""
    mapping = config.jira_to_local_status
    assert mapping["To Do"] == "open"
    assert mapping["In Progress"] == "in_progress"
    assert mapping["In Review"] == "in_progress"
    assert mapping["Done"] == "closed"


def test_jira_to_local_status_round_trips_through_forward_map(
    config: ModuleType,
) -> None:
    """Each inbound local status maps forward, and canonical statuses round-trip."""
    fwd = config.local_to_jira_status
    rev = config.jira_to_local_status
    for _jira_status, local_status in rev.items():
        assert local_status in fwd, (
            f"reverse-mapped local status {local_status!r} missing from "
            "local_to_jira_status — preflight would abort on it"
        )
    for jira_status in ("To Do", "In Progress", "Done"):
        assert fwd[rev[jira_status]] == jira_status


def test_jira_to_local_status_parity_with_inbound_differ(
    config: ModuleType,
) -> None:
    """The inbound differ and configuration share the Jira-to-local status map."""
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
    # The `idea ↔ IDEA` entry must be present on BOTH sides of the parity.
    assert config.jira_to_local_status["IDEA"] == "idea"
    assert inbound_differ._JIRA_TO_LOCAL_STATUS["IDEA"] == "idea"


def test_idea_maps_to_jira_idea_across_all_status_maps(config: ModuleType) -> None:
    """`idea ↔ IDEA` is a unique (injective) mapping present in every hand-maintained
    reconciler status map — a missing one causes a preflight abort or a silent
    mistranslation (story tawny-herb-bug)."""

    def _load_attr(module_file: str, attr: str):
        path = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / module_file
        spec = importlib.util.spec_from_file_location(f"_parity_{module_file}", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"_parity_{module_file}"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return getattr(mod, attr)

    import sys

    assert config.local_to_jira_status["idea"] == "IDEA"
    assert config.jira_to_local_status["IDEA"] == "idea"
    assert _load_attr("inbound_differ.py", "_JIRA_TO_LOCAL_STATUS")["IDEA"] == "idea"
    assert (
        _load_attr("adapters/jira_family/value_maps.py", "LOCAL_STATUS_TO_JIRA")["idea"] == "IDEA"
    )
    assert _load_attr("adapters/jira/outbound_fields.py", "_LOCAL_TO_JIRA_STATUS")["idea"] == "IDEA"


def test_local_to_jira_status_parity_with_the_jira_family_map(config: ModuleType) -> None:
    """The core status map equals the Jira-family map.

    Full-dictionary parity catches drift without importing a vendor adapter
    into core. The test loads the adapter map by path to preserve dependency
    direction.
    """
    import sys

    # Load by path to compare maps without importing the vendor package into core.
    path = (
        REPO_ROOT
        / "src"
        / "rebar"
        / "_engine"
        / "rebar_reconciler"
        / "adapters"
        / "jira_family"
        / "value_maps.py"
    )
    spec = importlib.util.spec_from_file_location("_parity_jira_family_value_maps", path)
    assert spec is not None and spec.loader is not None
    value_maps = importlib.util.module_from_spec(spec)
    sys.modules["_parity_jira_family_value_maps"] = value_maps
    spec.loader.exec_module(value_maps)  # type: ignore[union-attr]

    assert config.local_to_jira_status == value_maps.LOCAL_STATUS_TO_JIRA, (
        "config.local_to_jira_status has DRIFTED from the canonical Jira-family map. These are "
        "two independent literals of one mapping (see the lock-step comment in config.py); a "
        "status that maps to a different Jira state through the config surface than through the "
        "adapter surface produces transitions Jira rejects, or silently wrong ones."
    )
