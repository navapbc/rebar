"""Replay coverage for first-event-wins UUID deduplication."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Reducer under test — ``rebar.reducer``.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reducer() -> ModuleType:
    """Return the in-process ``rebar.reducer`` module (reduce_ticket et al.)."""
    import rebar.reducer as reducer_mod

    return reducer_mod


def _write_event(
    ticket_dir: Path,
    timestamp: int,
    uuid: str,
    event_type: str,
    data: dict,
    env_id: str = "00000000-0000-4000-8000-000000000001",
    author: str = "Test User",
) -> Path:
    """Write an event file, allowing one payload UUID at distinct timestamps."""
    filename = f"{timestamp}-{uuid}-{event_type}.json"
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    path = ticket_dir / filename
    path.write_text(json.dumps(payload))
    return path


_CREATE_UUID = "11111111-1111-4111-8111-111111111111"
_COMMENT_UUID = "22222222-2222-4222-8222-222222222222"
_STATUS_UUID = "33333333-3333-4333-8333-333333333333"


# Duplicate comments


@pytest.mark.unit
@pytest.mark.scripts
def test_duplicate_uuid_comment_applies_once(tmp_path: Path, reducer: ModuleType) -> None:
    """A repeated COMMENT UUID applies once."""
    ticket_dir = tmp_path / "tkt-dup-comment"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_CREATE_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Dedup test"},
    )
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=_COMMENT_UUID,
        event_type="COMMENT",
        data={"body": "hello world"},
    )
    # Same payload uuid, later timestamp => distinct filename, duplicate event.
    _write_event(
        ticket_dir,
        timestamp=2001,
        uuid=_COMMENT_UUID,
        event_type="COMMENT",
        data={"body": "hello world"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    bodies = [c["body"] for c in state["comments"]]
    assert bodies == ["hello world"], (
        f"Duplicate-UUID COMMENT must apply exactly once; got {bodies}"
    )


# Duplicate statuses


@pytest.mark.unit
@pytest.mark.scripts
def test_duplicate_uuid_status_does_not_self_fork(tmp_path: Path, reducer: ModuleType) -> None:
    """A repeated STATUS UUID produces one transition, not a fork."""
    ticket_dir = tmp_path / "tkt-dup-status"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_CREATE_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Status dedup"},
    )
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=_STATUS_UUID,
        event_type="STATUS",
        data={"status": "closed", "current_status": "open"},
    )
    _write_event(
        ticket_dir,
        timestamp=2001,
        uuid=_STATUS_UUID,
        event_type="STATUS",
        data={"status": "closed", "current_status": "open"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert state["status"] == "closed", (
        f"Duplicate STATUS uuid must resolve to a single transition; got {state['status']}"
    )


# Distinct events


@pytest.mark.unit
@pytest.mark.scripts
def test_distinct_uuid_events_unchanged(tmp_path: Path, reducer: ModuleType) -> None:
    """Three distinct COMMENT uuids all apply — dedup must not over-collapse."""
    ticket_dir = tmp_path / "tkt-distinct"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_CREATE_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Distinct"},
    )
    for i in range(3):
        _write_event(
            ticket_dir,
            timestamp=2000 + i,
            uuid=f"4444444{i}-4444-4444-8444-444444444444",
            event_type="COMMENT",
            data={"body": f"comment-{i}"},
        )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    bodies = [c["body"] for c in state["comments"]]
    assert bodies == ["comment-0", "comment-1", "comment-2"], (
        f"Distinct-UUID comments must all apply in filename order; got {bodies}"
    )


# Snapshot interaction


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_plus_post_snapshot_duplicate_applies_once(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A post-snapshot COMMENT duplicated by UUID applies once."""
    ticket_dir = tmp_path / "tkt-snap"
    ticket_dir.mkdir()

    # Pre-snapshot CREATE + COMMENT, captured into the snapshot's source uuids.
    pre_comment_uuid = "55555555-5555-4555-8555-555555555555"
    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_CREATE_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Snap base"},
    )
    _write_event(
        ticket_dir,
        timestamp=1100,
        uuid=pre_comment_uuid,
        event_type="COMMENT",
        data={"body": "pre-snapshot comment"},
    )

    # SNAPSHOT captures compiled state up to and including the pre-snapshot
    # comment (so the raw CREATE/COMMENT files are skipped on replay).
    _write_event(
        ticket_dir,
        timestamp=1500,
        uuid="66666666-6666-4666-8666-666666666666",
        event_type="SNAPSHOT",
        data={
            "source_event_uuids": [_CREATE_UUID, pre_comment_uuid],
            "compiled_state": {
                "ticket_id": "tkt-snap",
                "ticket_type": "task",
                "title": "Snap base",
                "status": "open",
                "comments": [
                    {
                        "body": "pre-snapshot comment",
                        "author": "Test User",
                        "timestamp": 1100,
                    }
                ],
            },
        },
    )

    # Post-snapshot COMMENT duplicated under two filenames (same payload uuid).
    post_uuid = "77777777-7777-4777-8777-777777777777"
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=post_uuid,
        event_type="COMMENT",
        data={"body": "post-snapshot comment"},
    )
    _write_event(
        ticket_dir,
        timestamp=2001,
        uuid=post_uuid,
        event_type="COMMENT",
        data={"body": "post-snapshot comment"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    bodies = [c["body"] for c in state["comments"]]
    assert bodies == ["pre-snapshot comment", "post-snapshot comment"], (
        f"Snapshot + duplicated post-snapshot comment must apply once each; got {bodies}"
    )
