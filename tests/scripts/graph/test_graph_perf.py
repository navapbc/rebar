"""Performance & scan-efficiency (build-1000, single-batch scan, hierarchy benchmark)

Split from the former monolithic tests/scripts/test_ticket_graph.py along
graph-concern seams. The `graph` fixture + autouse git-isolation fixture live in
conftest.py; event-writing helpers + the module loader in _helpers.py.
"""

from __future__ import annotations

import os
import time
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
def test_graph_build_scales_linearly(graph: ModuleType, tmp_path: Path) -> None:
    """build_dep_graph scales ~linearly in chain length — a RELATIVE signal.

    Setup:
        - Two linear blocks-chains, of N and 2N tickets (all closed but the tail).
        - Time build_dep_graph on the tail ticket of each.

    Why relative, not an absolute ceiling: a raw ``elapsed < 2.0s`` gate false-failed
    on loaded/slow shared CI runners (esp. macOS) even with unchanged code (bug
    wall-marlin-filth). The doubling ratio ``t(2N)/t(N)`` is immune to that — runner
    load inflates both measurements equally and cancels — while still catching a real
    algorithmic regression: a linear build doubles (~2.0x), a reintroduced O(n^2) build
    quadruples (~4.0x). The 3.0 threshold sits between, so a >2x regression fails and
    infra contention does not.
    """

    def build_tail(n: int, sub: str) -> float:
        tracker_dir = tmp_path / sub
        tracker_dir.mkdir()
        for i in range(n):
            _write_ticket(tracker_dir, f"ticket-{i:04d}", status="closed" if i < n - 1 else "open")
        for i in range(n - 1):
            _write_blocks_link(
                tracker_dir,
                f"ticket-{i:04d}",
                f"ticket-{i + 1:04d}",
                link_uuid=f"link-{i:04d}",
                timestamp=1500 + i,
            )
        start = time.monotonic()
        result = graph.build_dep_graph(f"ticket-{n - 1:04d}", str(tracker_dir))
        elapsed = time.monotonic() - start
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        return elapsed

    n = 1000
    t_n = build_tail(n, "chain_n")
    t_2n = build_tail(2 * n, "chain_2n")

    # Guard against a divide-by-noise on an implausibly fast machine: if the N build is
    # sub-20ms the ratio is dominated by timer noise, not scaling — the O(n) invariant
    # is anyway locked structurally by the single-batch-scan mock tests below.
    if t_n < 0.02:
        pytest.skip(f"build too fast to measure a stable ratio (t({n})={t_n:.4f}s)")

    ratio = t_2n / t_n
    assert ratio < 3.0, (
        f"build_dep_graph scaled super-linearly: t({n})={t_n:.3f}s, t({2 * n})={t_2n:.3f}s, "
        f"ratio={ratio:.2f} (linear≈2.0, O(n^2)≈4.0; limit 3.0) — likely an algorithmic regression"
    )


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

    with patch.object(graph._reducer, "reduce_all_tickets", side_effect=counting_reduce_all):
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
def test_find_direct_blockers_no_per_ticket_scan(graph: ModuleType, tmp_path: Path) -> None:
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
def test_compute_dep_graph_children_use_preloaded_state(graph: ModuleType, tmp_path: Path) -> None:
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


@pytest.mark.unit
@pytest.mark.scripts
def test_hierarchy_enforcement_benchmark_relative(graph: ModuleType, tmp_path: Path) -> None:
    """Cross-tier add_dependency stays cheap RELATIVE to a same-run baseline op.

    Setup: 10 epics × 10 stories × 10 tasks = 1,000 tickets.
    Action: 9 add_dependency calls linking a task to a *different* epic (cross-tier),
            which under the type-tier model promotes the task to its own epic ancestor.
    Assert: the 9 promotions cost less than a generous multiple of a SAME-RUN baseline
            (one build_dep_graph over the hierarchy) AND at least one epic-level dep was
            promoted.

    Why relative, not an absolute ceiling: a raw ``elapsed < 5.0s`` gate false-failed on
    loaded CI runners even with unchanged code (bug wall-marlin-filth). Normalizing to a
    baseline measured on the same machine/run cancels runner load (both inflate equally),
    while a per-call O(n^2) promotion regression — each call rescanning the whole store —
    explodes the ratio far past the bound, so a real algorithmic regression still fails.
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

    # Same-run baseline: one full graph build over the hierarchy, on this machine.
    b0 = time.monotonic()
    graph.build_dep_graph("epic-00", str(tracker_dir))
    baseline = time.monotonic() - b0

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

    # Relative performance assertion (load-independent). Observed ratio ~2–3.4 on linear
    # code (a cold first-call outlier included); the 15x bound leaves generous headroom
    # for CI contention while an O(n^2) per-call regression (ratio ~n) fails decisively.
    if baseline >= 0.02:
        ratio = elapsed / baseline
        assert ratio < 15.0, (
            f"9 cross-tier add_dependency calls cost {elapsed:.2f}s vs a {baseline:.3f}s "
            f"baseline build (ratio {ratio:.1f}, limit 15.0) — likely an O(n^2) per-call regression"
        )

    # Correctness: verify at least one epic-level dep was actually written
    # (task-01-00-00 promoted to epic-01, depends_on epic-00).
    epic_deps = graph.build_dep_graph("epic-01", str(tracker_dir)).get("deps", [])
    assert any(d["target_id"] == "epic-00" for d in epic_deps), (
        "Expected epic-01 → epic-00 dep after cross-tier task→epic link"
    )
