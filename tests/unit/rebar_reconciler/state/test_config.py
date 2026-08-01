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
    """`config.local_to_jira_status` must equal the canonical Jira-family map.

    THE THIRD COPY (bug fe15-3bc4-ed70-4b61). Story J2 consolidated the two drifted copies
    inside `adapters/jira/` into a single definition in
    `adapters/jira_family/value_maps.LOCAL_STATUS_TO_JIRA`, which both the ACLI transport and
    the Backend port now read. It deliberately left THIS copy out of scope and named this bug
    in its own module docstring. The two are content-identical today, so there is no drift yet
    — and nothing prevented one: mutating `local_to_jira_status["deleted"]` to
    "Archived-MUTANT", a value that is not a state in the live DIG workflow, left **59 tests
    green** across this module, the ACLI status-resolution suite and the jira-family seam
    suites. That measurement is why this test exists.

    A PARITY TEST RATHER THAN A SHARED IMPORT, for the reasons recorded on
    `local_to_jira_status` itself: `config.py` imports nothing but `__future__`, and pulling a
    vendor adapter into core would invert the one-way dependency direction `adapters/jira_family`
    declares. This mirrors what `test_jira_to_local_status_parity_with_inbound_differ` above
    already does for the reverse map — the established idiom here for exactly this problem.

    FULL-DICT EQUALITY, not a per-key spot check.
    `test_idea_maps_to_jira_idea_across_all_status_maps` already pins the single `idea -> IDEA`
    entry across all three maps; the other six keys were
    the unguarded ones, which is precisely why the mutation above went unseen.
    """
    import sys

    # Loaded BY PATH, not imported as a package, so reading the adapter's map here creates no
    # import-time dependency from core's test context onto the vendor package. The nested
    # helper mirrors `test_idea_maps_to_jira_idea_across_all_status_maps`'s own loader rather
    # than editing it, keeping this change purely additive.
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
