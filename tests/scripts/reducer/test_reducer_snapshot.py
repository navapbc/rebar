"""SNAPSHOT restore / dedup and compaction integration

Split from the former monolithic tests/scripts/test_ticket_reducer.py along
reducer-concern seams. The module-under-test fixture (`reducer`) lives in
conftest.py; event-writing helpers (`_write_event`, `_UUID*`) in _events.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _UUID3, _write_event

# ---------------------------------------------------------------------------
# Test 18: SNAPSHOT event restores compiled state
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_event_restores_compiled_state(tmp_path: Path, reducer: ModuleType) -> None:
    """A SNAPSHOT event with compiled_state in data must restore that state directly.

    Without the fix: ticket-reducer.py does not yet handle SNAPSHOT events. The reducer
    will either ignore the event or raise, causing this test to fail.
    """
    ticket_dir = tmp_path / "tkt-snapshot-basic"
    ticket_dir.mkdir()

    compiled = {
        "ticket_id": "tkt-snapshot-basic",
        "ticket_type": "task",
        "title": "Compacted title",
        "status": "closed",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": None,
        "source_event_uuids": [_UUID, _UUID2],
    }

    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid=_UUID3,
        event_type="SNAPSHOT",
        data={"compiled_state": compiled, "source_event_uuids": [_UUID, _UUID2]},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "SNAPSHOT event must produce non-None state"
    assert state["title"] == "Compacted title", (
        f"SNAPSHOT compiled_state title must be restored; got {state['title']!r}"
    )
    assert state["status"] == "closed", (
        f"SNAPSHOT compiled_state status must be restored; got {state['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 19: SNAPSHOT + post-snapshot events applied correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_plus_post_snapshot_events_applied(tmp_path: Path, reducer: ModuleType) -> None:
    """A STATUS event after a SNAPSHOT (not in source_event_uuids) must be applied.

    Without the fix: SNAPSHOT handling not yet implemented.
    """
    ticket_dir = tmp_path / "tkt-snapshot-post"
    ticket_dir.mkdir()

    compiled = {
        "ticket_id": "tkt-snapshot-post",
        "ticket_type": "task",
        "title": "Snapshot base",
        "status": "open",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": None,
    }

    # SNAPSHOT at t=1742606000
    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid=_UUID,
        event_type="SNAPSHOT",
        data={"compiled_state": compiled, "source_event_uuids": ["pre-uuid-1"]},
    )

    # Post-snapshot STATUS event at t=1742607000 (uuid NOT in source_event_uuids)
    _write_event(
        ticket_dir,
        timestamp=1742607000,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "closed", "current_status": "open"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "SNAPSHOT + STATUS must produce non-None state"
    assert state["status"] == "closed", (
        "Post-snapshot STATUS event must be applied on top of SNAPSHOT state; "
        f"got status={state['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 20: SNAPSHOT deduplicates events in source_event_uuids
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_deduplicates_events_in_source_event_uuids(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """An event whose uuid is listed in source_event_uuids must be skipped.

    Without the fix: SNAPSHOT handling and deduplication not yet implemented.
    """
    ticket_dir = tmp_path / "tkt-snapshot-dedup"
    ticket_dir.mkdir()

    dup_uuid = "dup-uuid-0000-0000-0000-000000001234"

    compiled = {
        "ticket_id": "tkt-snapshot-dedup",
        "ticket_type": "task",
        "title": "Dedup test",
        "status": "open",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": None,
    }

    # SNAPSHOT listing dup_uuid in source_event_uuids
    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid=_UUID,
        event_type="SNAPSHOT",
        data={
            "compiled_state": compiled,
            "source_event_uuids": [dup_uuid],
        },
    )

    # Duplicate event — uuid matches one in source_event_uuids, must be SKIPPED
    _write_event(
        ticket_dir,
        timestamp=1742607000,
        uuid=dup_uuid,
        event_type="STATUS",
        data={"status": "closed", "current_status": "open"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "SNAPSHOT + dup event must produce non-None state"
    assert state["status"] == "open", (
        "Event with uuid in source_event_uuids must be skipped; "
        f"expected status='open' (from SNAPSHOT), got status={state['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 21: SNAPSHOT-only ticket returns compiled state (no CREATE needed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_only_ticket_returns_compiled_state(tmp_path: Path, reducer: ModuleType) -> None:
    """A ticket with only a SNAPSHOT event (no CREATE) must return the compiled_state.

    Without the fix: SNAPSHOT handling not yet implemented; reducer currently returns None
    when no CREATE event is found.
    """
    ticket_dir = tmp_path / "tkt-snapshot-only"
    ticket_dir.mkdir()

    compiled = {
        "ticket_id": "tkt-snapshot-only",
        "ticket_type": "task",
        "title": "Snapshot only ticket",
        "status": "in_progress",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": "epic-123",
    }

    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid=_UUID,
        event_type="SNAPSHOT",
        data={"compiled_state": compiled, "source_event_uuids": ["old-1", "old-2"]},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "SNAPSHOT-only ticket must return compiled_state, not None"
    assert state["title"] == "Snapshot only ticket", (
        f"SNAPSHOT compiled_state must be used; got title={state['title']!r}"
    )


# ---------------------------------------------------------------------------
# Test 22: Cache invalidation after compaction (file deletion + SNAPSHOT)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_invalidation_after_compaction_file_deletion(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """After compaction (old files deleted, SNAPSHOT written), cache must invalidate.

    Setup: write CREATE + 3 STATUS events, call reduce_ticket() to warm cache,
    then delete those 4 files and write a SNAPSHOT event (simulating compaction).
    Call reduce_ticket() again and assert the result matches the SNAPSHOT state.

    Without the fix: SNAPSHOT handling not yet implemented. Even if cache invalidation works
    (file count change triggers cache miss), the reducer will fail on the
    SNAPSHOT event type.
    """
    ticket_dir = tmp_path / "tkt-compact-cache"
    ticket_dir.mkdir()

    # Write CREATE + 3 STATUS events
    create_path = _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Pre-compaction title",
            "parent_id": None,
        },
        author="Alice",
    )

    status_paths = []
    for _i, (uuid_val, ts) in enumerate(
        [
            ("11111111-1111-1111-1111-111111111111", 1742605300),
            ("22222222-2222-2222-2222-222222222222", 1742605400),
            ("33333333-3333-3333-3333-333333333333", 1742605500),
        ]
    ):
        p = _write_event(
            ticket_dir,
            timestamp=ts,
            uuid=uuid_val,
            event_type="STATUS",
            data={"status": "in_progress", "current_status": "open"},
        )
        status_paths.append(p)

    # Warm cache
    state1 = reducer.reduce_ticket(ticket_dir)
    assert state1 is not None, "Setup: first reduce must return state"

    # Simulate compaction: delete original files, write SNAPSHOT
    create_path.unlink()
    for p in status_paths:
        p.unlink()

    compacted_state = {
        "ticket_id": "tkt-compact-cache",
        "ticket_type": "task",
        "title": "Compacted title",
        "status": "closed",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": None,
    }

    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid="44444444-4444-4444-4444-444444444444",
        event_type="SNAPSHOT",
        data={
            "compiled_state": compacted_state,
            "source_event_uuids": [
                _UUID,
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            ],
        },
    )

    # Second call — cache must be invalidated (file count changed), SNAPSHOT applied
    state2 = reducer.reduce_ticket(ticket_dir)

    assert state2 is not None, "After compaction, reduce_ticket must return SNAPSHOT compiled_state"
    assert state2["title"] == "Compacted title", (
        "After compaction + cache invalidation, title must come from SNAPSHOT; "
        f"got title={state2['title']!r}"
    )
    assert state2["status"] == "closed", (
        "After compaction, status must come from SNAPSHOT compiled_state; "
        f"got status={state2['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 23: Integration — warm cache before compaction returns correct state after
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.scripts
def test_integ_cache_warm_before_compaction_returns_correct_state_after(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Warm cache before compaction must be invalidated after compaction runs.

    Setup: write CREATE + 3 STATUS events, warm the cache via reduce_ticket(),
    then simulate compaction (delete all event files, write a SNAPSHOT event).
    Call reduce_ticket() again and verify that:
      - The cache was invalidated (file count changed: 4 events → 1 SNAPSHOT)
      - The returned state reflects the SNAPSHOT compiled_state (not the cached state)

    This validates the end-to-end contract between the caching mechanism
    (w21-f8tg: directory listing hash) and SNAPSHOT handling (w21-vz2h):
    compaction changes both the file count AND the filenames, guaranteeing
    a cache miss via the dir_hash check.
    """
    ticket_dir = tmp_path / "tkt-compact-cache"
    ticket_dir.mkdir()

    # Write a CREATE + 3 STATUS events
    _write_event(
        ticket_dir,
        1742605200,
        _UUID,
        "CREATE",
        {"ticket_type": "task", "title": "Cache test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        1742605201,
        _UUID2,
        "STATUS",
        {"status": "in_progress", "current_status": None},
    )
    _write_event(
        ticket_dir,
        1742605202,
        _UUID3,
        "STATUS",
        {"status": "closed", "current_status": None},
    )
    _write_event(
        ticket_dir,
        1742605203,
        "aaaabbbb-aaaa-bbbb-cccc-ddddeeeeFFFF",
        "STATUS",
        {"status": "open", "current_status": None},
    )

    # Warm the cache
    state_before = reducer.reduce_ticket(ticket_dir)
    assert state_before is not None, (
        "Setup: reduce_ticket must return state after CREATE + STATUS events"
    )
    assert state_before["status"] == "open", (
        f"Setup: expected status='open' from last STATUS event, got {state_before['status']!r}"
    )

    # Verify cache was written after warm
    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), ".cache.json must be written after first reduce_ticket() call"

    # Simulate compaction: delete all 4 event files, write SNAPSHOT
    for f in ticket_dir.glob("*.json"):
        if f.name != ".cache.json":
            f.unlink()

    snapshot_payload = {
        "timestamp": 1742605210,
        "uuid": "snapshot-uuid-1234",
        "event_type": "SNAPSHOT",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Alice",
        "data": {
            "compiled_state": {
                "ticket_id": "tkt-compact-cache",
                "ticket_type": "task",
                "title": "Cache test",
                "status": "closed",  # compacted final state
                "author": "Alice",
                "created_at": 1742605200,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "parent_id": None,
                "comments": [],
                "deps": [],
            },
            "source_event_uuids": [
                _UUID,
                _UUID2,
                _UUID3,
                "aaaabbbb-aaaa-bbbb-cccc-ddddeeeeFFFF",
            ],
        },
    }
    (ticket_dir / "1742605210-snapshot-uuid-1234-SNAPSHOT.json").write_text(
        json.dumps(snapshot_payload)
    )

    # After compaction — cache must miss (file count changed) and return SNAPSHOT state
    state_after = reducer.reduce_ticket(ticket_dir)
    assert state_after is not None, (
        "reduce_ticket must return non-None state after compaction (SNAPSHOT present)"
    )
    assert state_after["status"] == "closed", (
        f"Expected status='closed' (SNAPSHOT state), got {state_after['status']!r}"
    )
    assert state_after["title"] == "Cache test", (
        f"Expected title='Cache test' from SNAPSHOT compiled_state, got {state_after['title']!r}"
    )


# ---------------------------------------------------------------------------
# creation_channel projection through SNAPSHOT restore (story 568c)
# ---------------------------------------------------------------------------

_CHANNEL_BASE = {
    "ticket_type": "task",
    "title": "T",
    "status": "open",
    "created_at": 1742605200,
    "comments": [],
    "deps": [],
    "parent_id": None,
    "source_event_uuids": [_UUID2],
}


def _snapshot_only_dir(tmp_path: Path, name: str, compiled: dict) -> Path:
    tdir = tmp_path / name
    tdir.mkdir()
    _write_event(
        tdir,
        1742605200,
        _UUID,
        "SNAPSHOT",
        {"compiled_state": compiled, "source_event_uuids": [_UUID2]},
    )
    return tdir


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_legacy_creation_channel_infers_jira(tmp_path: Path, reducer: ModuleType) -> None:
    # A pre-feature SNAPSHOT (compiled_state has no creation_channel) whose restored
    # envelope bears the exact legacy-Jira signature must re-infer jira at read time.
    compiled = {
        **_CHANNEL_BASE,
        "ticket_id": "jira-dig-1",
        "author": "reconciler",
        "env_id": "reconciler",
    }
    state = reducer.reduce_ticket(str(_snapshot_only_dir(tmp_path, "chan-legacy", compiled)))
    assert state["creation_channel"] == "jira"
    assert state["creation_channel_inferred"] is True


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_legacy_creation_channel_near_miss_unknown(
    tmp_path: Path, reducer: ModuleType
) -> None:
    # jira-* id but a non-reconciler author: near-miss → unknown, no marker.
    compiled = {
        **_CHANNEL_BASE,
        "ticket_id": "jira-dig-1",
        "author": "someone-else",
        "env_id": "reconciler",
    }
    state = reducer.reduce_ticket(str(_snapshot_only_dir(tmp_path, "chan-nearmiss", compiled)))
    assert state["creation_channel"] == "unknown"
    assert "creation_channel_inferred" not in state


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_recorded_creation_channel_not_clobbered(
    tmp_path: Path, reducer: ModuleType
) -> None:
    # A recorded channel in compiled_state must survive SNAPSHOT restore VERBATIM — even
    # when the envelope would otherwise match the legacy-Jira inference signature. This
    # pins the load-bearing guard (re-inference is skipped when a channel is present).
    compiled = {
        **_CHANNEL_BASE,
        "ticket_id": "jira-dig-1",
        "author": "reconciler",
        "env_id": "reconciler",
        "creation_channel": "cli",
    }
    state = reducer.reduce_ticket(str(_snapshot_only_dir(tmp_path, "chan-recorded", compiled)))
    assert state["creation_channel"] == "cli"
    assert "creation_channel_inferred" not in state


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_already_inferred_creation_channel_preserved(
    tmp_path: Path, reducer: ModuleType
) -> None:
    compiled = {
        **_CHANNEL_BASE,
        "ticket_id": "jira-dig-1",
        "author": "reconciler",
        "env_id": "reconciler",
        "creation_channel": "jira",
        "creation_channel_inferred": True,
    }
    state = reducer.reduce_ticket(str(_snapshot_only_dir(tmp_path, "chan-inferred", compiled)))
    assert state["creation_channel"] == "jira"
    assert state["creation_channel_inferred"] is True


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_provisional_unknown_creation_channel_infers_jira(
    tmp_path: Path, reducer: ModuleType
) -> None:
    # A provisional, marker-less "unknown" in compiled_state (not a concrete recorded value)
    # with the exact legacy-Jira envelope is re-evaluated to jira at restore time.
    compiled = {
        **_CHANNEL_BASE,
        "ticket_id": "jira-dig-1",
        "author": "reconciler",
        "env_id": "reconciler",
        "creation_channel": "unknown",
    }
    state = reducer.reduce_ticket(str(_snapshot_only_dir(tmp_path, "chan-prov-jira", compiled)))
    assert state["creation_channel"] == "jira"
    assert state["creation_channel_inferred"] is True


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_provisional_unknown_creation_channel_near_miss_stays_unknown(
    tmp_path: Path, reducer: ModuleType
) -> None:
    # Provisional "unknown" but a NON-jira id → re-evaluation keeps it unknown (no false jira).
    compiled = {
        **_CHANNEL_BASE,
        "ticket_id": "local-1",
        "author": "reconciler",
        "env_id": "reconciler",
        "creation_channel": "unknown",
    }
    state = reducer.reduce_ticket(str(_snapshot_only_dir(tmp_path, "chan-prov-unk", compiled)))
    assert state["creation_channel"] == "unknown"
    assert "creation_channel_inferred" not in state
