"""Tests for reduce_ticket UUID-dedup on replay (bug 944c-374d).

The reducer replays raw event files in filename order with seen-UUID dedup:
the FIRST occurrence of a given event uuid (in filename order) applies; any
later file carrying the same uuid is skipped. This guards against a duplicate
event file (e.g. a COMMENT copied to a new filename with the same payload uuid)
double-applying on replay.

The pluggable reducer "strategy" was dead code (never wired into reduce_ticket)
and was deleted; these tests exercise the actual replay semantics end-to-end via
reduce_ticket, not a strategy object.

Test: python3 -m pytest tests/scripts/test_ticket_reducer_conflict.py -q
"""

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
    """Write a well-formed event JSON file and return its path.

    Filename embeds the timestamp first so two files sharing the same payload
    ``uuid`` but different timestamps get distinct filenames yet the same
    in-payload event uuid — the exact duplicate-UUID scenario under test.
    """
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


# ---------------------------------------------------------------------------
# Test 1: a duplicate-UUID COMMENT file applies exactly once
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_duplicate_uuid_comment_applies_once(tmp_path: Path, reducer: ModuleType) -> None:
    """A COMMENT event copied to a second filename (same uuid) appears ONCE.

    RED before the dedup fix: filename-order replay double-applies the second
    file, so the comment shows up twice.
    """
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


# ---------------------------------------------------------------------------
# Test 2: a duplicate STATUS event does not self-fork the status
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_duplicate_uuid_status_does_not_self_fork(tmp_path: Path, reducer: ModuleType) -> None:
    """A STATUS event duplicated under a second filename resolves to one status.

    Re-applying the same STATUS uuid must not be treated as two distinct envs /
    a fork; the net status is just the single transition.
    """
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


# ---------------------------------------------------------------------------
# Test 3: distinct-UUID multi-event tickets are unchanged by dedup
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Test 4: a SNAPSHOT + a post-snapshot duplicate pair applies once
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_plus_post_snapshot_duplicate_applies_once(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A post-snapshot COMMENT duplicated under a second filename applies once.

    The dedup lives AFTER the snapshot-source-uuid skip, so it composes cleanly
    with compaction: the snapshot restores compiled state, and the duplicated
    post-snapshot event still applies exactly once.
    """
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
