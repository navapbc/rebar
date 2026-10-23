"""Test graph cache invalidation, batch scanning, and performance."""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType

import pytest
from _helpers import (
    _write_blocks_link,
    _write_ticket,
)

# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_private_reducer_shims_are_absent(graph: ModuleType) -> None:
    assert not hasattr(graph, "_reduce_ticket")
    assert not hasattr(graph, "_reducer")


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cache_invalidated_on_new_link(graph: ModuleType, tmp_path: Path) -> None:
    """Invalidate the graph cache when a new open blocker is linked."""
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
    """Invalidate the graph cache on an equal-size event rewrite with changed mtime.

    Filename and byte length alone can return stale dependency and readiness results.
    """

    from rebar.graph._cache import _compute_cache_key

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


# Batch-scan tests below pin a single preload and no per-ticket reduction.


@pytest.mark.unit
@pytest.mark.scripts
def test_build_dep_graph_single_batch_scan(graph: ModuleType, tmp_path: Path) -> None:
    """Build the graph with exactly one reduce_all_tickets call."""
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
    real_reduce_all = graph._loader.reducer.reduce_all_tickets

    call_count = []

    def counting_reduce_all(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_count.append(1)
        return real_reduce_all(*args, **kwargs)

    with patch.object(graph._loader.reducer, "reduce_all_tickets", side_effect=counting_reduce_all):
        graph.build_dep_graph("ticket-e", str(tracker_dir))

    assert len(call_count) == 1, (
        f"Expected reduce_all_tickets to be called exactly once during build_dep_graph, "
        f"but it was called {len(call_count)} time(s). "
        "build_dep_graph must pre-load all ticket states via a single reduce_all_tickets "
        "call instead of calling _loader.reduce_ticket per-ticket in _find_direct_blockers and "
        "_compute_dep_graph."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_find_direct_blockers_no_per_ticket_scan(graph: ModuleType, tmp_path: Path) -> None:
    """Find direct blockers from preloaded state without per-ticket reduction."""
    from unittest.mock import patch

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-blocker", status="open")
    _write_ticket(tracker_dir, "ticket-target", status="open")
    _write_blocks_link(tracker_dir, "ticket-blocker", "ticket-target")

    real_reduce_ticket = graph._loader.reduce_ticket
    reduce_ticket_calls = []

    def spy_reduce_ticket(*args, **kwargs):  # type: ignore[no-untyped-def]
        reduce_ticket_calls.append(args)
        return real_reduce_ticket(*args, **kwargs)

    with patch.object(graph._loader, "reduce_ticket", side_effect=spy_reduce_ticket):
        # Preloaded lookup skips reduce_ticket.
        graph._find_direct_blockers("ticket-target", str(tracker_dir))

    assert len(reduce_ticket_calls) == 0, (
        f"Expected _loader.reduce_ticket to be called 0 times in _find_direct_blockers "
        f"(should use pre-loaded state), but it was called {len(reduce_ticket_calls)} time(s). "
        "_find_direct_blockers must be refactored to accept a pre-loaded all_states dict "
        "and look up ticket states from it instead of calling _loader.reduce_ticket per ticket."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_compute_dep_graph_children_use_preloaded_state(graph: ModuleType, tmp_path: Path) -> None:
    """Discover child tickets from preloaded state, without reduce_ticket calls."""
    from unittest.mock import patch

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "parent-epic", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="parent-epic", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="parent-epic", ticket_type="story")
    _write_ticket(tracker_dir, "story-c", parent_id="parent-epic", ticket_type="story")

    real_reduce_ticket = graph._loader.reduce_ticket
    reduce_ticket_calls = []

    def spy_reduce_ticket(*args, **kwargs):  # type: ignore[no-untyped-def]
        reduce_ticket_calls.append(args)
        return real_reduce_ticket(*args, **kwargs)

    with patch.object(graph._loader, "reduce_ticket", side_effect=spy_reduce_ticket):
        graph._compute_dep_graph("parent-epic", str(tracker_dir))

    assert len(reduce_ticket_calls) == 0, (
        f"Expected _loader.reduce_ticket to be called 0 times in _compute_dep_graph "
        f"(should use pre-loaded state for children discovery), "
        f"but it was called {len(reduce_ticket_calls)} time(s). "
        "_compute_dep_graph must be refactored to receive a pre-loaded all_states dict "
        "and use it for both children discovery and blocker resolution instead of "
        "calling _loader.reduce_ticket per directory entry."
    )
