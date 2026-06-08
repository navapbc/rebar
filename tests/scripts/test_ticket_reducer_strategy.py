"""RED tests for ReducerStrategy protocol and LastTimestampWinsStrategy.

These tests are RED — they test functionality that does not yet exist in
ticket-reducer.py. All test functions must FAIL before T2 (w20-c38q)
adds ReducerStrategy and LastTimestampWinsStrategy.

Contract reference: src/rebar/_engine/docs/contracts/ticket-reducer-strategy-contract.md

Test: poetry run pytest tests/scripts/test_ticket_reducer_strategy.py --tb=short -q
All tests must return non-zero until the strategy classes are implemented.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading — filename has hyphens so we use importlib
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket-reducer.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_reducer", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def reducer() -> ModuleType:
    """Return the ticket-reducer module, failing all tests if absent (RED)."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"ticket-reducer.py not found at {SCRIPT_PATH} — "
            "this is expected RED state; implement the script to make tests pass."
        )
    return _load_module()


# ---------------------------------------------------------------------------
# Test 1: LastTimestampWinsStrategy is importable from ticket_reducer module
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_last_timestamp_wins_strategy_is_importable(reducer: ModuleType) -> None:
    """LastTimestampWinsStrategy must be importable from ticket-reducer.py.

    RED: class does not exist yet — AttributeError expected until T2 implements it.
    """
    assert hasattr(reducer, "LastTimestampWinsStrategy"), (
        "LastTimestampWinsStrategy not found in ticket-reducer.py — "
        "this is the expected RED state before T2 implementation."
    )


# ---------------------------------------------------------------------------
# Test 2: LastTimestampWinsStrategy deduplicates by UUID
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_last_timestamp_wins_dedup_by_uuid(reducer: ModuleType) -> None:
    """Merging two event lists with one overlapping UUID yields that UUID once.

    Contract: UUID uniqueness is the authoritative identity for events.
    If two events share the same uuid, only the first occurrence is kept.
    """
    LastTimestampWinsStrategy = getattr(reducer, "LastTimestampWinsStrategy")
    strategy = LastTimestampWinsStrategy()

    shared_uuid = "aaa-111"
    events = [
        {"uuid": shared_uuid, "timestamp": 100, "event_type": "CREATE"},
        {"uuid": "bbb-222", "timestamp": 200, "event_type": "STATUS"},
        # Duplicate: same UUID as first event (simulates same event from two envs)
        {"uuid": shared_uuid, "timestamp": 100, "event_type": "CREATE"},
    ]

    result = strategy.resolve(events)

    uuids = [e["uuid"] for e in result]
    assert uuids.count(shared_uuid) == 1, (
        f"Expected UUID '{shared_uuid}' to appear exactly once after dedup, "
        f"but got {uuids.count(shared_uuid)} occurrences. Full result: {result}"
    )


# ---------------------------------------------------------------------------
# Test 3: LastTimestampWinsStrategy sorts by ascending timestamp
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_last_timestamp_wins_sorted_by_timestamp(reducer: ModuleType) -> None:
    """Events from both lists appear in ascending timestamp order after resolve().

    Contract: After deduplication, events are sorted ascending by timestamp field.
    """
    LastTimestampWinsStrategy = getattr(reducer, "LastTimestampWinsStrategy")
    strategy = LastTimestampWinsStrategy()

    events = [
        {"uuid": "aaa", "timestamp": 100, "event_type": "CREATE"},
        {"uuid": "bbb", "timestamp": 200, "event_type": "STATUS"},
        {
            "uuid": "aaa",
            "timestamp": 100,
            "event_type": "CREATE",
        },  # duplicate — dropped
        {"uuid": "ccc", "timestamp": 150, "event_type": "COMMENT"},
    ]

    result = strategy.resolve(events)

    timestamps = [e["timestamp"] for e in result]
    assert timestamps == sorted(timestamps), (
        f"Expected events sorted ascending by timestamp, "
        f"but got order: {timestamps}. Full result: {result}"
    )
    # Verify dedup happened alongside sort
    assert len(result) == 3, (
        f"Expected 3 events after dedup+sort (uuid 'aaa' deduplicated), "
        f"but got {len(result)}. Full result: {result}"
    )


# ---------------------------------------------------------------------------
# Test 4: ReducerStrategy Protocol has resolve method with correct signature
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_strategy_protocol_has_resolve_method(reducer: ModuleType) -> None:
    """ReducerStrategy Protocol must expose resolve(events: list[dict]) -> list[dict].

    Contract: ReducerStrategy is a typing.Protocol with @runtime_checkable.
    Any class with a matching resolve method satisfies it structurally.
    """
    assert hasattr(reducer, "ReducerStrategy"), (
        "ReducerStrategy not found in ticket-reducer.py — "
        "this is the expected RED state before T2 implementation."
    )

    ReducerStrategy = getattr(reducer, "ReducerStrategy")

    # The protocol must be runtime-checkable: isinstance() works on conforming objects
    class ConformingClass:
        def resolve(self, events: list[dict]) -> list[dict]:
            return events

    conforming = ConformingClass()
    assert isinstance(conforming, ReducerStrategy), (
        "ReducerStrategy must be @runtime_checkable so isinstance() works for "
        "any object with a matching resolve method (structural subtyping)."
    )

    # An object without resolve must NOT satisfy the protocol
    class NonConforming:
        pass

    non_conforming = NonConforming()
    assert not isinstance(non_conforming, ReducerStrategy), (
        "An object without resolve() should NOT satisfy ReducerStrategy."
    )


# ---------------------------------------------------------------------------
# Test 5: reduce_ticket without strategy arg uses LastTimestampWinsStrategy behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_default_reducer_used_when_no_strategy_provided(
    reducer: ModuleType, tmp_path: Path
) -> None:
    """reduce_ticket(path) with no strategy arg uses LastTimestampWinsStrategy.

    The default behavior (dedup + sort) must apply even when no strategy is passed.
    This test creates a minimal ticket directory and verifies reduce_ticket still works,
    confirming backward compatibility and that the default strategy is wired in.
    """
    import json

    reduce_ticket = getattr(reducer, "reduce_ticket")

    ticket_dir = tmp_path / "tkt-default"
    ticket_dir.mkdir()

    # Write a minimal valid CREATE event
    create_event = {
        "event_type": "CREATE",
        "uuid": "create-uuid-001",
        "timestamp": 1000000,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": "task",
            "title": "Default strategy test ticket",
        },
    }
    event_file = ticket_dir / "1000000-create-uuid-001-CREATE.json"
    event_file.write_text(json.dumps(create_event), encoding="utf-8")

    # Call reduce_ticket without strategy — must not raise TypeError
    # (This will fail RED if reduce_ticket signature does not accept strategy param)
    import inspect

    sig = inspect.signature(reduce_ticket)
    assert "strategy" in sig.parameters, (
        "reduce_ticket() must accept a 'strategy' keyword argument. "
        "This is the expected RED state before T2 adds the parameter."
    )

    result = reduce_ticket(ticket_dir)
    assert result is not None, "reduce_ticket returned None for a valid ticket dir"
    assert result.get("title") == "Default strategy test ticket"
