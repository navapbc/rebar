"""LINK / UNLINK event handling, same-second ordering, alias-target normalization

Split from the former monolithic tests/scripts/test_ticket_reducer.py along
reducer-concern seams. The module-under-test fixture (`reducer`) lives in
conftest.py; event-writing helpers (`_write_event`, `_UUID*`) in _events.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, REPO_ROOT, _write_event

# ---------------------------------------------------------------------------
# Tests: LINK / UNLINK event handling (dso-vwoo)
# These tests MUST FAIL until ticket-reducer.py is extended to handle LINK/UNLINK.
# ---------------------------------------------------------------------------

_LINK_UUID = "11112222-3333-4444-5555-666677778888"
_LINK_UUID2 = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
_LINK_UUID3 = "deadd00d-1234-5678-9abc-def012345678"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_compiles_link_event_into_deps_list(tmp_path: Path, reducer: ModuleType) -> None:
    """A single LINK event with relation=blocks and target_id=tkt-002 results in
    state['deps'] containing exactly one entry with those fields plus link_uuid."""
    ticket_dir = tmp_path / "tkt-link-single"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Link test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert "deps" in state, "state must have a 'deps' key"
    assert len(state["deps"]) == 1, (
        f"Expected 1 dep entry, got {len(state['deps'])}: {state['deps']}"
    )
    dep = state["deps"][0]
    assert dep["target_id"] == "tkt-002", (
        f"Expected target_id='tkt-002', got {dep.get('target_id')!r}"
    )
    assert dep["relation"] == "blocks", f"Expected relation='blocks', got {dep.get('relation')!r}"
    assert dep["link_uuid"] == _LINK_UUID, (
        f"Expected link_uuid={_LINK_UUID!r}, got {dep.get('link_uuid')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_link_event_with_target_key_instead_of_target_id(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """LINK events using 'target' key (legacy format) are accepted and normalized
    to 'target_id' in the compiled state. All 112 existing LINK events on disk use
    'target' rather than 'target_id'. Fix for ticket 9e0f-0828."""
    ticket_dir = tmp_path / "tkt-link-legacy"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Legacy link test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "depends_on", "target": "tkt-legacy-001"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert len(state["deps"]) == 1, f"Expected 1 dep, got {len(state['deps'])}"
    dep = state["deps"][0]
    assert dep["target_id"] == "tkt-legacy-001", (
        f"Expected target_id='tkt-legacy-001', got {dep.get('target_id')!r}"
    )
    assert dep["relation"] == "depends_on"
    assert dep["link_uuid"] == _LINK_UUID


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_compiles_multiple_link_events(tmp_path: Path, reducer: ModuleType) -> None:
    """Two LINK events produce two independent entries in state['deps']."""
    ticket_dir = tmp_path / "tkt-link-multi"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Multi-link test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_LINK_UUID2,
        event_type="LINK",
        data={"relation": "depends_on", "target_id": "tkt-003"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["deps"]) == 2, (
        f"Expected 2 dep entries, got {len(state['deps'])}: {state['deps']}"
    )
    link_uuids = {d["link_uuid"] for d in state["deps"]}
    assert _LINK_UUID in link_uuids, "First LINK uuid must be in deps"
    assert _LINK_UUID2 in link_uuids, "Second LINK uuid must be in deps"
    target_ids = {d["target_id"] for d in state["deps"]}
    assert "tkt-002" in target_ids
    assert "tkt-003" in target_ids


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_unlink_event_removes_dep_entry(tmp_path: Path, reducer: ModuleType) -> None:
    """A LINK event followed by an UNLINK event with matching link_uuid removes
    the dep entry — state['deps'] is empty after the UNLINK.

    We verify using two ticket dirs: one with LINK only (must show 1 dep) and one
    with LINK + UNLINK (must show 0 deps). This ensures the test cannot pass unless
    LINK events are actually processed.
    """
    # Dir A: LINK only — must produce 1 dep (proves LINK is processed)
    dir_link_only = tmp_path / "tkt-unlink-removes-link-only"
    dir_link_only.mkdir()

    _write_event(
        dir_link_only,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Unlink removes test", "parent_id": None},
    )
    _write_event(
        dir_link_only,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )

    state_link_only = reducer.reduce_ticket(dir_link_only)
    assert state_link_only is not None
    assert len(state_link_only["deps"]) == 1, (
        f"Precondition: LINK-only dir must have 1 dep, got {state_link_only['deps']}"
    )

    # Dir B: LINK + UNLINK — dep must be removed
    dir_unlinked = tmp_path / "tkt-unlink-removes"
    dir_unlinked.mkdir()

    _write_event(
        dir_unlinked,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Unlink removes test", "parent_id": None},
    )
    _write_event(
        dir_unlinked,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )
    _write_event(
        dir_unlinked,
        timestamp=1742605400,
        uuid=_LINK_UUID2,
        event_type="UNLINK",
        data={"link_uuid": _LINK_UUID},
    )

    state = reducer.reduce_ticket(dir_unlinked)

    assert state is not None
    assert state["deps"] == [], f"Expected empty deps after UNLINK, got {state['deps']}"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_unlink_unknown_uuid_is_noop(tmp_path: Path, reducer: ModuleType) -> None:
    """An UNLINK event referencing an unknown link_uuid does not crash and leaves
    state['deps'] unchanged."""
    ticket_dir = tmp_path / "tkt-unlink-noop"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Unlink noop test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )
    # UNLINK with a uuid that was never linked
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_LINK_UUID2,
        event_type="UNLINK",
        data={"link_uuid": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must not raise on unknown UNLINK uuid"
    assert len(state["deps"]) == 1, (
        f"Existing dep must remain after UNLINK with unknown uuid, got {state['deps']}"
    )
    assert state["deps"][0]["link_uuid"] == _LINK_UUID


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_link_events_survive_snapshot(tmp_path: Path, reducer: ModuleType) -> None:
    """A LINK event included in a SNAPSHOT's compiled_state.deps, plus one new LINK
    event after the snapshot, both appear in the final state['deps']."""
    ticket_dir = tmp_path / "tkt-link-snapshot"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Link snapshot test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )

    # SNAPSHOT captures deps from the LINK above
    snapshot_payload = {
        "timestamp": 1742605400,
        "uuid": "snap-link-uuid-abcd",
        "event_type": "SNAPSHOT",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test",
        "data": {
            "compiled_state": {
                "ticket_id": "tkt-link-snapshot",
                "ticket_type": "task",
                "title": "Link snapshot test",
                "status": "open",
                "author": "Test User",
                "created_at": 1742605200,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "parent_id": None,
                "comments": [],
                "deps": [
                    {
                        "target_id": "tkt-002",
                        "relation": "blocks",
                        "link_uuid": _LINK_UUID,
                    }
                ],
            },
            "source_event_uuids": [_UUID, _LINK_UUID],
        },
    }
    (ticket_dir / "1742605400-snap-link-uuid-abcd-SNAPSHOT.json").write_text(
        json.dumps(snapshot_payload)
    )

    # New LINK event after the snapshot
    _write_event(
        ticket_dir,
        timestamp=1742605500,
        uuid=_LINK_UUID2,
        event_type="LINK",
        data={"relation": "depends_on", "target_id": "tkt-003"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["deps"]) == 2, (
        f"Expected 2 deps (one from snapshot, one post-snapshot LINK), got {state['deps']}"
    )
    link_uuids = {d["link_uuid"] for d in state["deps"]}
    assert _LINK_UUID in link_uuids, "Dep from snapshot must be preserved"
    assert _LINK_UUID2 in link_uuids, "Post-snapshot LINK must be appended"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_deps_in_snapshot_not_duplicated(tmp_path: Path, reducer: ModuleType) -> None:
    """A LINK event listed in a SNAPSHOT's source_event_uuids is not double-counted;
    its dep entry comes only from the compiled_state, not re-applied from the raw event."""
    ticket_dir = tmp_path / "tkt-link-nodupe"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "No-dup link test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )

    # SNAPSHOT includes the LINK event in source_event_uuids — reducer must not
    # re-apply the LINK raw event (it is already captured in compiled_state.deps)
    snapshot_payload = {
        "timestamp": 1742605400,
        "uuid": "snap-nodupe-uuid-abcd",
        "event_type": "SNAPSHOT",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test",
        "data": {
            "compiled_state": {
                "ticket_id": "tkt-link-nodupe",
                "ticket_type": "task",
                "title": "No-dup link test",
                "status": "open",
                "author": "Test User",
                "created_at": 1742605200,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "parent_id": None,
                "comments": [],
                "deps": [
                    {
                        "target_id": "tkt-002",
                        "relation": "blocks",
                        "link_uuid": _LINK_UUID,
                    }
                ],
            },
            "source_event_uuids": [_UUID, _LINK_UUID],
        },
    }
    (ticket_dir / "1742605400-snap-nodupe-uuid-abcd-SNAPSHOT.json").write_text(
        json.dumps(snapshot_payload)
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["deps"]) == 1, (
        f"Dep must appear exactly once (no double-count), got {state['deps']}"
    )
    assert state["deps"][0]["link_uuid"] == _LINK_UUID
    assert state["ticket_id"] == "tkt-link-nodupe"


# ---------------------------------------------------------------------------
# same-second LINK + UNLINK sort order (dso-jwan)
# LINK must always replay before UNLINK at the same Unix-second timestamp,
# even when the UNLINK filename UUID sorts alphabetically before the LINK UUID.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_same_second_link_unlink_sort_order(reducer: ModuleType, tmp_path: Path) -> None:
    """When LINK and UNLINK share the same Unix-second timestamp, LINK must
    replay before UNLINK so the dep is correctly cancelled.

    Bug scenario (dso-jwan): If filenames sort lexicographically as
    UNLINK < LINK (because UNLINK's UUID precedes LINK's UUID alphabetically),
    the reducer processes UNLINK first — the link_uuid is not yet in deps,
    so UNLINK is a no-op, then LINK adds the dep. The dep appears active when
    it should be cancelled.

    Fix: sort key must be (timestamp_segment, event_type_order, full_name)
    with LINK=0, UNLINK=1, so LINK always processes before UNLINK at the same
    second.
    """
    ticket_dir = tmp_path / "tkt-same-sec"
    ticket_dir.mkdir()

    ts = 1700000000
    link_uuid = "ffff1111-2222-3333-4444-555566667777"  # sorts HIGH alphabetically
    unlink_uuid = "aaaa9999-8888-7777-6666-555544443333"  # sorts LOW alphabetically

    # Write CREATE event
    _write_event(
        ticket_dir,
        timestamp=ts - 10,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Same-second sort test"},
    )

    # Write LINK event: link_uuid sorts HIGH → filename e.g. 1700000000-ffff1111-...-LINK.json
    _write_event(
        ticket_dir,
        timestamp=ts,
        uuid=link_uuid,
        event_type="LINK",
        data={"target_id": "tkt-target", "relation": "blocks"},
    )

    # Write UNLINK event: unlink_uuid sorts LOW → filename e.g. 1700000000-aaaa9999-...-UNLINK.json
    # Lexicographic sort would put UNLINK before LINK (aaaa < ffff), causing the bug.
    _write_event(
        ticket_dir,
        timestamp=ts,
        uuid=unlink_uuid,
        event_type="UNLINK",
        data={"link_uuid": link_uuid},
    )

    state = reducer.reduce_ticket(str(ticket_dir))

    assert state is not None, "reduce_ticket returned None"
    assert isinstance(state, dict), f"Expected dict, got {type(state)}"
    assert state["deps"] == [], (
        f"Expected empty deps after same-second LINK+UNLINK (UNLINK cancels LINK), "
        f"got {state['deps']!r}. "
        "This indicates UNLINK was processed before LINK (lexicographic sort bug)."
    )


# ---------------------------------------------------------------------------
# Test: LINK event alias target normalizes to canonical UUID (bug 8fc3-d3b1)
# ---------------------------------------------------------------------------

_ALIAS_TARGET_UUID = "abcd-1234-5678-abcd"
_ALIAS_LINK_UUID = "bbbb-cccc-dddd-eeee-ffff-0000-1111-2222"
_ALIAS_SOURCE_UUID = "1111-2222-3333-4444-5555-6666-7777-8888"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_link_event_alias_target_normalizes_to_canonical_uuid(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """LINK event with a short-hex (alias-form) target_id is resolved to the full
    canonical UUID during reduce_ticket when tracker_dir context is available.

    Without the fix: process_link stores the verbatim short-hex value ("abcd-1234")
    so deps[0]["target_id"] == "abcd-1234" (not the canonical UUID).

    With the fix: reduce_ticket derives tracker_dir and passes it through
    replay_events -> process_link -> resolve_ticket_id, so the stored
    target_id equals the canonical UUID "abcd-1234-5678-abcd".

    RED before fix: assertion `dep['target_id'] == 'abcd-1234-5678-abcd'` fails
    because target_id is still "abcd-1234".
    """
    # Build a 2-ticket tracker
    tracker_dir = tmp_path / "tracker-alias-resolve"
    tracker_dir.mkdir()

    # Target ticket: full canonical-UUID directory
    target_ticket_dir = tracker_dir / _ALIAS_TARGET_UUID
    target_ticket_dir.mkdir()
    _write_event(
        target_ticket_dir,
        timestamp=1742600000,
        uuid="ffff-eeee-dddd-cccc",
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Target ticket", "parent_id": None},
    )

    # Source ticket: LINK event with short-hex target (alias form "abcd-1234")
    source_ticket_dir = tracker_dir / "1111-2222-3333-4444"
    source_ticket_dir.mkdir()
    _write_event(
        source_ticket_dir,
        timestamp=1742601000,
        uuid="aaaa-bbbb-cccc-dddd",
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Source ticket", "parent_id": None},
    )
    _write_event(
        source_ticket_dir,
        timestamp=1742601100,
        uuid="1234-5678-9abc-def0",
        event_type="LINK",
        data={"relation": "blocks", "target_id": "abcd-1234"},
    )

    # Pre-assertion gate: confirm ticket_resolver.resolve_ticket_id is importable and
    # actually maps the short-hex alias to the canonical UUID when called directly.
    # This makes a resolver import/resolution failure unambiguous — the test will
    # skip with a clear reason rather than appearing to test the reducer when the
    # underlying resolver path is silently bypassed by the `except Exception` fallback.
    try:
        import importlib
        import sys as _sys

        # Resolve script directory so ticket_resolver is importable from tests.
        _scripts_dir = str(REPO_ROOT / "src" / "rebar" / "_engine")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        _tr = importlib.import_module("ticket_resolver")
        _resolve_fn = getattr(_tr, "resolve_ticket_id", None)
        assert _resolve_fn is not None, (
            "ticket_resolver.resolve_ticket_id not found — "
            "resolver module exists but lacks the expected function"
        )
        # Verify the resolver maps the alias to the canonical UUID against our tracker.
        resolved = _resolve_fn("abcd-1234", str(tracker_dir))
        assert resolved == _ALIAS_TARGET_UUID, (
            f"ticket_resolver.resolve_ticket_id('abcd-1234', tracker_dir) returned "
            f"{resolved!r}; expected {_ALIAS_TARGET_UUID!r}. "
            "Resolver is available but does not resolve the alias correctly — "
            "fix ticket_resolver before this test can be meaningful."
        )
    except ImportError as exc:
        pytest.skip(
            f"ticket_resolver is not importable ({exc}); cannot verify alias-resolution path. "
            "Install/implement ticket_resolver to un-skip this test."
        )

    state = reducer.reduce_ticket(source_ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert "deps" in state, "state must have 'deps' key"
    assert len(state["deps"]) == 1, (
        f"Expected 1 dep entry, got {len(state['deps'])}: {state['deps']}"
    )
    dep = state["deps"][0]
    assert dep["target_id"] == _ALIAS_TARGET_UUID, (
        f"Expected target_id to be resolved to canonical UUID {_ALIAS_TARGET_UUID!r}, "
        f"got {dep.get('target_id')!r}. "
        f"Fix: process_link must resolve short-hex alias to canonical UUID via tracker_dir."
    )
