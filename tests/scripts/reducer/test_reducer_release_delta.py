"""Reducer support for RELEASE_DELTA events (ADR 0124, epic inferior-seaborgium-aruanas).

RELEASE_DELTA carries Jira fix-version names into the compiled ``releases`` list with
TAG_DELTA's fold rules: removals before additions (an addition wins when one name is in
both lists), non-list bodies act as empty, non-string or empty names are skipped, and
repeated folds are idempotent.

Event structure::

    {"event_type": "RELEASE_DELTA", "timestamp": <int>, "uuid": "<uuid>", ...,
     "data": {"added": ["2.4", ...], "removed": ["2.3", ...]}}
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.scripts]


@pytest.fixture(scope="module")
def reducer() -> ModuleType:
    import rebar.reducer as reducer_mod

    return reducer_mod


_UUID_CREATE = "aaaaaaaa-0001-4000-8000-000000000001"
_UUID_A = "bbbbbbbb-0002-4000-8000-000000000002"
_UUID_B = "cccccccc-0003-4000-8000-000000000003"
_UUID_SNAP = "dddddddd-0004-4000-8000-000000000004"


def _write_event(ticket_dir: Path, timestamp: int, uuid: str, event_type: str, data: dict) -> None:
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test User",
        "data": data,
    }
    (ticket_dir / f"{timestamp}-{uuid}-{event_type}.json").write_text(json.dumps(payload))


def _ticket(tmp_path: Path, name: str, tags: list[str] | None = None) -> Path:
    d = tmp_path / name
    d.mkdir()
    data: dict = {"ticket_type": "task", "title": "T", "parent_id": ""}
    if tags is not None:
        data["tags"] = tags
    _write_event(d, 1000, _UUID_CREATE, "CREATE", data)
    return d


def test_initial_state_has_empty_releases(reducer: ModuleType) -> None:
    assert reducer.make_initial_state()["releases"] == []


def test_no_release_events_reduces_to_empty(tmp_path: Path, reducer: ModuleType) -> None:
    d = _ticket(tmp_path, "rd-none")
    assert reducer.reduce_ticket(d)["releases"] == []


def test_add_then_move_to_next_release(tmp_path: Path, reducer: ModuleType) -> None:
    d = _ticket(tmp_path, "rd-happy")
    _write_event(d, 2000, _UUID_A, "RELEASE_DELTA", {"added": ["2.4"]})
    _write_event(d, 3000, _UUID_B, "RELEASE_DELTA", {"added": ["2.5"], "removed": ["2.4"]})
    assert reducer.reduce_ticket(d)["releases"] == ["2.5"]


def test_intra_event_add_wins(tmp_path: Path, reducer: ModuleType) -> None:
    d = _ticket(tmp_path, "rd-conflict")
    _write_event(d, 2000, _UUID_A, "RELEASE_DELTA", {"added": ["2.4"], "removed": ["2.4"]})
    assert reducer.reduce_ticket(d)["releases"] == ["2.4"]


def test_repeated_delta_is_idempotent(tmp_path: Path, reducer: ModuleType) -> None:
    d = _ticket(tmp_path, "rd-idem")
    _write_event(d, 2000, _UUID_A, "RELEASE_DELTA", {"added": ["2.4"]})
    _write_event(d, 3000, _UUID_B, "RELEASE_DELTA", {"added": ["2.4"], "removed": ["absent"]})
    assert reducer.reduce_ticket(d)["releases"] == ["2.4"]


def test_two_concurrent_adds_both_survive(tmp_path: Path, reducer: ModuleType) -> None:
    d = _ticket(tmp_path, "rd-concurrent")
    _write_event(d, 2000, _UUID_A, "RELEASE_DELTA", {"added": ["2.4"]})
    _write_event(d, 2000, _UUID_B, "RELEASE_DELTA", {"added": ["2.5"]})
    assert sorted(reducer.reduce_ticket(d)["releases"]) == ["2.4", "2.5"]


@pytest.mark.parametrize(
    "body",
    [{"added": "2.4"}, {"added": [1, ""]}, {"added": None, "removed": "2.4"}],
    ids=["string-body", "non-string-and-empty-names", "null-and-string"],
)
def test_malformed_bodies_leave_releases_empty(
    tmp_path: Path, reducer: ModuleType, body: dict
) -> None:
    d = _ticket(tmp_path, "rd-malformed")
    _write_event(d, 2000, _UUID_A, "RELEASE_DELTA", body)
    assert reducer.reduce_ticket(d)["releases"] == []


def test_release_delta_never_changes_tags(tmp_path: Path, reducer: ModuleType) -> None:
    d = _ticket(tmp_path, "rd-no-leak", tags=["keep"])
    _write_event(d, 2000, _UUID_A, "RELEASE_DELTA", {"added": ["2.4"], "removed": ["keep"]})
    state = reducer.reduce_ticket(d)
    assert state["tags"] == ["keep"]
    assert state["releases"] == ["2.4"]


def test_pre_feature_snapshot_then_delta(tmp_path: Path, reducer: ModuleType) -> None:
    """A SNAPSHOT compacted before ``releases`` existed keeps the ``[]`` default, and a
    later RELEASE_DELTA folds onto it."""
    d = _ticket(tmp_path, "rd-snapshot")
    compiled = reducer.reduce_ticket(d)
    compiled.pop("releases", None)
    _write_event(
        d,
        2000,
        _UUID_SNAP,
        "SNAPSHOT",
        {"compiled_state": compiled, "source_event_uuids": [_UUID_CREATE]},
    )
    (d / f"1000-{_UUID_CREATE}-CREATE.json").unlink()
    assert reducer.reduce_ticket(d)["releases"] == []
    _write_event(d, 3000, _UUID_A, "RELEASE_DELTA", {"added": ["2.4"]})
    assert reducer.reduce_ticket(d)["releases"] == ["2.4"]


def test_downgraded_clone_preserves_and_ignores(
    tmp_path: Path, reducer: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reducer predating RELEASE_DELTA ignores it on replay without error."""
    from rebar.reducer import _replay

    masked_known = frozenset(t for t in _replay.KNOWN_EVENT_TYPES if t != "RELEASE_DELTA")
    monkeypatch.setattr(_replay, "KNOWN_EVENT_TYPES", masked_known)

    d = _ticket(tmp_path, "rd-downgrade")
    _write_event(d, 2000, _UUID_A, "RELEASE_DELTA", {"added": ["2.4"]})
    state = reducer.reduce_ticket(d)
    assert state is not None
    assert state["releases"] == []
