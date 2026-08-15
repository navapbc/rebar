"""Held-out oracle for the projects mapping store (story c927).

The edge/error cases the happy-path implementer does not see: the ``None`` not-synced
sentinels, and the fail-safe distinction between an absent record (legal, empty) and a
malformed one (fail-closed, raises).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


projects_store = _load("_projects_store_heldout", "projects_store.py")


def _write_record(repo_root: Path, record: dict) -> None:
    bridge_state = repo_root / ".tickets-tracker" / ".bridge_state"
    bridge_state.mkdir(parents=True, exist_ok=True)
    (bridge_state / "projects.json").write_text(json.dumps(record), encoding="utf-8")


def _write_raw(repo_root: Path, raw: str) -> None:
    bridge_state = repo_root / ".tickets-tracker" / ".bridge_state"
    bridge_state.mkdir(parents=True, exist_ok=True)
    (bridge_state / "projects.json").write_text(raw, encoding="utf-8")


def test_resolve_project_explicit_empty_string_is_none(tmp_path: Path) -> None:
    """``bridge_project == ""`` is the deliberate not-synced sentinel — never the default."""
    _write_record(
        tmp_path,
        {"version": 1, "legacy_default": "REB", "projects": {"REB": {"repos": ["rebar"]}}},
    )
    mapping = projects_store.load_mapping(tmp_path)

    assert projects_store.resolve_project({"bridge_project": ""}, mapping) is None


def test_resolve_project_null_sentinel_routes_to_legacy_default(tmp_path: Path) -> None:
    """A present-but-null ``bridge_project`` (the reducer's seeded absent/legacy sentinel,
    cef7 `_state.py:53-58`) is NOT the ``""`` never-sync value — it must route to
    ``legacy_default`` exactly like an absent field, so a no-flag legacy ticket syncs to
    the legacy project instead of being silently suppressed from outbound create."""
    _write_record(
        tmp_path,
        {"version": 1, "legacy_default": "REB", "projects": {"REB": {"repos": ["rebar"]}}},
    )
    mapping = projects_store.load_mapping(tmp_path)

    assert projects_store.resolve_project({"bridge_project": None}, mapping) == "REB"


def test_resolve_project_empty_legacy_default_degrades_to_none(tmp_path: Path) -> None:
    """An unconfigured store (empty ``legacy_default``) makes a legacy ticket resolve to None."""
    _write_record(tmp_path, {"version": 1, "legacy_default": "", "projects": {}})
    mapping = projects_store.load_mapping(tmp_path)

    assert projects_store.resolve_project({}, mapping) is None


def test_load_mapping_absent_record_is_legal_and_empty(tmp_path: Path) -> None:
    """No record on disk is legal: reads as an empty mapping whose legacy default is None."""
    (tmp_path / ".tickets-tracker").mkdir(parents=True, exist_ok=True)

    mapping = projects_store.load_mapping(tmp_path)

    # Observable: a legacy ticket against an absent record resolves to None (fail-safe:
    # nothing syncs until an ensure unit seeds the record).
    assert projects_store.resolve_project({}, mapping) is None


def test_load_mapping_malformed_record_raises(tmp_path: Path) -> None:
    """A truncated/corrupt record fails CLOSED rather than degrading to a permissive default."""
    _write_raw(tmp_path, '{"version": 1, "legacy_default": "REB", "projects":')

    # Contract is "raises, not silent degrade" (fail-closed); the impl raises ValueError.
    with pytest.raises(ValueError):
        projects_store.load_mapping(tmp_path)


# -- write-time key validation (ticket 209b) -------------------------------
# set_project / remove_project must reject a syntactically-invalid project key
# with a clear error BEFORE any file write, reusing fetcher._validate_project_key
# (no duplicated regex, no network/Jira call).

_INVALID_KEYS = ["le-g", "", "1x", "LEG ", " REB", "RE B", "_x", "9"]


def _projects_json(repo_root: Path) -> Path:
    return repo_root / ".tickets-tracker" / ".bridge_state" / "projects.json"


@pytest.mark.parametrize("bad_key", _INVALID_KEYS)
def test_set_project_rejects_invalid_key_and_writes_nothing(tmp_path: Path, bad_key: str) -> None:
    """A syntactically-invalid key raises and leaves projects.json unwritten."""
    with pytest.raises(ValueError):
        projects_store.set_project(tmp_path, bad_key, ["rebar"])

    assert not _projects_json(tmp_path).exists()


def test_set_project_invalid_key_does_not_mutate_existing_record(tmp_path: Path) -> None:
    """An invalid key must not disturb an already-persisted mapping (no partial write)."""
    _write_record(
        tmp_path,
        {"version": 1, "legacy_default": "REB", "projects": {"REB": {"repos": ["rebar"]}}},
    )
    before = _projects_json(tmp_path).read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        projects_store.set_project(tmp_path, "le-g", ["x"])

    assert _projects_json(tmp_path).read_text(encoding="utf-8") == before


@pytest.mark.parametrize("good_key", ["REB", "PROJ_2", "A", "Dig", "X9_y"])
def test_set_project_accepts_valid_key_and_writes(tmp_path: Path, good_key: str) -> None:
    """A valid key is accepted unchanged and persisted (B's write path preserved)."""
    projects_store.set_project(tmp_path, good_key, ["rebar", "other"])

    mapping = projects_store.load_mapping(tmp_path)
    assert mapping.projects[good_key] == {"repos": ["rebar", "other"]}


@pytest.mark.parametrize("bad_key", _INVALID_KEYS)
def test_remove_project_rejects_invalid_key_before_mutation(tmp_path: Path, bad_key: str) -> None:
    """remove_project rejects an invalid key with the same guard, before any mutation."""
    _write_record(
        tmp_path,
        {"version": 1, "legacy_default": "REB", "projects": {"REB": {"repos": ["rebar"]}}},
    )
    before = _projects_json(tmp_path).read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        projects_store.remove_project(tmp_path, bad_key)

    assert _projects_json(tmp_path).read_text(encoding="utf-8") == before
