"""RED tests for ticket-graph.py.

These tests are RED — they test functionality that does not yet exist.
All test functions MUST FAIL before ticket-graph.py is implemented.

The module under test is expected to expose:
    build_dep_graph(ticket_id: str, tracker_dir: str) -> dict
    add_dependency(source_id: str, target_id: str, tracker_dir: str) -> None
    CyclicDependencyError (exception class)

Contract:
  - build_dep_graph returns:
        {"ticket_id": str, "deps": list, "ready_to_work": bool, "blockers": list}
  - ready_to_work=True when all direct blockers are closed (or tombstoned)
  - add_dependency raises CyclicDependencyError for cycles (direct or transitive)
  - Missing blocker directories (archived/tombstoned) are treated as closed
  - .tombstone.json with {"status": "closed"} in a blocker dir → treated as closed
  - Graph results are cached; cache is invalidated when a new LINK event is added

Test: python3 -m pytest tests/scripts/test_ticket_graph.py -x
All tests must return non-zero until ticket-graph.py is implemented.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading — filename has hyphens so we use importlib
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket-graph.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_graph", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def graph() -> ModuleType:
    """Return the ticket-graph module, failing all tests if absent (RED)."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"ticket-graph.py not found at {SCRIPT_PATH} — "
            "this is expected RED state; implement the script to make tests pass."
        )
    return _load_module()


@pytest.fixture(autouse=True)
def _isolate_git_from_enclosing_repo(tmp_path, monkeypatch):
    """Make every test in this module fully hermetic with respect to git.

    ``add_dependency()`` -> ``_write_link_event()`` runs
    ``git -C <tracker> add/commit`` (and a best-effort push). The trackers built
    here are plain directories, not git repos, so without a boundary git walks
    UP and can commit the test's LINK events into whatever repo encloses the
    pytest tmp dir — e.g. the rebar checkout itself when the pytest basetemp is
    nested inside it (the failure mode that once leaked ``ticket: link ...``
    commits onto main).

    Pin ``GIT_CEILING_DIRECTORIES`` so git can never chdir up out of the
    disposable tmp tree — nor into the rebar checkout — while searching for a
    repository. With no enclosing repo reachable, ``git add`` against a non-repo
    tracker fails cleanly (``_write_link_event`` already swallows that) and the
    LINK event file — which is all ``build_dep_graph`` reads — is still written.
    Tests that exercise a real push create their own repo + remote *under*
    ``tmp_path`` (below the ceiling), so they are unaffected.
    """
    import os

    ceilings = os.pathsep.join(
        # de-dupe while preserving order; cover symlinked temp roots (macOS
        # /var -> /private/var) so the ceiling matches git's resolved walk.
        dict.fromkeys(
            [
                str(tmp_path.parent),
                os.path.realpath(tmp_path.parent),
                str(REPO_ROOT),
                os.path.realpath(REPO_ROOT),
            ]
        )
    )
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", ceilings)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_A = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
_UUID_C = "cccccccc-cccc-4ccc-cccc-cccccccccccc"
_UUID_D = "dddddddd-dddd-4ddd-dddd-dddddddddddd"


def _write_ticket(
    tracker_dir: Path,
    ticket_id: str,
    status: str = "open",
    parent_id: str | None = None,
    ticket_type: str = "task",
) -> Path:
    """Write a minimal ticket directory with a CREATE event and optional STATUS event.

    Returns the ticket directory path.
    """
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)

    create_event = {
        "event_type": "CREATE",
        "uuid": f"create-{ticket_id}",
        "timestamp": 1000,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": ticket_type,
            "title": f"Ticket {ticket_id}",
            "parent_id": parent_id,
        },
    }
    with open(ticket_dir / f"1000-create-{ticket_id}-CREATE.json", "w") as f:
        json.dump(create_event, f)

    if status != "open":
        status_event = {
            "event_type": "STATUS",
            "uuid": f"status-{ticket_id}",
            "timestamp": 2000,
            "author": "Test User",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "data": {
                "status": status,
                "current_status": "open",
            },
        }
        with open(ticket_dir / f"2000-status-{ticket_id}-STATUS.json", "w") as f:
            json.dump(status_event, f)

    return ticket_dir


def _write_blocks_link(
    tracker_dir: Path,
    blocker_id: str,
    blocked_id: str,
    link_uuid: str | None = None,
    timestamp: int = 1500,
) -> None:
    """Write a LINK event in blocker_id's directory: blocker_id blocks blocked_id.

    Follows the schema used by ticket-link.sh: LINK event is stored in the
    blocker's directory with data.target_id=blocked_id and data.relation='blocks'.
    """
    if link_uuid is None:
        link_uuid = f"link-{blocker_id}-blocks-{blocked_id}"
    blocker_dir = tracker_dir / blocker_id
    blocker_dir.mkdir(parents=True, exist_ok=True)
    link_event = {
        "event_type": "LINK",
        "uuid": link_uuid,
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": blocked_id,
            "relation": "blocks",
        },
    }
    filename = f"{timestamp}-{link_uuid}-LINK.json"
    with open(blocker_dir / filename, "w") as f:
        json.dump(link_event, f)


# ---------------------------------------------------------------------------
# Graph traversal & ready_to_work
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_ready_to_work_all_blockers_closed(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Ticket B is ready_to_work=True when its only blocker (A) is closed.

    Setup:
        - ticket-a: closed (blocks ticket-b)
        - ticket-b: open

    Expected: build_dep_graph('ticket-b', tracker_dir)['ready_to_work'] == True
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is True, (
        f"Expected ready_to_work=True (blocker ticket-a is closed), got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_ready_to_work_blocker_still_open(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Ticket B is ready_to_work=False when its blocker (A) is still open.

    Setup:
        - ticket-a: open (blocks ticket-b)
        - ticket-b: open

    Expected: build_dep_graph('ticket-b', tracker_dir)['ready_to_work'] == False
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is False, (
        f"Expected ready_to_work=False (blocker ticket-a is still open), got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_ready_to_work_direct_blockers_only(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Ticket B is ready_to_work=False when at least one direct blocker is open.

    Setup:
        - ticket-a: open  (blocks ticket-b)
        - ticket-c: closed (blocks ticket-b)
        - ticket-b: open

    Expected: ready_to_work=False because ticket-a (direct blocker) is still open.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="closed")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)
    _write_blocks_link(tracker_dir, "ticket-c", "ticket-b", timestamp=1501)

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is False, (
        f"Expected ready_to_work=False (ticket-a still open), got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_deps_output_schema(graph: ModuleType, tmp_path: Path) -> None:
    """build_dep_graph returns the expected output schema.

    Expected keys: ticket_id, deps, ready_to_work, blockers
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert "ticket_id" in result, f"Missing 'ticket_id' key in {result!r}"
    assert "deps" in result, f"Missing 'deps' key in {result!r}"
    assert "ready_to_work" in result, f"Missing 'ready_to_work' key in {result!r}"
    assert "blockers" in result, f"Missing 'blockers' key in {result!r}"
    assert result["ticket_id"] == "ticket-b", (
        f"Expected ticket_id='ticket-b', got {result['ticket_id']!r}"
    )
    assert isinstance(result["ready_to_work"], bool), (
        f"ready_to_work must be bool, got {type(result['ready_to_work'])}"
    )
    assert isinstance(result["deps"], list), (
        f"deps must be list, got {type(result['deps'])}"
    )
    assert isinstance(result["blockers"], list), (
        f"blockers must be list, got {type(result['blockers'])}"
    )


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cycle_detection_rejects_direct_cycle(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency raises CyclicDependencyError for a direct cycle A→B, B→A.

    Setup: ticket-a blocks ticket-b already exists.
    Action: add_dependency('ticket-b', 'ticket-a', ...) must raise CyclicDependencyError.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("ticket-b", "ticket-a", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cycle_detection_rejects_transitive_cycle(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency raises CyclicDependencyError for a transitive cycle A→B→C→A.

    Setup: ticket-a blocks ticket-b, ticket-b blocks ticket-c.
    Action: add_dependency('ticket-c', 'ticket-a', ...) must raise CyclicDependencyError.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)
    _write_blocks_link(tracker_dir, "ticket-b", "ticket-c", timestamp=1501)

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("ticket-c", "ticket-a", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cycle_detection_allows_dag(graph: ModuleType, tmp_path: Path) -> None:
    """add_dependency does NOT raise for a valid DAG: A→B, A→C, B→D.

    Setup: ticket-a blocks ticket-b, ticket-a blocks ticket-c, ticket-b blocks ticket-d.
    Action: These are all valid DAG edges — no CyclicDependencyError should be raised.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")
    _write_ticket(tracker_dir, "ticket-d", status="open")

    # Should not raise
    graph.add_dependency("ticket-a", "ticket-b", str(tracker_dir))
    graph.add_dependency("ticket-a", "ticket-c", str(tracker_dir))
    graph.add_dependency("ticket-b", "ticket-d", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_visited_set_prevents_infinite_loop(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Diamond graph (A→B, A→C, B→D, C→D) traverses without infinite recursion.

    Setup:
        - ticket-a blocks ticket-b and ticket-c
        - ticket-b blocks ticket-d
        - ticket-c blocks ticket-d
        - All open

    Expected: build_dep_graph('ticket-d', ...) completes without RecursionError or hang.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")
    _write_ticket(tracker_dir, "ticket-d", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-c", timestamp=1501)
    _write_blocks_link(tracker_dir, "ticket-b", "ticket-d", timestamp=1502)
    _write_blocks_link(tracker_dir, "ticket-c", "ticket-d", timestamp=1503)

    # Must complete without error (visited set prevents re-traversing ticket-a twice)
    result = graph.build_dep_graph("ticket-d", str(tracker_dir))
    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"


# ---------------------------------------------------------------------------
# Tombstone-awareness
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_archived_ticket_treated_as_closed(
    graph: ModuleType, tmp_path: Path
) -> None:
    """A missing blocker directory (archived/tombstoned) is treated as satisfied.

    Setup:
        - ticket-a: directory MISSING (was archived — its dir does not exist)
        - ticket-b: open, has a LINK event (depends_on ticket-a) in ticket-b's own dir

    Since ticket-a's directory is absent, it is treated as closed → ready_to_work=True.

    Note: The LINK event is stored in ticket-b's directory using relation='depends_on'
    with target_id='ticket-a'. This means ticket-b knows it depends on ticket-a, and
    the relationship is discoverable even when ticket-a's directory is absent.
    This tests tombstone resolution: the implementation must treat a missing blocker
    directory as closed, not as an error.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # Write ticket-b with a CREATE event and a LINK event (depends_on ticket-a).
    # The LINK lives in ticket-b's directory — ticket-a's directory is never created.
    ticket_b_dir = tracker_dir / "ticket-b"
    ticket_b_dir.mkdir(parents=True)

    create_event = {
        "event_type": "CREATE",
        "uuid": "create-ticket-b",
        "timestamp": 1000,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": "task",
            "title": "Ticket ticket-b",
            "parent_id": None,
        },
    }
    with open(ticket_b_dir / "1000-create-ticket-b-CREATE.json", "w") as f:
        json.dump(create_event, f)

    # ticket-b depends_on ticket-a: LINK event stored in ticket-b's dir.
    # ticket-a's directory does NOT exist (simulates archival / tombstoned blocker).
    link_event = {
        "event_type": "LINK",
        "uuid": "link-ticket-b-depends_on-ticket-a",
        "timestamp": 1500,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": "ticket-a",
            "relation": "depends_on",
        },
    }
    with open(
        ticket_b_dir / "1500-link-ticket-b-depends_on-ticket-a-LINK.json", "w"
    ) as f:
        json.dump(link_event, f)

    # ticket-a directory intentionally absent (archived/tombstoned)
    assert not (tracker_dir / "ticket-a").exists(), (
        "ticket-a directory must not exist to simulate archival"
    )

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is True, (
        f"Expected ready_to_work=True (blocker ticket-a directory missing = archived), "
        f"got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_tombstone_tombstone_json_respected(
    graph: ModuleType, tmp_path: Path
) -> None:
    """A blocker with .tombstone.json {'status': 'closed'} is treated as closed.

    Setup:
        - ticket-a: directory exists but contains only .tombstone.json
        - ticket-a blocks ticket-b
        - ticket-b: open

    Expected: ready_to_work=True because .tombstone.json signals closed status.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-b", status="open")

    # Create ticket-a directory with only a .tombstone.json
    ticket_a_dir = tracker_dir / "ticket-a"
    ticket_a_dir.mkdir(parents=True)
    tombstone = {"status": "closed", "closed_at": 1700000000}
    with open(ticket_a_dir / ".tombstone.json", "w") as f:
        json.dump(tombstone, f)

    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is True, (
        f"Expected ready_to_work=True (.tombstone.json signals closed), got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_ready_to_work_when_blocker_has_deleted_tombstone(
    graph: ModuleType, tmp_path: Path
) -> None:
    """A blocker with .tombstone.json {'status': 'deleted'} is treated as terminal (satisfied).

    Setup:
        - ticket-a: has CREATE event (so reduce_all_tickets includes it) AND
          .tombstone.json {"status": "deleted"}; blocks ticket-b
        - ticket-b: open

    Expected: ready_to_work=True because a "deleted" tombstone status is terminal.

    Regression test: _graph.py treats "deleted" as a terminal status alongside "closed".
    _get_ticket_status returns "deleted" for ticket-a (reads .tombstone.json),
    and the terminal-status check now recognises "deleted" → blocker is terminal
    → ready_to_work=True.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-b", status="open")

    # Write ticket-a with a CREATE event so it appears in reduce_all_tickets results,
    # then add .tombstone.json with status="deleted" to simulate a deleted ticket.
    # _get_ticket_status reads .tombstone.json first and returns "deleted",
    # but the current check `status != "closed"` fails to recognise "deleted" as terminal.
    _write_ticket(tracker_dir, "ticket-a", status="open")
    ticket_a_dir = tracker_dir / "ticket-a"
    tombstone = {"status": "deleted", "deleted_at": 1700000000}
    with open(ticket_a_dir / ".tombstone.json", "w") as f:
        json.dump(tombstone, f)

    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is True, (
        f"Expected ready_to_work=True (blocker ticket-a has deleted tombstone), "
        f"got {result!r}. A 'deleted' tombstone status must be treated as terminal, "
        f"not as an open blocker."
    )


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_build_1000_tickets_under_2s(graph: ModuleType, tmp_path: Path) -> None:
    """build_dep_graph for the tail of a 1,000-ticket linear chain completes in <2s.

    Setup:
        - 1,000 ticket directories: ticket-0000 through ticket-0999
        - Linear chain: ticket-0000 blocks ticket-0001, ticket-0001 blocks ticket-0002, ...
        - All tickets are closed except the last (ticket-0999)

    Expected: build_dep_graph('ticket-0999', ...) returns in under 2 seconds.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    n = 1000
    for i in range(n):
        tid = f"ticket-{i:04d}"
        status = "closed" if i < n - 1 else "open"
        _write_ticket(tracker_dir, tid, status=status)

    for i in range(n - 1):
        blocker_id = f"ticket-{i:04d}"
        blocked_id = f"ticket-{i + 1:04d}"
        link_uuid = f"link-{i:04d}"
        _write_blocks_link(
            tracker_dir, blocker_id, blocked_id, link_uuid=link_uuid, timestamp=1500 + i
        )

    start = time.monotonic()
    result = graph.build_dep_graph(f"ticket-{n - 1:04d}", str(tracker_dir))
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, (
        f"build_dep_graph took {elapsed:.3f}s for 1,000-ticket chain (limit: 2.0s)"
    )
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cache_invalidated_on_new_link(graph: ModuleType, tmp_path: Path) -> None:
    """Graph cache is invalidated when a new LINK event is added to a ticket.

    Setup:
        - ticket-a: closed (blocks ticket-b)
        - ticket-b: open
        - First call: build_dep_graph('ticket-b') → ready_to_work=True (only blocker closed)
        - Add new blocker: ticket-c (open) blocks ticket-b
        - Second call: build_dep_graph('ticket-b') → ready_to_work=False (new blocker open)

    Expected: second call reflects the new dependency — cache was invalidated.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)

    # First call — ticket-b has one closed blocker → ready_to_work=True
    first_result = graph.build_dep_graph("ticket-b", str(tracker_dir))
    assert first_result["ready_to_work"] is True, (
        f"Pre-condition failed: expected ready_to_work=True before adding new blocker, "
        f"got {first_result!r}"
    )

    # Add a new open blocker
    _write_ticket(tracker_dir, "ticket-c", status="open")
    _write_blocks_link(tracker_dir, "ticket-c", "ticket-b", timestamp=1600)

    # Second call — cache must be invalidated; new blocker (open) detected
    second_result = graph.build_dep_graph("ticket-b", str(tracker_dir))
    assert second_result["ready_to_work"] is False, (
        f"Expected ready_to_work=False after adding open blocker ticket-c, "
        f"got {second_result!r}. Cache may not have been invalidated."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cache_key_invalidated_on_same_size_rewrite(tmp_path: Path) -> None:
    """Bug zonal-folly-ditch (sibling of reducer bug 1d76): the graph cache key
    must fold in mtime so a same-BYTE-LENGTH in-place rewrite of an event file
    (git checkout/rebase of the tickets branch, fsck-recover cherry-pick)
    invalidates the cache — filename+size alone cannot see it and would serve a
    stale graph through deps/ready/next-batch.
    """
    import os

    from ticket_graph._cache import _compute_cache_key

    tracker = tmp_path / "tracker"
    (tracker / "0000-aaaa-bbbb-cccc").mkdir(parents=True)
    ev = tracker / "0000-aaaa-bbbb-cccc" / "0000-create.json"
    ev.write_text('{"event_type":"CREATE","data":{"title":"AAAA"}}')

    key1 = _compute_cache_key(str(tracker))
    # Unchanged dir → cache still hits (key stable; no read-path regression).
    assert _compute_cache_key(str(tracker)) == key1

    body = ev.read_text()
    st = ev.stat()
    ev.write_text(body.replace("AAAA", "BBBB"))  # same byte length, new content
    assert len(ev.read_text()) == len(body), "rewrite must be same byte length"
    # Simulate a checkout/rebase that bumps mtime without changing size.
    os.utime(ev, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    key2 = _compute_cache_key(str(tracker))
    assert key2 != key1, (
        "same-size in-place rewrite must invalidate the graph cache key "
        "(else deps/ready/next-batch serve stale graph state)"
    )


# ---------------------------------------------------------------------------
# Same-second LINK/UNLINK timestamp ordering — _is_active_link must not allow
# UNLINK to replay before LINK when they share the same Unix-second timestamp
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_is_active_link_same_second_unlink_sorts_after_link(
    graph: ModuleType, tmp_path: Path
) -> None:
    """_is_active_link correctly handles LINK+UNLINK events that share the same Unix-second timestamp.

    When a LINK and its cancelling UNLINK share the same timestamp second but have
    different random UUIDs, a pure alphabetic filename sort can place the UNLINK before
    the LINK — making the link appear active when it has been cancelled.

    This test crafts filenames where the UNLINK UUID sorts alphabetically before the LINK UUID
    at the same timestamp, directly exercising the sort-order bug.

    Expected: _is_active_link returns False (link is net-inactive after the UNLINK).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "src-ticket", status="open")
    _write_ticket(tracker_dir, "tgt-ticket", status="open")

    src_dir = tracker_dir / "src-ticket"

    # The link UUID embedded in the LINK event (and referenced by UNLINK's link_uuid)
    link_uuid = "ffffffff-ffff-4fff-ffff-ffffffffffff"
    # UNLINK UUID starts with '00000000...' → sorts before LINK UUID alphabetically
    unlink_uuid = "00000000-0000-4000-8000-000000000000"
    same_ts = 1000000000

    # Craft filenames so UNLINK sorts before LINK at the same timestamp
    #   UNLINK: "1000000000-00000000-...-UNLINK.json"   ← sorts first alphabetically
    #   LINK:   "1000000000-ffffffff-...-LINK.json"     ← sorts second alphabetically
    link_filename = f"{same_ts}-{link_uuid}-LINK.json"
    unlink_filename = f"{same_ts}-{unlink_uuid}-UNLINK.json"

    # Verify our crafted names actually produce the bad sort order (pre-condition)
    assert unlink_filename < link_filename, (
        "Pre-condition failed: UNLINK filename must sort before LINK filename to exercise the bug. "
        f"Got unlink={unlink_filename!r}, link={link_filename!r}"
    )

    # Write LINK event (link_uuid in 'uuid' field)
    link_event = {
        "event_type": "LINK",
        "uuid": link_uuid,
        "timestamp": same_ts,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": "tgt-ticket",
            "relation": "blocks",
        },
    }
    with open(src_dir / link_filename, "w") as f:
        json.dump(link_event, f)

    # Write UNLINK event (references link_uuid via data.link_uuid)
    unlink_event = {
        "event_type": "UNLINK",
        "uuid": unlink_uuid,
        "timestamp": same_ts,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "link_uuid": link_uuid,
            "target_id": "tgt-ticket",
            "relation": "blocks",
        },
    }
    with open(src_dir / unlink_filename, "w") as f:
        json.dump(unlink_event, f)

    # _is_active_link must return False: the UNLINK cancels the LINK, net state = inactive
    # With the bug: returns True (UNLINK replayed before LINK → LINK appears active again)
    # With the fix: returns False (LINK always replays before UNLINK at same timestamp)
    result = graph._is_active_link(
        "src-ticket", "tgt-ticket", "blocks", str(tracker_dir)
    )
    assert result is False, (
        "_is_active_link returned True but the link was cancelled by an UNLINK event. "
        "This indicates same-second UNLINK is sorting before LINK — the timestamp "
        "tie-breaker (event_type_order: LINK=0, UNLINK=1) is missing or incorrect."
    )


# ---------------------------------------------------------------------------
# Parent-child (children) tests — bug 8cbf-e13b
# ---------------------------------------------------------------------------


def test_build_dep_graph_includes_children(graph: ModuleType, tmp_path: Path) -> None:
    """build_dep_graph must return a 'children' field listing tickets whose
    parent_id matches the queried ticket.

    Bug 8cbf-e13b: ticket deps returns empty deps for epics with parent-linked
    children because it only traverses dependency links, not parent_id.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # Create an epic
    _write_ticket(tracker_dir, "epic-001", ticket_type="epic")
    # Create 3 child stories with parent_id pointing to the epic
    _write_ticket(tracker_dir, "story-a", parent_id="epic-001", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-001", ticket_type="story")
    _write_ticket(tracker_dir, "story-c", parent_id="epic-001", ticket_type="story")
    # Create an unrelated ticket (no parent)
    _write_ticket(tracker_dir, "unrelated")

    result = graph.build_dep_graph("epic-001", str(tracker_dir))

    assert "children" in result, (
        "build_dep_graph result is missing 'children' field — "
        "parent-child relationships are not included in the graph output"
    )
    children = sorted(result["children"])
    assert children == ["story-a", "story-b", "story-c"], (
        f"Expected 3 children [story-a, story-b, story-c], got {children}"
    )


def test_build_dep_graph_children_empty_when_no_children(
    graph: ModuleType, tmp_path: Path
) -> None:
    """build_dep_graph must return an empty 'children' list when no tickets
    have parent_id matching the queried ticket."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "lonely-ticket")
    _write_ticket(tracker_dir, "other-ticket")

    result = graph.build_dep_graph("lonely-ticket", str(tracker_dir))

    assert "children" in result, "build_dep_graph result missing 'children' field"
    assert result["children"] == [], (
        f"Expected empty children, got {result['children']}"
    )


# ---------------------------------------------------------------------------
# Archive exclusion — RED tests (feature not yet implemented)
# ---------------------------------------------------------------------------


def _write_archive_event(
    tracker_dir: Path, ticket_id: str, timestamp: int = 3000
) -> None:
    """Write an ARCHIVED event to ticket_id's directory.

    This marks the ticket as archived in the event-sourced state.
    The ticket-reducer.py handles ARCHIVED events by setting state['archived'] = True.
    """
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    archive_event = {
        "event_type": "ARCHIVED",
        "uuid": f"archive-{ticket_id}",
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {},
    }
    with open(ticket_dir / f"{timestamp}-archive-{ticket_id}-ARCHIVED.json", "w") as f:
        json.dump(archive_event, f)


@pytest.mark.unit
@pytest.mark.scripts
def test_compute_archive_eligible_regression(graph: ModuleType, tmp_path: Path) -> None:
    """compute_archive_eligible must still see ALL tickets (including archived) — regression guard.

    GREEN: This test passes today and must continue passing after archived exclusion
    is implemented. Placed BEFORE the RED marker so regressions are caught, not tolerated.

    Setup:
        - ticket-already-archived: closed + ARCHIVED event (already archived)
        - ticket-eligible: closed, no blockers, no dependents (should be eligible)
        - ticket-open: open (seed for BFS — not eligible itself)

    Expected:
        compute_archive_eligible returns ticket-eligible (not ticket-already-archived,
        since it's already archived).
    """
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        # ticket-already-archived: closed and already archived
        _write_ticket(tracker_dir, "ticket-already-archived", status="closed")
        _write_archive_event(tracker_dir, "ticket-already-archived")

        # ticket-eligible: closed, not archived, no open deps — should be eligible
        _write_ticket(tracker_dir, "ticket-eligible", status="closed")

        # ticket-open: open, not linked to anything
        _write_ticket(tracker_dir, "ticket-open", status="open")

        eligible = graph.compute_archive_eligible(str(tracker_dir))

        assert "ticket-eligible" in eligible, (
            f"ticket-eligible should be archive-eligible; got {eligible}. "
            "compute_archive_eligible must still scan all tickets including archived ones."
        )

        assert "ticket-already-archived" not in eligible, (
            f"ticket-already-archived is already archived, must not be re-eligible; "
            f"got {eligible}"
        )

        assert "ticket-open" not in eligible, (
            f"ticket-open is not closed, must not be eligible; got {eligible}"
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_transitive_traversal_includes_archived_midchain(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Transitive blocker traversal must NOT skip archived tickets mid-chain — regression guard.

    GREEN: This test passes today and must continue passing after archived exclusion
    is implemented. Placed BEFORE the RED marker so regressions are caught, not tolerated.

    Setup:
        - ticket-a: open, blocks ticket-b
        - ticket-b: open, ARCHIVED, blocks ticket-c
        - ticket-c: open (the ticket we query)

    Expected: check_would_create_cycle('ticket-c', 'ticket-a', 'blocks', ...) == True
    """
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        _write_ticket(tracker_dir, "ticket-a", status="open")
        _write_ticket(tracker_dir, "ticket-b", status="open")
        _write_ticket(tracker_dir, "ticket-c", status="open")
        _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)
        _write_blocks_link(tracker_dir, "ticket-b", "ticket-c", timestamp=1501)
        _write_archive_event(tracker_dir, "ticket-b")

        would_cycle = graph.check_would_create_cycle(
            "ticket-c", "ticket-a", "blocks", str(tracker_dir)
        )

        assert would_cycle is True, (
            "check_would_create_cycle must detect cycle through archived mid-chain ticket-b. "
            "Archived exclusion must NOT prune nodes during transitive traversal. "
            f"Got would_cycle={would_cycle!r} (expected True)."
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_check_would_create_cycle_no_false_positive_for_transitive_depends_on(
    graph: ModuleType, tmp_path: Path
) -> None:
    """check_would_create_cycle must NOT raise for a redundant transitive depends_on edge.

    Regression guard for bug 122a-6b11: when A depends_on C and C depends_on B,
    the edge A depends_on B is a redundant transitive edge — not a cycle.
    The previous implementation incorrectly flagged this as a cycle because A is
    reachable from B by traversing depends_on edges backward (B←C←A), which is
    the reverse direction and does not constitute a cycle.

    Setup:
        - ticket-a: open
        - ticket-b: open
        - ticket-c: open
        - ticket-a depends_on ticket-c  (A needs C)
        - ticket-c depends_on ticket-b  (C needs B)

    Action: check_would_create_cycle('ticket-a', 'ticket-b', 'depends_on', ...)
            must return False — adding A depends_on B is redundant but not a cycle.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")

    # ticket-a depends_on ticket-c: LINK in ticket-a's dir
    _write_link_event(
        "ticket-a", "ticket-c", "depends_on", str(tracker_dir), timestamp=1500
    )
    # ticket-c depends_on ticket-b: LINK in ticket-c's dir
    _write_link_event(
        "ticket-c", "ticket-b", "depends_on", str(tracker_dir), timestamp=1501
    )

    # Redundant transitive edge: A→B is already implied by A→C→B, but is NOT a cycle.
    would_cycle = graph.check_would_create_cycle(
        "ticket-a", "ticket-b", "depends_on", str(tracker_dir)
    )

    assert would_cycle is False, (
        "check_would_create_cycle returned True for a redundant transitive depends_on edge "
        "(ticket-a→ticket-b when ticket-a→ticket-c→ticket-b already exists). "
        "This is not a cycle — it is a redundant edge that should be allowed. "
        "Bug 122a-6b11: the check was traversing depends_on edges backward, "
        "incorrectly treating A as reachable from B."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_allows_redundant_transitive_depends_on(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency must NOT raise CyclicDependencyError for a redundant transitive edge.

    Regression guard for bug 122a-6b11: ticket link <A> <B> depends_on was
    rejected with 'would create a cycle' when A→C→B already existed.

    Setup:
        - ticket-a, ticket-b, ticket-c: all open
        - ticket-a depends_on ticket-c
        - ticket-c depends_on ticket-b

    Action: add_dependency('ticket-a', 'ticket-b', tracker_dir, 'depends_on')
            must NOT raise — the edge is redundant but valid.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")

    _write_link_event(
        "ticket-a", "ticket-c", "depends_on", str(tracker_dir), timestamp=1500
    )
    _write_link_event(
        "ticket-c", "ticket-b", "depends_on", str(tracker_dir), timestamp=1501
    )

    # Must not raise CyclicDependencyError — redundant transitive edge is allowed
    try:
        graph.add_dependency(
            "ticket-a", "ticket-b", str(tracker_dir), relation="depends_on"
        )
    except graph.CyclicDependencyError as exc:
        pytest.fail(
            f"add_dependency raised CyclicDependencyError for a redundant transitive "
            f"depends_on edge (ticket-a→ticket-b, with ticket-a→ticket-c→ticket-b already "
            f"existing). This is bug 122a-6b11. Error: {exc}"
        )


# ── RED MARKER BOUNDARY ──────────────────────────────────────────────────────
# Tests below this line are expected to FAIL (RED) until archived exclusion is
# implemented in ticket-graph.py. The .test-index RED marker points to the first
# test below (test_build_dep_graph_excludes_archived_children).
# Tests ABOVE this line are GREEN regression guards that must always pass.


@pytest.mark.unit
@pytest.mark.scripts
def test_build_dep_graph_excludes_archived_children(
    graph: ModuleType, tmp_path: Path
) -> None:
    """build_dep_graph must exclude archived tickets from the children list by default.

    Setup:
        - epic-001: open epic
        - story-active: open, parent_id=epic-001 (not archived)
        - story-archived: open, parent_id=epic-001, then ARCHIVED event written

    Expected (default exclude_archived=True):
        result['children'] contains only story-active, not story-archived.

    This test is RED — archived exclusion is not yet implemented.
    To make it GREEN: add exclude_archived parameter to build_dep_graph
    (default True) and filter children by archived status.
    """
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        _write_ticket(tracker_dir, "epic-001", ticket_type="epic")
        _write_ticket(
            tracker_dir, "story-active", parent_id="epic-001", ticket_type="story"
        )
        _write_ticket(
            tracker_dir, "story-archived", parent_id="epic-001", ticket_type="story"
        )
        _write_archive_event(tracker_dir, "story-archived")

        result = graph.build_dep_graph("epic-001", str(tracker_dir))

        assert "children" in result, "build_dep_graph result missing 'children' field"
        assert "story-active" in result["children"], (
            f"story-active should be in children; got {result['children']}"
        )
        assert "story-archived" not in result["children"], (
            f"story-archived (archived) should NOT be in children by default; "
            f"got {result['children']}. "
            "Archived tickets must be excluded from children by default."
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_build_dep_graph_excludes_archived_blockers(
    graph: ModuleType, tmp_path: Path
) -> None:
    """build_dep_graph must exclude archived tickets from the blockers list by default.

    Setup:
        - ticket-active-blocker: open, blocks ticket-target
        - ticket-archived-blocker: open, blocks ticket-target, then ARCHIVED event written
        - ticket-target: open

    Expected (default exclude_archived=True):
        result['blockers'] contains only ticket-active-blocker,
        not ticket-archived-blocker.

    This test is RED — archived exclusion in blockers is not yet implemented.
    """
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        _write_ticket(tracker_dir, "ticket-active-blocker", status="open")
        _write_ticket(tracker_dir, "ticket-archived-blocker", status="open")
        _write_ticket(tracker_dir, "ticket-target", status="open")
        _write_blocks_link(
            tracker_dir, "ticket-active-blocker", "ticket-target", timestamp=1500
        )
        _write_blocks_link(
            tracker_dir, "ticket-archived-blocker", "ticket-target", timestamp=1501
        )
        _write_archive_event(tracker_dir, "ticket-archived-blocker")

        result = graph.build_dep_graph("ticket-target", str(tracker_dir))

        assert "blockers" in result, "build_dep_graph result missing 'blockers' field"
        assert "ticket-active-blocker" in result["blockers"], (
            f"ticket-active-blocker should be in blockers; got {result['blockers']}"
        )
        assert "ticket-archived-blocker" not in result["blockers"], (
            f"ticket-archived-blocker (archived) should NOT be in blockers by default; "
            f"got {result['blockers']}. "
            "Archived tickets must be excluded from blockers by default."
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_deps_cli_include_archived(tmp_path: Path) -> None:
    """ticket-graph.py CLI with --include-archived returns full graph including archived.

    Setup:
        - ticket-parent: epic
        - ticket-child-active: story, parent_id=ticket-parent (not archived)
        - ticket-child-archived: story, parent_id=ticket-parent, ARCHIVED

    Without --include-archived: children = [ticket-child-active] (archived excluded by default)
    With --include-archived: children = [ticket-child-active, ticket-child-archived]

    This test is RED — default archived exclusion is not yet implemented, so the
    without-flag case incorrectly includes the archived child.
    """
    import subprocess
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        _write_ticket(tracker_dir, "ticket-parent", ticket_type="epic")
        _write_ticket(
            tracker_dir,
            "ticket-child-active",
            parent_id="ticket-parent",
            ticket_type="story",
        )
        _write_ticket(
            tracker_dir,
            "ticket-child-archived",
            parent_id="ticket-parent",
            ticket_type="story",
        )
        _write_archive_event(tracker_dir, "ticket-child-archived")

        # First: verify default behavior excludes archived (RED — not yet implemented)
        result_default = subprocess.run(
            [
                "python3",
                str(SCRIPT_PATH),
                "ticket-parent",
                f"--tickets-dir={tracker_dir}",
            ],
            capture_output=True,
            text=True,
        )

        assert result_default.returncode == 0, (
            f"CLI (no flag) exited with {result_default.returncode}; "
            f"stderr={result_default.stderr!r}"
        )
        output_default = json.loads(result_default.stdout)
        children_default = output_default.get("children", [])
        assert "ticket-child-archived" not in children_default, (
            f"Without --include-archived, archived child must be excluded by default; "
            f"children={children_default}. "
            "Default archived exclusion is not yet implemented."
        )

        # Second: verify --include-archived includes the archived child
        result_with_flag = subprocess.run(
            [
                "python3",
                str(SCRIPT_PATH),
                "ticket-parent",
                f"--tickets-dir={tracker_dir}",
                "--include-archived",
            ],
            capture_output=True,
            text=True,
        )

        assert result_with_flag.returncode == 0, (
            f"CLI (--include-archived) exited with {result_with_flag.returncode}; "
            f"stderr={result_with_flag.stderr!r}. "
            "--include-archived flag must be recognized and return exit 0."
        )

        output_with_flag = json.loads(result_with_flag.stdout)
        children_with_flag = output_with_flag.get("children", [])
        assert "ticket-child-archived" in children_with_flag, (
            f"With --include-archived, archived child must appear in result; "
            f"children={children_with_flag}. "
            "--include-archived flag is not yet implemented."
        )
        assert "ticket-child-active" in children_with_flag, (
            f"With --include-archived, active child must still appear; "
            f"children={children_with_flag}"
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_deps_archived_direct_target_error(tmp_path: Path) -> None:
    """CLI: querying deps for an archived ticket directly exits 1 with a helpful message.

    When a user runs `ticket-graph.py <archived-ticket-id> --tickets-dir=...`,
    the ticket exists on disk but is archived. The CLI must:
      - Exit with code 1
      - Print a message to stderr suggesting --include-archived

    This guards against silently returning an empty/stale graph for an archived ticket
    when the user likely needs to use --include-archived.

    This test is RED — the archived-ticket-direct-query guard is not yet implemented.
    """
    import subprocess
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        # Create an archived ticket
        _write_ticket(tracker_dir, "ticket-archived", status="closed")
        _write_archive_event(tracker_dir, "ticket-archived")

        result = subprocess.run(
            [
                "python3",
                str(SCRIPT_PATH),
                "ticket-archived",
                f"--tickets-dir={tracker_dir}",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1, (
            f"CLI must exit 1 when querying an archived ticket directly; "
            f"got returncode={result.returncode}. "
            "The archived-ticket guard is not yet implemented."
        )
        assert "--include-archived" in result.stderr, (
            f"CLI stderr must suggest --include-archived when querying archived ticket; "
            f"got stderr={result.stderr!r}. "
            "The error message must guide users to the correct flag."
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


# ── RED MARKER BOUNDARY ──────────────────────────────────────────────────────
# Tests below this line are expected to FAIL (RED) until ticket-graph.py is
# refactored to use a single reduce_all_tickets call for deps operations.
# The .test-index RED marker points to the first test below:
# test_build_dep_graph_single_batch_scan
# Tests ABOVE this line are GREEN and must always pass.


@pytest.mark.unit
@pytest.mark.scripts
def test_build_dep_graph_single_batch_scan(graph: ModuleType, tmp_path: Path) -> None:
    """build_dep_graph must use a single reduce_all_tickets call instead of per-ticket scans.

    Setup:
        - A tracker with 5 tickets: ticket-a (closed, blocks ticket-e), ticket-b,
          ticket-c, ticket-d (all open), ticket-e (open, target ticket).

    Expected: reduce_all_tickets is called exactly once during build_dep_graph.

    Currently RED: build_dep_graph calls _reduce_ticket per-ticket via
    _compute_dep_graph and _find_direct_blockers. It does not call reduce_all_tickets.
    """
    from unittest.mock import patch

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")
    _write_ticket(tracker_dir, "ticket-d", status="open")
    _write_ticket(tracker_dir, "ticket-e", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-e")

    # Capture the real reduce_all_tickets so the patch can delegate to it
    real_reduce_all = graph._reducer.reduce_all_tickets

    call_count = []

    def counting_reduce_all(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_count.append(1)
        return real_reduce_all(*args, **kwargs)

    with patch.object(
        graph._reducer, "reduce_all_tickets", side_effect=counting_reduce_all
    ):
        graph.build_dep_graph("ticket-e", str(tracker_dir))

    assert len(call_count) == 1, (
        f"Expected reduce_all_tickets to be called exactly once during build_dep_graph, "
        f"but it was called {len(call_count)} time(s). "
        "build_dep_graph must pre-load all ticket states via a single reduce_all_tickets "
        "call instead of calling _reduce_ticket per-ticket in _find_direct_blockers and "
        "_compute_dep_graph."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_find_direct_blockers_no_per_ticket_scan(
    graph: ModuleType, tmp_path: Path
) -> None:
    """_find_direct_blockers must not call _reduce_ticket directly — use pre-loaded state.

    Setup:
        - ticket-blocker: open, blocks ticket-target
        - ticket-target: open

    Pre-loaded state dict is passed in. _reduce_ticket must NOT be called.

    Currently RED: _find_direct_blockers calls _reduce_ticket directly for each
    ticket dir it scans. After refactor, it must accept a pre-loaded all_states
    dict and use that instead.
    """
    from unittest.mock import patch

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-blocker", status="open")
    _write_ticket(tracker_dir, "ticket-target", status="open")
    _write_blocks_link(tracker_dir, "ticket-blocker", "ticket-target")

    reduce_ticket_calls = []

    def spy_reduce_ticket(*args, **kwargs):  # type: ignore[no-untyped-def]
        reduce_ticket_calls.append(args)
        return graph._reduce_ticket(*args, **kwargs)

    with patch.object(graph, "_reduce_ticket", side_effect=spy_reduce_ticket):
        # After refactor, _find_direct_blockers should accept all_states and not call _reduce_ticket
        graph._find_direct_blockers("ticket-target", str(tracker_dir))

    assert len(reduce_ticket_calls) == 0, (
        f"Expected _reduce_ticket to be called 0 times in _find_direct_blockers "
        f"(should use pre-loaded state), but it was called {len(reduce_ticket_calls)} time(s). "
        "_find_direct_blockers must be refactored to accept a pre-loaded all_states dict "
        "and look up ticket states from it instead of calling _reduce_ticket per ticket."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_compute_dep_graph_children_use_preloaded_state(
    graph: ModuleType, tmp_path: Path
) -> None:
    """_compute_dep_graph must not call _reduce_ticket for children discovery.

    Setup:
        - parent-epic: epic with 3 child stories
        - story-a, story-b, story-c: open stories with parent_id=parent-epic

    Expected: _reduce_ticket is NOT called during _compute_dep_graph. All state
    lookups should use a pre-loaded all_states dict passed in from build_dep_graph.

    Currently RED: _compute_dep_graph calls _reduce_ticket for each directory entry
    to discover children. After refactor, it must use pre-loaded state.
    """
    from unittest.mock import patch

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "parent-epic", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="parent-epic", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="parent-epic", ticket_type="story")
    _write_ticket(tracker_dir, "story-c", parent_id="parent-epic", ticket_type="story")

    reduce_ticket_calls = []

    def spy_reduce_ticket(*args, **kwargs):  # type: ignore[no-untyped-def]
        reduce_ticket_calls.append(args)
        return graph._reduce_ticket(*args, **kwargs)

    with patch.object(graph, "_reduce_ticket", side_effect=spy_reduce_ticket):
        graph._compute_dep_graph("parent-epic", str(tracker_dir))

    assert len(reduce_ticket_calls) == 0, (
        f"Expected _reduce_ticket to be called 0 times in _compute_dep_graph "
        f"(should use pre-loaded state for children discovery), "
        f"but it was called {len(reduce_ticket_calls)} time(s). "
        "_compute_dep_graph must be refactored to receive a pre-loaded all_states dict "
        "and use it for both children discovery and blocker resolution instead of "
        "calling _reduce_ticket per directory entry."
    )


# ---------------------------------------------------------------------------
# Level-scoped cycle detection wired into add_dependency
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_raises_on_cycle_at_level(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency must raise CyclicDependencyError when a level-scoped cycle would be created.

    Setup:
        - story-a: story, open — blocks story-b
        - story-b: story, open — blocks story-c
        - story-c: story, open

    Action: add_dependency(story-c, story-a, ...) should detect a cycle
    (story-a → story-b → story-c → story-a) at the 'story' level.

    Expected: CyclicDependencyError is raised.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-a", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", ticket_type="story")
    _write_ticket(tracker_dir, "story-c", ticket_type="story")
    _write_blocks_link(tracker_dir, "story-a", "story-b", timestamp=1500)
    _write_blocks_link(tracker_dir, "story-b", "story-c", timestamp=1501)

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("story-c", "story-a", str(tracker_dir), relation="blocks")


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_raises_on_self_loop_at_level(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency must raise CyclicDependencyError for a self-referential dependency.

    Setup:
        - task-x: task, open

    Action: add_dependency(task-x, task-x, ...) — self-loop.

    Expected: CyclicDependencyError is raised with a message indicating
    self-referential dependency.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "task-x", ticket_type="task")

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("task-x", "task-x", str(tracker_dir), relation="blocks")


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_skips_cycle_check_when_no_type(
    tmp_path: Path,
) -> None:
    """check_cycle_at_level with empty level returns False without BFS traversal.

    This is the fail-open path in add_dependency: when resolved_source_state
    returns ticket_type as None or empty string, level evaluates to "" and
    `if level and check_cycle_at_level(...)` short-circuits — no cycle check runs.

    Tests check_cycle_at_level directly with level="" to verify it returns False
    (fail-open) without accessing the tracker directory.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    check_cycle_at_level = _get_check_cycle_at_level()

    # Empty level string → fail-open: returns False without BFS
    result = check_cycle_at_level("task-a", "task-b", "", str(tracker_dir))
    assert result is False, (
        "check_cycle_at_level must return False for empty level (fail-open)"
    )


# ---------------------------------------------------------------------------
# resolve_hierarchy_link tests (SC1, SC3, SC5, SC10, SC11 + is_redundant)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_same_parent_story_sc1(
    graph: ModuleType, tmp_path: Path
) -> None:
    """SC1: Two tasks sharing the same parent story → original IDs unchanged.

    Setup:
        - story-parent: story (no parent)
        - task-a: task with parent_id=story-parent
        - task-b: task with parent_id=story-parent

    Expected: resolved_source=task-a, resolved_target=task-b,
              was_redirected=False, is_redundant=False
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-parent", ticket_type="story")
    _write_ticket(tracker_dir, "task-a", parent_id="story-parent", ticket_type="task")
    _write_ticket(tracker_dir, "task-b", parent_id="story-parent", ticket_type="task")

    result = graph.resolve_hierarchy_link("task-a", "task-b", str(tracker_dir))

    assert result["resolved_source"] == "task-a", (
        f"SC1: expected resolved_source='task-a', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "task-b", (
        f"SC1: expected resolved_target='task-b', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is False, (
        f"SC1: expected was_redirected=False, got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC1: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_cross_story_same_epic_sc3(
    graph: ModuleType, tmp_path: Path
) -> None:
    """SC3 (new type-tier semantics): cross-story SAME-tier task pair → NOT promoted.

    Under the type-tier model, both endpoints are tasks (tier 0 == tier 0), so a
    blocking dep links them directly. They are NOT promoted to their parent
    stories — promotion only kicks in when the endpoints differ in tier.

    Setup:
        - epic-root: epic (no parent)
        - story-a: story with parent_id=epic-root
        - story-b: story with parent_id=epic-root
        - task-a1: task with parent_id=story-a
        - task-b1: task with parent_id=story-b

    Expected: resolved_source=task-a1, resolved_target=task-b1,
              was_redirected=False, is_redundant=False
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-a1", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "task-b1", parent_id="story-b", ticket_type="task")

    result = graph.resolve_hierarchy_link(
        "task-a1", "task-b1", str(tracker_dir), "blocks"
    )

    assert result["resolved_source"] == "task-a1", (
        f"SC3: expected resolved_source='task-a1', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "task-b1", (
        f"SC3: expected resolved_target='task-b1', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is False, (
        f"SC3: expected was_redirected=False (same tier), got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC3: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_cross_epic_sc5(
    graph: ModuleType, tmp_path: Path
) -> None:
    """SC5 (new type-tier semantics): cross-epic SAME-tier task pair → NOT promoted.

    Both endpoints are tasks (tier 0). Even across separate epics, a same-tier
    blocking dep links the exact tasks; promotion only applies across tiers.

    Setup:
        - epic-a: epic (no parent)
        - epic-b: epic (no parent)
        - story-a: story with parent_id=epic-a
        - story-b: story with parent_id=epic-b
        - task-a1: task with parent_id=story-a
        - task-b1: task with parent_id=story-b

    Expected: resolved_source=task-a1, resolved_target=task-b1,
              was_redirected=False, is_redundant=False
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-a", ticket_type="epic")
    _write_ticket(tracker_dir, "epic-b", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-a", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-b", ticket_type="story")
    _write_ticket(tracker_dir, "task-a1", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "task-b1", parent_id="story-b", ticket_type="task")

    result = graph.resolve_hierarchy_link(
        "task-a1", "task-b1", str(tracker_dir), "depends_on"
    )

    assert result["resolved_source"] == "task-a1", (
        f"SC5: expected resolved_source='task-a1', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "task-b1", (
        f"SC5: expected resolved_target='task-b1', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is False, (
        f"SC5: expected was_redirected=False (same tier), got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC5: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_orphan_ticket_sc10(
    graph: ModuleType, tmp_path: Path
) -> None:
    """SC10: Tickets with no parent_id → original IDs unchanged.

    Setup:
        - orphan-a: task (no parent)
        - orphan-b: task (no parent)

    Expected: resolved_source=orphan-a, resolved_target=orphan-b,
              was_redirected=False, is_redundant=False
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "orphan-a", ticket_type="task")
    _write_ticket(tracker_dir, "orphan-b", ticket_type="task")

    result = graph.resolve_hierarchy_link("orphan-a", "orphan-b", str(tracker_dir))

    assert result["resolved_source"] == "orphan-a", (
        f"SC10: expected resolved_source='orphan-a', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "orphan-b", (
        f"SC10: expected resolved_target='orphan-b', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is False, (
        f"SC10: expected was_redirected=False, got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC10: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_unreadable_ticket_sc11(
    graph: ModuleType, tmp_path: Path
) -> None:
    """SC11: If ticket state cannot be reduced → AttributeError or returns error dict.

    Setup:
        - ticket-ok: valid task
        - missing-ticket: does not exist in tracker

    Expected: resolve_hierarchy_link returns a dict with 'error' key (not silent fallthrough)
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-ok", ticket_type="task")
    # missing-ticket directory is intentionally absent

    result = graph.resolve_hierarchy_link(
        "ticket-ok", "missing-ticket", str(tracker_dir)
    )

    assert "error" in result, (
        f"SC11: expected result to contain 'error' key for missing ticket, got {result!r}"
    )
    assert result.get("ticket_id") == "missing-ticket", (
        f"SC11: expected ticket_id='missing-ticket' in error, got {result.get('ticket_id')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_is_redundant_direct_parent(
    graph: ModuleType, tmp_path: Path
) -> None:
    """is_redundant=True when source IS the direct parent of target.

    Setup:
        - story-parent: story (no parent)
        - task-child: task with parent_id=story-parent

    Expected: resolved_source=story-parent, resolved_target=task-child (or vice versa),
              is_redundant=True (because story-parent is the direct parent of task-child)
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-parent", ticket_type="story")
    _write_ticket(
        tracker_dir, "task-child", parent_id="story-parent", ticket_type="task"
    )

    result = graph.resolve_hierarchy_link(
        "story-parent", "task-child", str(tracker_dir)
    )

    assert result["is_redundant"] is True, (
        f"is_redundant=True expected when source is direct parent of target, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_cli_outputs_json(
    graph: ModuleType, tmp_path: Path
) -> None:
    """CLI subcommand resolve-hierarchy-link outputs valid JSON.

    Setup:
        - orphan-x: task (no parent)
        - orphan-y: task (no parent)

    Verify: python3 ticket-graph.py resolve-hierarchy-link orphan-x orphan-y
            --tickets-dir=... outputs JSON with required keys.
    """
    import subprocess

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "orphan-x", ticket_type="task")
    _write_ticket(tracker_dir, "orphan-y", ticket_type="task")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "resolve-hierarchy-link",
            "orphan-x",
            "orphan-y",
            f"--tickets-dir={tracker_dir}",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"CLI returned non-zero exit code {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"CLI output is not valid JSON: {e!r}. stdout={result.stdout!r}")

    required_keys = {
        "resolved_source",
        "resolved_target",
        "was_redirected",
        "is_redundant",
    }
    missing = required_keys - set(output.keys())
    assert not missing, f"CLI output missing keys {missing}. Got: {output!r}"


# ---------------------------------------------------------------------------
# add_dependency hierarchy integration tests (story 983e-7fff)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_cross_story_redirects_to_story_level(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Cross-TIER task→epic dep: add_dependency promotes the task to its epic.

    Under the new type-tier model a task (tier 0) blocking an epic (tier 2) is
    promoted up its parent chain to the nearest epic ancestor so the resulting
    link is epic↔epic.

    Setup:
        - epic-root: epic (no parent)
        - story-a: story with parent_id=epic-root
        - task-a1: task with parent_id=story-a
        - epic-other: epic (the higher-tier endpoint)

    Expected: add_dependency('task-a1', 'epic-other', ...) writes a LINK event for
              epic-root -> epic-other (task promoted to its epic ancestor), not
              task-a1 -> epic-other.
    """
    import glob as _glob

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-a1", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "epic-other", ticket_type="epic")

    graph.add_dependency("task-a1", "epic-other", str(tracker_dir), "depends_on")

    # LINK event must be written in epic-root's directory (promoted source)
    epic_root_dir = tracker_dir / "epic-root"
    link_files = _glob.glob(str(epic_root_dir / "*-LINK.json"))
    assert len(link_files) >= 1, (
        f"Expected LINK event in epic-root dir (promoted source), found: {link_files}"
    )

    # Verify the LINK event targets epic-other (unchanged target)
    found_redirect = False
    for lf in link_files:
        with open(lf) as f:
            ev = json.load(f)
        if ev.get("data", {}).get("target_id") == "epic-other":
            found_redirect = True
            break
    assert found_redirect, (
        "Expected LINK event in epic-root with target_id='epic-other' (promotion), "
        f"but no such event found. Files: {link_files}"
    )

    # No LINK event should be written in task-a1's directory
    task_a1_dir = tracker_dir / "task-a1"
    task_link_files = _glob.glob(str(task_a1_dir / "*-LINK.json"))
    assert len(task_link_files) == 0, (
        f"Expected NO LINK event in task-a1 dir (original source), found: {task_link_files}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_redundant_link_exits_nonzero(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Task-to-direct-parent link is redundant: add_dependency raises ValueError.

    Setup:
        - story-parent: story (no parent)
        - task-child: task with parent_id=story-parent

    Expected: add_dependency('story-parent', 'task-child', ...) raises ValueError
              (is_redundant=True path -- story-parent IS the direct parent of task-child).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-parent", ticket_type="story")
    _write_ticket(
        tracker_dir, "task-child", parent_id="story-parent", ticket_type="task"
    )

    with pytest.raises(ValueError, match="redundant"):
        graph.add_dependency("story-parent", "task-child", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_cross_story_emits_redirect_json_to_stdout(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Cross-TIER promotion: add_dependency prints redirect JSON to stdout.

    Setup: a task under a story under an epic, blocking a separate epic.
    Expected: stdout contains JSON with "redirected": true plus original/resolved
    IDs reflecting the task being promoted to its epic ancestor.
    """
    import io
    import contextlib

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root2", ticket_type="epic")
    _write_ticket(tracker_dir, "story-x", parent_id="epic-root2", ticket_type="story")
    _write_ticket(tracker_dir, "task-x1", parent_id="story-x", ticket_type="task")
    _write_ticket(tracker_dir, "epic-target", ticket_type="epic")

    captured_stdout = io.StringIO()
    with contextlib.redirect_stdout(captured_stdout):
        graph.add_dependency("task-x1", "epic-target", str(tracker_dir), "depends_on")

    stdout_val = captured_stdout.getvalue()
    assert stdout_val.strip(), "Expected redirect JSON on stdout, got empty output"

    try:
        redirect_data = json.loads(stdout_val.strip())
    except json.JSONDecodeError as e:
        pytest.fail(f"stdout is not valid JSON: {e!r}. stdout={stdout_val!r}")

    assert redirect_data.get("redirected") is True, (
        f"Expected 'redirected': true in stdout JSON, got {redirect_data!r}"
    )
    assert redirect_data.get("original", {}).get("source") == "task-x1", (
        f"Expected original.source='task-x1', got {redirect_data!r}"
    )
    assert redirect_data.get("original", {}).get("target") == "epic-target", (
        f"Expected original.target='epic-target', got {redirect_data!r}"
    )
    assert redirect_data.get("resolved", {}).get("source") == "epic-root2", (
        f"Expected resolved.source='epic-root2', got {redirect_data!r}"
    )
    assert redirect_data.get("resolved", {}).get("target") == "epic-target", (
        f"Expected resolved.target='epic-target', got {redirect_data!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_same_parent_still_works(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Same-parent tasks: add_dependency writes LINK normally (no redirect).

    Setup:
        - story-shared: story (no parent)
        - task-p: task with parent_id=story-shared
        - task-q: task with parent_id=story-shared

    Expected: add_dependency('task-p', 'task-q', ...) writes LINK in task-p's
              directory with target_id='task-q' (no redirect -- same parent story).
    """
    import glob as _glob

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-shared", ticket_type="story")
    _write_ticket(tracker_dir, "task-p", parent_id="story-shared", ticket_type="task")
    _write_ticket(tracker_dir, "task-q", parent_id="story-shared", ticket_type="task")

    graph.add_dependency("task-p", "task-q", str(tracker_dir))

    # LINK event must be in task-p's directory (no redirect)
    task_p_dir = tracker_dir / "task-p"
    link_files = _glob.glob(str(task_p_dir / "*-LINK.json"))
    assert len(link_files) >= 1, (
        f"Expected LINK event in task-p dir (same parent, no redirect), found: {link_files}"
    )

    # Verify the LINK targets task-q (original target unchanged)
    found = False
    for lf in link_files:
        with open(lf) as f:
            ev = json.load(f)
        if ev.get("data", {}).get("target_id") == "task-q":
            found = True
            break
    assert found, (
        "Expected LINK event in task-p with target_id='task-q', "
        f"but no such event found. Files: {link_files}"
    )


# ---------------------------------------------------------------------------
# Type-tier comparability for BLOCKING deps only (new semantics)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize(
    "relation", ["relates_to", "duplicates", "supersedes", "discovered_from"]
)
def test_resolve_hierarchy_link_non_blocking_never_promoted(
    graph: ModuleType, tmp_path: Path, relation: str
) -> None:
    """(a) Non-blocking relations are NEVER promoted — exact pair, was_redirected=False.

    Even with a maximal tier gap (task ↔ epic), a non-blocking relation links the
    exact source/target the caller passed.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-mid", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-leaf", parent_id="story-mid", ticket_type="task")
    _write_ticket(tracker_dir, "epic-other", ticket_type="epic")

    result = graph.resolve_hierarchy_link(
        "task-leaf", "epic-other", str(tracker_dir), relation
    )

    assert result["resolved_source"] == "task-leaf", result
    assert result["resolved_target"] == "epic-other", result
    assert result["was_redirected"] is False, (
        f"{relation}: expected was_redirected=False, got {result!r}"
    )
    assert result["is_redundant"] is False, result


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize("relation", ["blocks", "depends_on"])
def test_resolve_hierarchy_link_task_to_epic_promotes_to_epic(
    graph: ModuleType, tmp_path: Path, relation: str
) -> None:
    """(b) blocks/depends_on between a task and an epic promotes the task to its epic.

    task-leaf (tier 0) under story-mid under epic-root, linked to epic-other
    (tier 2). The lower endpoint (task) is promoted up its chain to its epic
    ancestor → epic-root ↔ epic-other.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-mid", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-leaf", parent_id="story-mid", ticket_type="task")
    _write_ticket(tracker_dir, "epic-other", ticket_type="epic")

    # source is the lower-tier endpoint
    result = graph.resolve_hierarchy_link(
        "task-leaf", "epic-other", str(tracker_dir), relation
    )
    assert result["resolved_source"] == "epic-root", result
    assert result["resolved_target"] == "epic-other", result
    assert result["was_redirected"] is True, result

    # symmetric: lower-tier endpoint as TARGET is promoted too
    result_rev = graph.resolve_hierarchy_link(
        "epic-other", "task-leaf", str(tracker_dir), relation
    )
    assert result_rev["resolved_source"] == "epic-other", result_rev
    assert result_rev["resolved_target"] == "epic-root", result_rev
    assert result_rev["was_redirected"] is True, result_rev


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_task_to_story_promotes_to_story(
    graph: ModuleType, tmp_path: Path
) -> None:
    """(b') Task↔story blocking dep promotes the task only to its STORY ancestor.

    Confirms promotion targets the HIGHER endpoint's tier (story), not all the
    way to the epic root.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-mid", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-leaf", parent_id="story-mid", ticket_type="task")
    _write_ticket(tracker_dir, "story-other", parent_id="epic-root", ticket_type="story")

    result = graph.resolve_hierarchy_link(
        "task-leaf", "story-other", str(tracker_dir), "blocks"
    )
    assert result["resolved_source"] == "story-mid", result
    assert result["resolved_target"] == "story-other", result
    assert result["was_redirected"] is True, result


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize(
    "type_a,type_b", [("task", "task"), ("task", "bug"), ("bug", "bug")]
)
def test_resolve_hierarchy_link_same_tier_siblings_cousins_not_promoted(
    graph: ModuleType, tmp_path: Path, type_a: str, type_b: str
) -> None:
    """(c) Same-tier (task/bug) blocking link between siblings/cousins is NOT promoted.

    task and bug are both leaf tier 0, so they are comparable and link directly —
    regardless of whether they are siblings (same story) or cousins (different
    stories). Also exercises the bug==task tier mapping.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-root", ticket_type="story")
    # cousins: different parent stories
    _write_ticket(tracker_dir, "leaf-a", parent_id="story-a", ticket_type=type_a)
    _write_ticket(tracker_dir, "leaf-b", parent_id="story-b", ticket_type=type_b)

    result = graph.resolve_hierarchy_link(
        "leaf-a", "leaf-b", str(tracker_dir), "depends_on"
    )
    assert result["resolved_source"] == "leaf-a", result
    assert result["resolved_target"] == "leaf-b", result
    assert result["was_redirected"] is False, (
        f"({type_a},{type_b}): same leaf tier should not promote, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_fallback_no_comparable_ancestor(
    graph: ModuleType, tmp_path: Path
) -> None:
    """(d) Fallback: no comparable-tier ancestor → highest ancestor (chain root), still redirected.

    An orphan task (no parent) blocking an epic has NO epic ancestor to promote
    to. Per the fallback rule it promotes to the chain root — which for an orphan
    is itself — so was_redirected stays False (nothing moved).

    The richer fallback: a task whose only ancestor is a STORY (no epic above it)
    linked to an EPIC. There is no epic-tier ancestor, so it falls back to the
    chain root (the story) and reports was_redirected=True.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # Orphan task ↔ epic: no ancestor at all → resolves to itself, not redirected.
    _write_ticket(tracker_dir, "orphan-task", ticket_type="task")
    _write_ticket(tracker_dir, "the-epic", ticket_type="epic")
    res_orphan = graph.resolve_hierarchy_link(
        "orphan-task", "the-epic", str(tracker_dir), "blocks"
    )
    assert res_orphan["resolved_source"] == "orphan-task", res_orphan
    assert res_orphan["resolved_target"] == "the-epic", res_orphan
    assert res_orphan["was_redirected"] is False, res_orphan

    # Task under a story (no epic ancestor) ↔ epic: no epic-tier ancestor exists,
    # so fall back to the chain root (the story) and still flag the redirect.
    _write_ticket(tracker_dir, "lone-story", ticket_type="story")
    _write_ticket(tracker_dir, "story-task", parent_id="lone-story", ticket_type="task")
    res_fallback = graph.resolve_hierarchy_link(
        "story-task", "the-epic", str(tracker_dir), "blocks"
    )
    assert res_fallback["resolved_source"] == "lone-story", res_fallback
    assert res_fallback["resolved_target"] == "the-epic", res_fallback
    assert res_fallback["was_redirected"] is True, res_fallback


# ---------------------------------------------------------------------------
# check_cycle_at_level helpers
# ---------------------------------------------------------------------------


def _make_ticket(tracker: Path, ticket_id: str, ticket_type: str = "task") -> Path:
    """Write a minimal ticket directory with a CREATE event. Returns the ticket dir."""
    ticket_dir = tracker / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    create_event = {
        "event_type": "CREATE",
        "uuid": f"create-{ticket_id}",
        "timestamp": 1000,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": ticket_type,
            "title": f"Ticket {ticket_id}",
            "parent_id": None,
        },
    }
    with open(ticket_dir / f"1000-create-{ticket_id}-CREATE.json", "w") as f:
        json.dump(create_event, f)
    return ticket_dir


def _write_link_event(
    source_id: str,
    target_id: str,
    relation: str,
    tracker_dir: str,
    timestamp: int = 1500,
) -> None:
    """Write a LINK event in source_id's directory pointing at target_id."""
    source_dir = Path(tracker_dir) / source_id
    source_dir.mkdir(parents=True, exist_ok=True)
    link_uuid = f"link-{source_id}-{relation}-{target_id}"
    link_event = {
        "event_type": "LINK",
        "uuid": link_uuid,
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": target_id,
            "relation": relation,
        },
    }
    filename = f"{timestamp}-{link_uuid}-LINK.json"
    with open(source_dir / filename, "w") as f:
        json.dump(link_event, f)


def _get_check_cycle_at_level():  # type: ignore[no-untyped-def]
    """Load check_cycle_at_level from ticket-graph module."""
    mod = _load_module()
    return mod.check_cycle_at_level


# ---------------------------------------------------------------------------
# check_cycle_at_level tests (RED — function does not exist yet)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_detects_cycle(tmp_path: Path) -> None:
    """check_cycle_at_level returns True when adding A→B would close an existing B→A cycle."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    # epic-A and epic-B exist; epic-B has depends_on link to epic-A (B→A)
    # So adding epic-A→epic-B would create a cycle: A→B→A
    _make_ticket(tracker, "epic-A", ticket_type="epic")
    _make_ticket(tracker, "epic-B", ticket_type="epic")
    _write_link_event("epic-B", "epic-A", "depends_on", str(tracker))
    assert check_cycle_at_level("epic-A", "epic-B", "epic", str(tracker)) is True


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_detects_self_loop(tmp_path: Path) -> None:
    """check_cycle_at_level returns True for self-loops (source==target)."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    _make_ticket(tracker, "epic-A", ticket_type="epic")
    assert check_cycle_at_level("epic-A", "epic-A", "epic", str(tracker)) is True


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_case_insensitive(tmp_path: Path) -> None:
    """check_cycle_at_level matches ticket_type case-insensitively."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    _make_ticket(tracker, "epic-A", ticket_type="Epic")  # capital E
    _make_ticket(tracker, "epic-B", ticket_type="Epic")
    _write_link_event("epic-B", "epic-A", "depends_on", str(tracker))
    assert check_cycle_at_level("epic-A", "epic-B", "epic", str(tracker)) is True


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_no_false_positive(tmp_path: Path) -> None:
    """check_cycle_at_level returns False when no cycle exists."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    _make_ticket(tracker, "epic-A", ticket_type="epic")
    _make_ticket(tracker, "epic-B", ticket_type="epic")
    # A→B exists, but we're checking if adding B→A would cycle — that's True
    # Instead test: no existing links, checking epic-A→epic-B: should be False
    assert check_cycle_at_level("epic-A", "epic-B", "epic", str(tracker)) is False


@pytest.mark.unit
@pytest.mark.scripts
def test_hierarchy_enforcement_benchmark_1000_tickets(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Hierarchy enforcement completes 10 cross-tier add_dependency calls under 5s on 1000-ticket hierarchy.

    Setup: 10 epics × 10 stories × 10 tasks = 1,000 tickets.
    Action: 10 add_dependency calls linking a task to a *different* epic (cross-tier),
            which under the type-tier model promotes the task to its own epic ancestor.
    Assert: all calls complete in <5.0 seconds AND at least one epic-level dep was promoted.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # Build 10×10×10 hierarchy
    for i in range(10):
        _write_ticket(tracker_dir, f"epic-{i:02d}", ticket_type="epic")
        for j in range(10):
            _write_ticket(
                tracker_dir,
                f"story-{i:02d}-{j:02d}",
                ticket_type="story",
                parent_id=f"epic-{i:02d}",
            )
            for k in range(10):
                _write_ticket(
                    tracker_dir,
                    f"task-{i:02d}-{j:02d}-{k:02d}",
                    ticket_type="task",
                    parent_id=f"story-{i:02d}-{j:02d}",
                )

    # 9 cross-tier add_dependency calls: each task in epic-01..epic-09 depends_on
    # epic-00. Each task is promoted up to its own epic ancestor, yielding
    # epic-0X → epic-00 (a fan-in DAG — no cycle, no wrap-around).
    start = time.monotonic()
    for i in range(1, 10):
        graph.add_dependency(
            f"task-{i:02d}-00-00",
            "epic-00",
            str(tracker_dir),
            "depends_on",
        )
    elapsed = time.monotonic() - start

    # Performance assertion
    assert elapsed < 5.0, (
        f"9 cross-tier add_dependency calls took {elapsed:.2f}s (limit: 5.0s)"
    )

    # Correctness: verify at least one epic-level dep was actually written
    # (task-01-00-00 promoted to epic-01, depends_on epic-00).
    epic_deps = graph.build_dep_graph("epic-01", str(tracker_dir)).get("deps", [])
    assert any(d["target_id"] == "epic-00" for d in epic_deps), (
        "Expected epic-01 → epic-00 dep after cross-tier task→epic link"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_dep_graph_correctness_after_cross_story_link(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Cross-tier task→story link is promoted to story level; task-level link NOT written.

    Under the type-tier model, a task (tier 0) blocking a story (tier 1) is
    promoted up to the task's own story ancestor, yielding story↔story.

    Given: epic-a → story-x → task-a, and epic-a → story-y
    When: add_dependency('task-a', 'story-y', tracker_dir, 'depends_on')
    Then: story-x has story-y in its deps (task-a promoted to story-x);
          task-a does NOT have story-y in its deps.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-a", ticket_type="epic")
    _write_ticket(tracker_dir, "story-x", ticket_type="story", parent_id="epic-a")
    _write_ticket(tracker_dir, "task-a", ticket_type="task", parent_id="story-x")
    _write_ticket(tracker_dir, "story-y", ticket_type="story", parent_id="epic-a")

    graph.add_dependency("task-a", "story-y", str(tracker_dir), "depends_on")

    # Story-level dep should be present (task-a promoted to story-x)
    story_x_deps = graph.build_dep_graph("story-x", str(tracker_dir)).get("deps", [])
    assert any(d["target_id"] == "story-y" for d in story_x_deps), (
        "Expected story-x → story-y dep after cross-tier task→story link"
    )

    # Task-level dep should be absent (NOT written at original task level)
    task_a_deps = graph.build_dep_graph("task-a", str(tracker_dir)).get("deps", [])
    assert not any(d["target_id"] == "story-y" for d in task_a_deps), (
        "Expected NO task-a → story-y dep (should have been promoted to story level)"
    )


# ---------------------------------------------------------------------------
# Tests for _write_link_event push retry logic (bug 79de-85d4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_write_link_event_retries_on_non_fast_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_write_link_event retries push on non-fast-forward and succeeds on second attempt.

    Mock ordering note: subprocess.run is called for git add, git commit, git remote
    BEFORE the push retry loop, so side_effects must account for all 7 calls:
      add_ok, commit_ok, remote_ok, push_fail, fetch_ok, rebase_ok, push_ok
    MagicMock does NOT simulate check=True raising CalledProcessError — returncode is
    returned but no exception is raised for add/commit mocks.
    """
    import sys
    from unittest.mock import MagicMock, patch

    _scripts_dir = str(REPO_ROOT / "src" / "rebar" / "_engine")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    from ticket_graph._links import _write_link_event as _real_write_link_event

    # Sandbox cwd to tmp_path so any relative file write under test stays inside
    # the auto-cleaned fixture rather than landing in REPO_ROOT.
    monkeypatch.chdir(tmp_path)

    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    (tracker_dir / "tkt-src3").mkdir()

    ok = MagicMock(returncode=0, stdout="", stderr="")
    remote_ok = MagicMock(returncode=0, stdout="origin\n", stderr="")
    push_fail = MagicMock(
        returncode=1, stdout="", stderr="error: non-fast-forward updates were rejected"
    )
    push_ok = MagicMock(returncode=0, stdout="", stderr="")

    # Call order: git add, git commit, git remote, git push (fail),
    #             git fetch, git rebase, git push (success)
    side_effects = [ok, ok, remote_ok, push_fail, ok, ok, push_ok]

    with (
        patch("subprocess.run", side_effect=side_effects) as mock_run,
        patch.dict("os.environ", {"_TICKET_TEST_NO_SYNC": ""}, clear=False),
    ):
        _real_write_link_event("tkt-src3", "tkt-tgt3", "depends_on", str(tracker_dir))

    all_cmds = [c.args[0] for c in mock_run.call_args_list if c.args]
    push_calls = [cmd for cmd in all_cmds if "push" in cmd]
    assert len(push_calls) == 2, (
        f"Expected 2 push attempts (1 non-fast-forward + 1 retry success), got {len(push_calls)}. "
        f"All commands: {all_cmds}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_write_link_event_push_gives_up_on_merge_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_write_link_event is best-effort: if rebase AND merge both fail, it gives up silently."""
    import sys
    from unittest.mock import MagicMock, patch

    _scripts_dir = str(REPO_ROOT / "src" / "rebar" / "_engine")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    from ticket_graph._links import _write_link_event as _real_write_link_event

    # Sandbox cwd to tmp_path so any relative file write under test stays inside
    # the auto-cleaned fixture rather than landing in REPO_ROOT.
    monkeypatch.chdir(tmp_path)

    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    (tracker_dir / "tkt-src4").mkdir()

    ok = MagicMock(returncode=0, stdout="", stderr="")
    remote_ok = MagicMock(returncode=0, stdout="origin\n", stderr="")
    push_fail = MagicMock(
        returncode=1, stdout="", stderr="error: non-fast-forward updates were rejected"
    )
    rebase_fail = MagicMock(
        returncode=1, stdout="", stderr="CONFLICT (content): Merge conflict"
    )
    merge_fail = MagicMock(
        returncode=1, stdout="", stderr="CONFLICT (content): Merge conflict"
    )

    # git add, git commit, git remote, git push (fail), git fetch,
    # git rebase (fail), git rebase --abort, git merge (fail), git merge --abort
    side_effects = [ok, ok, remote_ok, push_fail, ok, rebase_fail, ok, merge_fail, ok]

    with (
        patch("subprocess.run", side_effect=side_effects),
        patch.dict("os.environ", {"_TICKET_TEST_NO_SYNC": ""}, clear=False),
    ):
        # Must not raise — best-effort means failure is silently swallowed
        _real_write_link_event("tkt-src4", "tkt-tgt4", "depends_on", str(tracker_dir))


# ---------------------------------------------------------------------------
# Bug c7a6-96e8: cycle-detection must skip hidden dirs (.suggestions, etc.)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cycle_detection_ignores_suggestions_dir(tmp_path: Path) -> None:
    """Cycle detection via _get_all_blocked_by must not emit 'skipping corrupt event'
    warnings when the tracker dir contains a .suggestions/ subdirectory.

    Bug c7a6-96e8: during `ticket link <src> <tgt> depends_on`, the graph
    traversal iterated all directories in the tracker, including hidden dirs like
    .suggestions/.  Suggestion JSON files lack 'event_type' and are not ticket
    events, so the reducer emitted a WARNING for each one.

    After the fix, hidden directories starting with '.' are skipped entirely.
    """
    import io
    import sys

    _scripts_dir = str(REPO_ROOT / "src" / "rebar" / "_engine")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    from ticket_graph._graph import _get_all_blocked_by

    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()

    # Write two regular tickets with no dependency links
    _write_ticket(tracker_dir, "tkt-aaa", status="open")
    _write_ticket(tracker_dir, "tkt-bbb", status="open")

    # Write a .suggestions/ directory with a well-formed suggestion file
    # (schema_version, not event_type — exactly what suggestion-record.sh writes)
    suggestions_dir = tracker_dir / ".suggestions"
    suggestions_dir.mkdir()
    suggestion_payload = {
        "schema_version": 1,
        "timestamp": 1778262194332,
        "session_id": "b25af26e-4e7d-6a27-ad27-4d6a8184522b",
        "source": "stop-hook",
        "observation": "test suggestion",
    }
    suggestion_file = (
        suggestions_dir
        / "1778262194332-b25af26e-4e7d6a27-ad27-4d6a-8184-5f894b522b0e.json"
    )
    suggestion_file.write_text(json.dumps(suggestion_payload))

    # Capture stderr to detect any spurious warnings
    captured_stderr = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured_stderr
    try:
        # _get_all_blocked_by scans the tracker dir for reverse-dep lookup —
        # this is exactly the code path that triggers during cycle detection.
        blocked = _get_all_blocked_by("tkt-aaa", str(tracker_dir))
    finally:
        sys.stderr = old_stderr

    stderr_output = captured_stderr.getvalue()

    # The suggestion file must NOT trigger the "skipping corrupt event" warning
    assert "skipping corrupt event" not in stderr_output, (
        f"Expected no 'skipping corrupt event' warnings, but got:\n{stderr_output}\n"
        "Fix: skip hidden directories (starting with '.') in _get_all_blocked_by."
    )
    # The result must also be correct — no spurious deps from suggestions
    assert "tkt-bbb" not in blocked, (
        f"Expected tkt-bbb not in blocked set (no link exists), got {blocked!r}"
    )


# ---------------------------------------------------------------------------
# Relation-grammar validation (Bug 61b8-bb44-fb04-402c)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_rejects_non_canonical_relation(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency raises ValueError for a non-canonical relation ('blocked_by').

    Bug 61b8-bb44-fb04-402c: the ticket-graph.py --link path (add_dependency in
    _links.py) performed NO validation of the relation argument against the
    canonical set {blocks, depends_on, relates_to, duplicates, supersedes}.  It
    wrote whatever string was passed (e.g. 'blocked_by') verbatim.

    Setup: two open tickets ticket-a and ticket-b.
    Action: add_dependency('ticket-a', 'ticket-b', tracker_dir, 'blocked_by')
    Expected: raises ValueError (not CyclicDependencyError); no LINK event written.
    """
    import glob
    import subprocess

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")

    subprocess.run(
        ["git", "-C", str(tracker_dir), "init", "-q", "--initial-branch=tickets"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    link_count_before = len(glob.glob(str(tracker_dir / "ticket-a" / "*-LINK.json")))

    with pytest.raises(ValueError, match="blocked_by"):
        graph.add_dependency("ticket-a", "ticket-b", str(tracker_dir), "blocked_by")

    link_count_after = len(glob.glob(str(tracker_dir / "ticket-a" / "*-LINK.json")))
    assert link_count_after == link_count_before, (
        f"Expected no LINK event to be written for non-canonical relation 'blocked_by', "
        f"but found {link_count_after - link_count_before} new LINK event(s). "
        "Fix: add CANONICAL_RELATIONS validation at the top of add_dependency() in _links.py."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_rejects_all_non_canonical_relations(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency raises ValueError for every string outside the canonical set.

    Checks a representative sample of non-canonical relation strings:
    'blocked_by', 'is_blocked_by', 'related', 'causes', 'parent', 'child', '', 'BLOCKS'.

    For each: no LINK event must be written to disk.
    """
    import glob
    import subprocess

    non_canonical = [
        "blocked_by",
        "is_blocked_by",
        "related",
        "causes",
        "parent",
        "child",
        "",
        "BLOCKS",  # case mismatch — must also be rejected
    ]

    for bad_relation in non_canonical:
        subdir = tmp_path / f"tracker_{bad_relation or 'empty'}"
        subdir.mkdir()

        _write_ticket(subdir, "src", status="open")
        _write_ticket(subdir, "tgt", status="open")

        subprocess.run(
            ["git", "-C", str(subdir), "init", "-q", "--initial-branch=tickets"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )

        with pytest.raises(ValueError):
            graph.add_dependency("src", "tgt", str(subdir), bad_relation)

        link_files = glob.glob(str(subdir / "src" / "*-LINK.json"))
        assert len(link_files) == 0, (
            f"Expected no LINK event for non-canonical relation {bad_relation!r}, "
            f"but found: {link_files}. "
            "Fix: validate relation in add_dependency() before writing any event."
        )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_accepts_all_canonical_relations(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add_dependency accepts all five canonical relations without raising ValueError.

    Canonical set: blocks, depends_on, relates_to, duplicates, supersedes.
    Each should succeed (no ValueError raised) when two open tickets exist.
    """
    import subprocess

    canonical = ["blocks", "depends_on", "relates_to", "duplicates", "supersedes"]

    for relation in canonical:
        subdir = tmp_path / f"tracker_{relation}"
        subdir.mkdir()

        _write_ticket(subdir, "src", status="open")
        _write_ticket(subdir, "tgt", status="open")

        subprocess.run(
            ["git", "-C", str(subdir), "init", "-q", "--initial-branch=tickets"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(subdir),
                "commit",
                "-q",
                "--no-verify",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
            capture_output=True,
        )

        # Should NOT raise ValueError for canonical relations
        monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")
        try:
            graph.add_dependency("src", "tgt", str(subdir), relation)
        except graph.CyclicDependencyError:
            pass  # cycle detection is a different guard — OK
        except ValueError as exc:
            pytest.fail(
                f"add_dependency raised ValueError for canonical relation {relation!r}: {exc}\n"
                "Fix: only reject non-canonical relations, not canonical ones."
            )
