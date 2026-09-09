"""Bulk reduction fast-skip, orphan-marker repair, and directory hashing."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _UUID3, REPO_ROOT, _write_event

# Archived-marker fast-skip and missing-marker fallback.


@pytest.mark.unit
@pytest.mark.scripts
def test_reduce_all_tickets_skips_dir_with_archived_marker(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Skip marked archived directories before calling `reduce_ticket`."""
    import unittest.mock

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-archived-marker"
    ticket_dir.mkdir()

    # Write a valid CREATE + ARCHIVED event sequence
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Archived with marker"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="ARCHIVED",
        data={},
    )

    # Write the .archived marker file (simulates successful marker write after event)
    (ticket_dir / ".archived").write_text("")

    # Spy on reduce_ticket to detect if it was called for our archived dir
    original_reduce_ticket = reducer.reduce_ticket
    called_dirs: list[str] = []

    def spy_reduce_ticket(
        ticket_dir_path: str | os.PathLike[str],
        **kwargs: object,
    ) -> dict | None:
        called_dirs.append(str(ticket_dir_path))
        return original_reduce_ticket(ticket_dir_path, **kwargs)

    with unittest.mock.patch.object(reducer, "reduce_ticket", side_effect=spy_reduce_ticket):
        results = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    # Verify the archived ticket dir was NOT processed by reduce_ticket()
    archived_dir_str = str(ticket_dir)
    assert not any(
        os.path.normpath(d) == os.path.normpath(archived_dir_str) for d in called_dirs
    ), (
        "reduce_all_tickets(exclude_archived=True) must skip calling reduce_ticket() "
        "for directories with a .archived marker file (fast-skip path not implemented); "
        f"reduce_ticket was called for dirs: {called_dirs}"
    )

    # Ticket must not appear in the returned results
    returned_ids = [r.get("ticket_id") for r in results]
    assert "tkt-archived-marker" not in returned_ids, (
        "Ticket with .archived marker must not appear in results when "
        f"exclude_archived=True; got {returned_ids}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reduce_all_tickets_fallback_without_marker_correct_state(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Replay ARCHIVED state when its fast-skip marker is missing."""
    import unittest.mock

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-archived-no-marker"
    ticket_dir.mkdir()

    # Write a valid CREATE + ARCHIVED event sequence (NO .archived marker file)
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Archived without marker"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="ARCHIVED",
        data={},
    )

    # Deliberately do NOT create a .archived marker — simulates crash-injection scenario

    # Spy on reduce_ticket to verify it IS called for this dir.
    # reduce_all_tickets lives in ticket_reducer._api, so patch the name there.
    import rebar.reducer._api as _api_mod

    original_reduce_ticket = _api_mod.reduce_ticket
    called_dirs: list[str] = []

    def spy_reduce_ticket(
        ticket_dir_path: str | os.PathLike[str],
        **kwargs: object,
    ) -> dict | None:
        called_dirs.append(str(ticket_dir_path))
        return original_reduce_ticket(ticket_dir_path, **kwargs)

    with unittest.mock.patch.object(_api_mod, "reduce_ticket", side_effect=spy_reduce_ticket):
        results = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=False)

    # Verify reduce_ticket() WAS called for the crash-scenario dir (slow path)
    archived_dir_str = str(ticket_dir)
    assert any(os.path.normpath(d) == os.path.normpath(archived_dir_str) for d in called_dirs), (
        "reduce_all_tickets(exclude_archived=False) must call reduce_ticket() for dirs "
        "with ARCHIVED event but no .archived marker (slow-path fallback); "
        f"reduce_ticket was called for dirs: {called_dirs}"
    )

    # Verify the returned state has archived=True (slow-path correctness)
    assert len(results) == 1, f"Expected 1 result, got {len(results)}: {results}"
    state = results[0]
    assert state.get("ticket_id") == "tkt-archived-no-marker", (
        f"Expected ticket_id='tkt-archived-no-marker', got {state.get('ticket_id')!r}"
    )
    assert state.get("archived") is True, (
        "Ticket with ARCHIVED event but no .archived marker must have archived=True "
        f"in returned state (slow-path fallback correctness); got {state.get('archived')!r}"
    )


# Directory hashes include archived-marker presence.

_SCRIPTS_DIR = str(REPO_ROOT / "src" / "rebar" / "_engine")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from rebar.reducer._cache import compute_dir_hash as _compute_dir_hash  # noqa: E402


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hash_differs_with_marker_present(tmp_path: Path) -> None:
    """Change the directory hash when an archived marker appears."""
    ticket_dir = tmp_path / "tkt-marker-present"
    ticket_dir.mkdir()

    event_file = ticket_dir / f"1742605200-{_UUID}-CREATE.json"
    event_file.write_text(
        json.dumps(
            {
                "timestamp": 1742605200,
                "uuid": _UUID,
                "event_type": "CREATE",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Alice",
                "data": {"ticket_type": "task", "title": "Marker hash test"},
            }
        )
    )

    ticket_dir_str = str(ticket_dir)
    event_filenames = [event_file.name]

    # Hash without .archived marker
    hash_without_marker = _compute_dir_hash(ticket_dir_str, event_filenames)

    # Write the .archived marker (simulates write_marker())
    (ticket_dir / ".archived").touch()

    # Hash with .archived marker present — must differ
    hash_with_marker = _compute_dir_hash(ticket_dir_str, event_filenames)

    assert hash_without_marker != hash_with_marker, (
        "compute_dir_hash() must return a different hash when .archived marker is present; "
        "compute_dir_hash() must include marker presence in hash (SC5)"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hash_stable_when_no_marker_change(tmp_path: Path) -> None:
    """Hash must be stable across calls when events and marker state are unchanged.

    This verifies the positive case: no spurious cache invalidation when nothing changes.

    Setup: create a ticket dir with one event file and no marker.
    Compute the hash twice with the same inputs.

    Asserts: both hashes are identical (stable hash with no changes).
    """
    ticket_dir = tmp_path / "tkt-hash-stable"
    ticket_dir.mkdir()

    event_file = ticket_dir / f"1742605200-{_UUID}-CREATE.json"
    event_file.write_text(
        json.dumps(
            {
                "timestamp": 1742605200,
                "uuid": _UUID,
                "event_type": "CREATE",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Alice",
                "data": {"ticket_type": "task", "title": "Stable hash test"},
            }
        )
    )

    ticket_dir_str = str(ticket_dir)
    event_filenames = [event_file.name]

    # Compute hash twice with identical inputs — must be stable
    hash_first = _compute_dir_hash(ticket_dir_str, event_filenames)
    hash_second = _compute_dir_hash(ticket_dir_str, event_filenames)

    assert hash_first == hash_second, (
        "compute_dir_hash() must return the same hash when called twice with "
        "identical event files and no marker change; got unstable hashes"
    )


# Orphan-marker repair and cache invalidation.


@pytest.mark.unit
@pytest.mark.scripts
def test_orphan_marker_removed_and_slow_path_taken(tmp_path: Path, reducer: ModuleType) -> None:
    """Remove an orphan marker and return the ticket as active."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-orphan-marker"
    ticket_dir.mkdir()

    # Write only a CREATE event — no ARCHIVED event (active ticket)
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Active ticket with orphan marker"},
    )

    # Write a stale .archived marker with NO matching *-ARCHIVED.json event
    marker_path = ticket_dir / ".archived"
    marker_path.write_text("")

    # Call reduce_all_tickets — self-heal must fire, removing orphan marker
    results = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    # Orphan marker must be removed by self-heal
    assert not marker_path.exists(), (
        "reduce_all_tickets() must remove the stale .archived marker when no "
        "*-ARCHIVED.json event file is present (orphan self-heal not implemented)"
    )

    # Active ticket must appear in results (slow path taken after self-heal)
    returned_ids = [r.get("ticket_id") for r in results]
    assert "tkt-orphan-marker" in returned_ids, (
        "reduce_all_tickets() must include the ticket after self-healing an orphan "
        f"marker and falling back to slow path; got returned_ids={returned_ids}"
    )

    # Returned state must not be archived
    state = next(r for r in results if r.get("ticket_id") == "tkt-orphan-marker")
    assert not state.get("archived"), (
        "After orphan self-heal, returned ticket must not have archived=True; "
        f"got state['archived']={state.get('archived')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_valid_marker_not_removed(tmp_path: Path, reducer: ModuleType) -> None:
    """Retain a marker backed by an ARCHIVED event and skip its ticket."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-valid-marker"
    ticket_dir.mkdir()

    # Write CREATE + ARCHIVED events (legitimate archived ticket)
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Legitimately archived"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="ARCHIVED",
        data={},
    )

    # Write .archived marker (valid — ARCHIVED event is present)
    marker_path = ticket_dir / ".archived"
    marker_path.write_text("")

    # Call reduce_all_tickets — self-heal must NOT fire for a valid marker
    results = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    # Valid marker must remain untouched
    assert marker_path.exists(), (
        "reduce_all_tickets() must NOT remove a valid .archived marker when a "
        "*-ARCHIVED.json event file is present; marker was incorrectly removed"
    )

    # Ticket must be absent from results (fast-skip still applies to valid marker)
    returned_ids = [r.get("ticket_id") for r in results]
    assert "tkt-valid-marker" not in returned_ids, (
        "Ticket with valid .archived marker must be excluded when exclude_archived=True; "
        f"got returned_ids={returned_ids}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_orphan_marker_cache_miss_on_self_heal(tmp_path: Path, reducer: ModuleType) -> None:
    """Invalidate cached marker state after orphan repair.

    Both reductions must return the ticket as active.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-orphan-cache"
    ticket_dir.mkdir()

    # Write only a CREATE event (active ticket)
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Cache miss after self-heal"},
    )

    # Write stale .archived marker (no ARCHIVED event)
    marker_path = ticket_dir / ".archived"
    marker_path.write_text("")

    # First call — self-heal fires, marker removed, slow path returns active state
    results_first = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    assert not marker_path.exists(), (
        "First reduce_all_tickets() call must remove the orphan .archived marker "
        "(self-heal not implemented)"
    )

    ids_first = [r.get("ticket_id") for r in results_first]
    assert "tkt-orphan-cache" in ids_first, (
        f"First call must return the ticket after orphan self-heal; got ids_first={ids_first}"
    )

    # Second call — cache must NOT serve stale archived=True; marker is absent now
    results_second = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    ids_second = [r.get("ticket_id") for r in results_second]
    assert "tkt-orphan-cache" in ids_second, (
        "Second reduce_all_tickets() call must also return the ticket as active "
        "after orphan self-heal (cache must reflect marker absence); "
        f"got ids_second={ids_second}"
    )

    # Both calls must agree on active (non-archived) state
    state_second = next(r for r in results_second if r.get("ticket_id") == "tkt-orphan-cache")
    assert not state_second.get("archived"), (
        "Second call: ticket must not have archived=True after orphan self-heal; "
        f"got state['archived']={state_second.get('archived')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hash_differs_after_marker_removal(tmp_path: Path) -> None:
    """Restore the original directory hash after removing the marker."""
    ticket_dir = tmp_path / "tkt-marker-removed"
    ticket_dir.mkdir()

    event_file = ticket_dir / f"1742605200-{_UUID}-CREATE.json"
    event_file.write_text(
        json.dumps(
            {
                "timestamp": 1742605200,
                "uuid": _UUID,
                "event_type": "CREATE",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Alice",
                "data": {"ticket_type": "task", "title": "Marker removal hash test"},
            }
        )
    )

    ticket_dir_str = str(ticket_dir)
    event_filenames = [event_file.name]

    # Baseline hash — no marker
    hash_baseline = _compute_dir_hash(ticket_dir_str, event_filenames)

    # Write .archived marker (simulates write_marker())
    marker_path = ticket_dir / ".archived"
    marker_path.touch()

    hash_with_marker = _compute_dir_hash(ticket_dir_str, event_filenames)

    # Remove .archived marker (simulates remove_marker())
    marker_path.unlink()

    hash_after_removal = _compute_dir_hash(ticket_dir_str, event_filenames)

    assert hash_with_marker != hash_after_removal, (
        "compute_dir_hash() must return a different hash after .archived marker removal"
    )
    assert hash_after_removal == hash_baseline, (
        "compute_dir_hash() hash after marker removal must equal the original "
        f"baseline hash (symmetric); baseline={hash_baseline!r}, "
        f"after_removal={hash_after_removal!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reverted_archived_marker_is_orphan(tmp_path: Path, reducer: ModuleType) -> None:
    """Treat a marker as orphaned when REVERT cancels its ARCHIVED event.

    Self-heal removes the marker before the slow reduction path.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-reverted-archive"
    ticket_dir.mkdir()

    archived_uuid = _UUID2

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Ticket archived then un-archived"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=archived_uuid,
        event_type="ARCHIVED",
        data={},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_UUID3,
        event_type="REVERT",
        data={
            "target_event_uuid": archived_uuid,
            "target_event_type": "ARCHIVED",
            "reason": "",
        },
    )

    marker_path = ticket_dir / ".archived"
    marker_path.write_text("")

    # Self-heal must fire and remove the stale marker (REVERT cancels ARCHIVED)
    reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    assert not marker_path.exists(), (
        "reduce_all_tickets() must remove the .archived marker when the ARCHIVED event "
        "has been cancelled by a REVERT (net non-archived state); marker not removed"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_revert_of_archived_unarchives_state(reducer: ModuleType, tmp_path: Path) -> None:
    """Bug vocal-jig-apron: reverting an ARCHIVED event must un-archive the
    COMPILED STATE (not just remove the marker). Replay of CREATE + ARCHIVED +
    REVERT(target=ARCHIVED) must yield archived=False, status=open, and the
    ticket must reappear in the default (exclude_archived) projection.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()
    ticket_dir = tracker_dir / "tkt-unarchive"
    ticket_dir.mkdir()
    archived_uuid = _UUID2

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "archived then reverted"},
    )
    _write_event(
        ticket_dir, timestamp=1742605300, uuid=archived_uuid, event_type="ARCHIVED", data={}
    )
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_UUID3,
        event_type="REVERT",
        data={"target_event_uuid": archived_uuid, "target_event_type": "ARCHIVED", "reason": ""},
    )

    state = reducer.reduce_ticket(str(ticket_dir))
    assert state["archived"] is False, f"revert must clear archived; got {state!r}"
    assert state["status"] == "open", (
        f"revert of ARCHIVED must restore open; got {state['status']!r}"
    )

    visible = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)
    assert any(t.get("ticket_id") == "tkt-unarchive" for t in visible), (
        "un-archived ticket must reappear in the default exclude_archived projection"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_revert_of_archived_does_not_resurrect_deleted(reducer: ModuleType, tmp_path: Path) -> None:
    """Bug vocal-jig-apron (guard): a DELETED ticket (delete writes
    STATUS(deleted)+ARCHIVED) must NOT be resurrected to open by reverting the
    ARCHIVED event — its terminal deleted status wins (process_archived never set
    status=archived for it)."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()
    ticket_dir = tracker_dir / "tkt-deleted"
    ticket_dir.mkdir()
    archived_uuid = _UUID2

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "deleted ticket"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605250,
        uuid=_UUID3,
        event_type="STATUS",
        data={"status": "deleted", "current_status": "open"},
    )
    _write_event(
        ticket_dir, timestamp=1742605300, uuid=archived_uuid, event_type="ARCHIVED", data={}
    )
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid="cafef00d-dead-beef-dead-beefcafef00d",
        event_type="REVERT",
        data={"target_event_uuid": archived_uuid, "target_event_type": "ARCHIVED", "reason": ""},
    )

    state = reducer.reduce_ticket(str(ticket_dir))
    assert state["status"] == "deleted", f"deleted must NOT be resurrected; got {state['status']!r}"
    # Review H1: reverting the ARCHIVED of a deleted ticket must also leave it
    # archived=True so it stays HIDDEN — the default list excludes archived but
    # not deleted, so clearing archived would resurrect it into the listing.
    assert state["archived"] is True, (
        f"deleted ticket must stay archived (hidden) after revert; "
        f"got archived={state.get('archived')!r}"
    )
    visible = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)
    assert not any(t.get("ticket_id") == "tkt-deleted" for t in visible), (
        "deleted ticket must NOT reappear in the default exclude_archived projection after revert"
    )
