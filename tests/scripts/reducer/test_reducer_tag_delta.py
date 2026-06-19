"""Reducer support for TAG_DELTA events (epic P2.3 / WU-1).

TAG_DELTA replaces the whole-field ``EDIT.tags`` last-writer-wins clobber with
add/remove deltas that converge under the deterministic HLC+UUID replay order.

Event structure::

    {"event_type": "TAG_DELTA", "timestamp": <int>, "uuid": "<uuid>", ...,
     "data": {"added": ["x", ...], "removed": ["y", ...]}}

Pinned here: add/remove application, EDIT-base-then-delta carry-forward, two-clone
convergence (both adds survive), idempotent replay, the intra-event add-wins
contract, defensive non-list guards, and the downgraded-clone forward-compat case
(a reducer that does not know TAG_DELTA preserves-and-ignores it).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def reducer() -> ModuleType:
    import rebar.reducer as reducer_mod

    return reducer_mod


_UUID_CREATE = "aaaaaaaa-0001-4000-8000-000000000001"
_UUID_A = "bbbbbbbb-0002-4000-8000-000000000002"
_UUID_B = "cccccccc-0003-4000-8000-000000000003"
_UUID_C = "dddddddd-0004-4000-8000-000000000004"


def _write_event(ticket_dir: Path, timestamp: int, uuid: str, event_type: str, data: dict) -> Path:
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test User",
        "data": data,
    }
    path = ticket_dir / f"{timestamp}-{uuid}-{event_type}.json"
    path.write_text(json.dumps(payload))
    return path


def _ticket_dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def _create(ticket_dir: Path, tags: list[str] | None = None) -> None:
    data: dict = {"ticket_type": "task", "title": "T", "parent_id": ""}
    if tags is not None:
        data["tags"] = tags
    _write_event(ticket_dir, 1000, _UUID_CREATE, "CREATE", data)


@pytest.mark.unit
@pytest.mark.scripts
def test_tag_delta_adds_tags(tmp_path: Path, reducer: ModuleType) -> None:
    d = _ticket_dir(tmp_path, "td-add")
    _create(d)
    _write_event(d, 2000, _UUID_A, "TAG_DELTA", {"added": ["blue", "red"], "removed": []})
    state = reducer.reduce_ticket(d)
    assert sorted(state["tags"]) == ["blue", "red"]


@pytest.mark.unit
@pytest.mark.scripts
def test_tag_delta_removes_tags(tmp_path: Path, reducer: ModuleType) -> None:
    d = _ticket_dir(tmp_path, "td-remove")
    _create(d, tags=["blue", "red", "green"])
    _write_event(d, 2000, _UUID_A, "TAG_DELTA", {"added": [], "removed": ["red"]})
    state = reducer.reduce_ticket(d)
    assert sorted(state["tags"]) == ["blue", "green"]


@pytest.mark.unit
@pytest.mark.scripts
def test_edit_tags_base_then_delta_carries_forward(tmp_path: Path, reducer: ModuleType) -> None:
    """A historical whole-field EDIT.tags forms the base; a later delta layers on it."""
    d = _ticket_dir(tmp_path, "td-base")
    _create(d)
    _write_event(d, 2000, _UUID_A, "EDIT", {"fields": {"tags": ["a", "b"]}})
    _write_event(d, 3000, _UUID_B, "TAG_DELTA", {"added": ["c"], "removed": []})
    state = reducer.reduce_ticket(d)
    assert sorted(state["tags"]) == ["a", "b", "c"]


@pytest.mark.unit
@pytest.mark.scripts
def test_two_concurrent_adds_both_survive(tmp_path: Path, reducer: ModuleType) -> None:
    """Two clones each add a different tag (two TAG_DELTA events) -> both survive."""
    d = _ticket_dir(tmp_path, "td-concurrent")
    _create(d)
    _write_event(d, 2000, _UUID_A, "TAG_DELTA", {"added": ["x"], "removed": []})
    _write_event(d, 2000, _UUID_B, "TAG_DELTA", {"added": ["y"], "removed": []})
    state = reducer.reduce_ticket(d)
    assert sorted(state["tags"]) == ["x", "y"]


@pytest.mark.unit
@pytest.mark.scripts
def test_add_then_remove_same_tag_is_deterministic(tmp_path: Path, reducer: ModuleType) -> None:
    """add c (t=2000) then remove c (t=3000) in replay order -> c removed."""
    d = _ticket_dir(tmp_path, "td-add-remove")
    _create(d)
    _write_event(d, 2000, _UUID_A, "TAG_DELTA", {"added": ["c"], "removed": []})
    _write_event(d, 3000, _UUID_B, "TAG_DELTA", {"added": [], "removed": ["c"]})
    state = reducer.reduce_ticket(d)
    assert state["tags"] == []


@pytest.mark.unit
@pytest.mark.scripts
def test_intra_event_conflict_add_wins(tmp_path: Path, reducer: ModuleType) -> None:
    """A malformed delta with a tag in both added and removed: add wins (reducer contract)."""
    d = _ticket_dir(tmp_path, "td-conflict")
    _create(d)
    _write_event(d, 2000, _UUID_A, "TAG_DELTA", {"added": ["x"], "removed": ["x"]})
    state = reducer.reduce_ticket(d)
    assert state["tags"] == ["x"]


@pytest.mark.unit
@pytest.mark.scripts
def test_idempotent_replay(tmp_path: Path, reducer: ModuleType) -> None:
    """Adding an already-present tag is a no-op union; remove of absent is a no-op."""
    d = _ticket_dir(tmp_path, "td-idem")
    _create(d, tags=["x"])
    _write_event(d, 2000, _UUID_A, "TAG_DELTA", {"added": ["x"], "removed": ["absent"]})
    state = reducer.reduce_ticket(d)
    assert state["tags"] == ["x"]


@pytest.mark.unit
@pytest.mark.scripts
def test_non_list_fields_are_ignored(tmp_path: Path, reducer: ModuleType) -> None:
    """Defensive guards: non-list added/removed (from a buggy client) don't crash."""
    d = _ticket_dir(tmp_path, "td-guard")
    _create(d, tags=["keep"])
    _write_event(d, 2000, _UUID_A, "TAG_DELTA", {"added": "notalist", "removed": None})
    state = reducer.reduce_ticket(d)
    assert state["tags"] == ["keep"]


@pytest.mark.unit
@pytest.mark.scripts
def test_downgraded_clone_preserves_and_ignores(
    tmp_path: Path, reducer: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reducer predating TAG_DELTA (type absent from KNOWN_EVENT_TYPES and no
    dispatch branch) must preserve-and-ignore it on replay (epic AC5 forward-compat).

    We faithfully simulate the v2 clone by masking the dispatch-comparison constant
    and removing TAG_DELTA from KNOWN_EVENT_TYPES, so the event falls through to the
    generic unknown-type path and the tag mutation is invisible (no error)."""
    from rebar.reducer import _processors

    masked_known = frozenset(t for t in _processors.KNOWN_EVENT_TYPES if t != "TAG_DELTA")
    monkeypatch.setattr(_processors, "TAG_DELTA", "TAG_DELTA__masked_for_test")
    monkeypatch.setattr(_processors, "KNOWN_EVENT_TYPES", masked_known)

    d = _ticket_dir(tmp_path, "td-downgrade")
    _create(d, tags=["base"])
    _write_event(d, 2000, _UUID_A, "TAG_DELTA", {"added": ["new"], "removed": []})
    state = reducer.reduce_ticket(d)
    # The downgraded reducer ignores the delta: tags stay at the base, no crash.
    assert state is not None
    assert state["tags"] == ["base"]
