"""Tests for MostStatusEventsWinsStrategy conflict resolution.

Tests verify MostStatusEventsWinsStrategy behavior (GREEN after dso-b0ku).

Contract reference: src/rebar/_engine/docs/contracts/ticket-reducer-strategy-contract.md
Story: dso-je9x (write RED tests), dso-b0ku (implement MostStatusEventsWinsStrategy)

Test: python3 -m pytest tests/scripts/test_ticket_reducer_conflict.py -q
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


def _make_create_event(env_id: str, uuid: str = "create-001", ts: int = 1000) -> dict:
    """Helper: build a minimal valid CREATE event dict."""
    return {
        "event_type": "CREATE",
        "uuid": uuid,
        "timestamp": ts,
        "author": "test-user",
        "env_id": env_id,
        "data": {
            "ticket_type": "task",
            "title": "Conflict test ticket",
        },
    }


def _make_status_event(
    env_id: str,
    uuid: str,
    ts: int,
    status: str,
    current_status: str = "open",
) -> dict:
    """Helper: build a STATUS event dict."""
    return {
        "event_type": "STATUS",
        "uuid": uuid,
        "timestamp": ts,
        "env_id": env_id,
        "data": {
            "status": status,
            "current_status": current_status,
        },
    }


# ---------------------------------------------------------------------------
# Test 1: MostStatusEventsWinsStrategy is importable from ticket_reducer module
#         and simple majority: env with more net STATUS transitions wins
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_most_status_events_wins_simple_majority(reducer: ModuleType) -> None:
    """Env-A has 3 net STATUS transitions, env-B has 1; env-A's latest STATUS wins.

    RED: MostStatusEventsWinsStrategy does not exist yet — AttributeError expected
    until dso-b0ku implements it.

    Setup:
      env-A: CREATE → in_progress → review → closed  (3 net transitions)
      env-B: CREATE → in_progress                     (1 net transition)
    Expected: final status = "closed" (env-A wins by net transition count)
    """
    assert hasattr(reducer, "MostStatusEventsWinsStrategy"), (
        "MostStatusEventsWinsStrategy not found in ticket-reducer.py — "
        "this is the expected RED state before dso-b0ku implementation."
    )

    MostStatusEventsWinsStrategy = getattr(reducer, "MostStatusEventsWinsStrategy")
    strategy = MostStatusEventsWinsStrategy()

    env_a = "env-aaaa-0000-0000-0000-000000000001"
    env_b = "env-bbbb-0000-0000-0000-000000000002"

    events = [
        # env-A: 3 net STATUS transitions
        _make_create_event(env_a, uuid="create-aaa", ts=1000),
        _make_status_event(env_a, "status-a1", ts=2000, status="in_progress"),
        _make_status_event(
            env_a,
            "status-a2",
            ts=3000,
            status="review",
            current_status="in_progress",
        ),
        _make_status_event(
            env_a, "status-a3", ts=4000, status="closed", current_status="review"
        ),
        # env-B: 1 net STATUS transition
        _make_create_event(env_b, uuid="create-bbb", ts=1000),
        _make_status_event(env_b, "status-b1", ts=2500, status="in_progress"),
    ]

    result = strategy.resolve(events)

    # The resolved event list should contain env-A's STATUS events (the winner)
    status_events = [e for e in result if e.get("event_type") == "STATUS"]
    last_status = status_events[-1] if status_events else None
    assert last_status is not None, "resolve() returned no STATUS events"
    assert last_status["data"]["status"] == "closed", (
        f"Expected final status 'closed' (env-A with 3 net transitions wins), "
        f"but got '{last_status['data']['status']}'. "
        f"env-A has 3 net transitions; env-B has 1."
    )


# ---------------------------------------------------------------------------
# Test 2: Net transitions count, not raw event count
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_net_transitions_not_raw_events(reducer: ModuleType) -> None:
    """Env with 5 raw STATUS events but 1 net transition loses to env with 2 net transitions.

    RED: MostStatusEventsWinsStrategy does not exist yet.

    "Net transitions" = unique status changes (each distinct target status counts once).
    Raw event count must NOT be used as the tiebreaker.

    Setup:
      env-A: 5 raw STATUS events that oscillate back and forth → 1 net transition
             (open → in_progress → open → in_progress → open → in_progress)
             The repeated cycling means only 1 unique net transition forward.
      env-B: 2 raw STATUS events → 2 net transitions
             (open → in_progress → closed)
    Expected: env-B wins (2 net transitions > 1 net transition)
    """
    assert hasattr(reducer, "MostStatusEventsWinsStrategy"), (
        "MostStatusEventsWinsStrategy not found in ticket-reducer.py — "
        "this is the expected RED state before dso-b0ku implementation."
    )

    MostStatusEventsWinsStrategy = getattr(reducer, "MostStatusEventsWinsStrategy")
    strategy = MostStatusEventsWinsStrategy()

    env_a = "env-aaaa-0000-0000-0000-000000000001"
    env_b = "env-bbbb-0000-0000-0000-000000000002"

    events = [
        _make_create_event(env_a, uuid="create-aaa", ts=1000),
        # env-A: 5 raw STATUS events, but oscillates — net unique transitions = 1
        _make_status_event(env_a, "status-a1", ts=2000, status="in_progress"),
        _make_status_event(
            env_a,
            "status-a2",
            ts=2100,
            status="open",
            current_status="in_progress",
        ),
        _make_status_event(
            env_a, "status-a3", ts=2200, status="in_progress", current_status="open"
        ),
        _make_status_event(
            env_a,
            "status-a4",
            ts=2300,
            status="open",
            current_status="in_progress",
        ),
        _make_status_event(
            env_a, "status-a5", ts=2400, status="in_progress", current_status="open"
        ),
        _make_create_event(env_b, uuid="create-bbb", ts=1000),
        # env-B: 2 raw STATUS events, 2 net transitions (open→in_progress, in_progress→closed)
        _make_status_event(env_b, "status-b1", ts=3000, status="in_progress"),
        _make_status_event(
            env_b,
            "status-b2",
            ts=4000,
            status="closed",
            current_status="in_progress",
        ),
    ]

    result = strategy.resolve(events)

    status_events = [e for e in result if e.get("event_type") == "STATUS"]
    last_status = status_events[-1] if status_events else None
    assert last_status is not None, "resolve() returned no STATUS events"
    assert last_status["data"]["status"] == "closed", (
        f"Expected final status 'closed' (env-B with 2 net transitions wins over "
        f"env-A with 1 net transition despite having 5 raw events), "
        f"but got '{last_status['data']['status']}'."
    )


# ---------------------------------------------------------------------------
# Test 3: Timestamp tiebreaker when net transition counts are equal
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_timestamp_tiebreaker(reducer: ModuleType) -> None:
    """Two envs with equal net transitions; latest timestamp wins.

    RED: MostStatusEventsWinsStrategy does not exist yet.

    Setup:
      env-A: 2 net transitions, latest STATUS at ts=3000, final status="review"
      env-B: 2 net transitions, latest STATUS at ts=5000, final status="closed"
    Expected: env-B wins (tie on net transitions → latest timestamp wins)
    """
    assert hasattr(reducer, "MostStatusEventsWinsStrategy"), (
        "MostStatusEventsWinsStrategy not found in ticket-reducer.py — "
        "this is the expected RED state before dso-b0ku implementation."
    )

    MostStatusEventsWinsStrategy = getattr(reducer, "MostStatusEventsWinsStrategy")
    strategy = MostStatusEventsWinsStrategy()

    env_a = "env-aaaa-0000-0000-0000-000000000001"
    env_b = "env-bbbb-0000-0000-0000-000000000002"

    events = [
        _make_create_event(env_a, uuid="create-aaa", ts=1000),
        # env-A: 2 net transitions, last at ts=3000
        _make_status_event(env_a, "status-a1", ts=2000, status="in_progress"),
        _make_status_event(
            env_a,
            "status-a2",
            ts=3000,
            status="review",
            current_status="in_progress",
        ),
        _make_create_event(env_b, uuid="create-bbb", ts=1000),
        # env-B: 2 net transitions, last at ts=5000
        _make_status_event(env_b, "status-b1", ts=4000, status="in_progress"),
        _make_status_event(
            env_b,
            "status-b2",
            ts=5000,
            status="closed",
            current_status="in_progress",
        ),
    ]

    result = strategy.resolve(events)

    status_events = [e for e in result if e.get("event_type") == "STATUS"]
    last_status = status_events[-1] if status_events else None
    assert last_status is not None, "resolve() returned no STATUS events"
    assert last_status["data"]["status"] == "closed", (
        f"Expected final status 'closed' (env-B has later timestamp ts=5000 vs "
        f"env-A ts=3000 on equal net transitions), "
        f"but got '{last_status['data']['status']}'."
    )


# ---------------------------------------------------------------------------
# Test 4: Bridge env is excluded from net transition count
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_env_excluded(reducer: ModuleType) -> None:
    """Bridge env ID is excluded from net transition count; non-bridge env wins even with fewer raw events.

    RED: MostStatusEventsWinsStrategy does not exist yet.

    A "bridge env" is an environment whose ID matches the bridge env pattern
    (e.g., "bridge" in env_id, or env_id == BRIDGE_ENV_ID sentinel). Bridge envs
    act as sync coordinators and must not contribute to the winner selection.

    Setup:
      bridge-env: 5 raw STATUS events (3 net transitions) — EXCLUDED from count
      env-A: 1 raw STATUS event (1 net transition)
    Expected: env-A wins — only non-bridge envs are counted. bridge-env's events
    are excluded from the net-transition count.

    The bridge env ID used here follows the convention that the strategy recognises
    as a bridge: env_id contains "bridge" substring (implementation may vary).
    """
    assert hasattr(reducer, "MostStatusEventsWinsStrategy"), (
        "MostStatusEventsWinsStrategy not found in ticket-reducer.py — "
        "this is the expected RED state before dso-b0ku implementation."
    )

    MostStatusEventsWinsStrategy = getattr(reducer, "MostStatusEventsWinsStrategy")

    bridge_env = "bridge-env-0000-0000-0000-000000000099"
    env_a = "env-aaaa-0000-0000-0000-000000000001"

    # Pass bridge_env_id to the constructor so the strategy knows which env to exclude
    strategy = MostStatusEventsWinsStrategy(bridge_env_id=bridge_env)

    events = [
        _make_create_event(bridge_env, uuid="create-bridge", ts=1000),
        # bridge-env: 3 net transitions — should be excluded
        _make_status_event(bridge_env, "status-bridge1", ts=2000, status="in_progress"),
        _make_status_event(
            bridge_env,
            "status-bridge2",
            ts=3000,
            status="review",
            current_status="in_progress",
        ),
        _make_status_event(
            bridge_env,
            "status-bridge3",
            ts=4000,
            status="closed",
            current_status="review",
        ),
        _make_create_event(env_a, uuid="create-aaa", ts=1000),
        # env-A: 1 net transition — should win because bridge is excluded
        _make_status_event(env_a, "status-a1", ts=2500, status="in_progress"),
    ]

    result = strategy.resolve(events)

    # env-A's STATUS events should be in the result (bridge excluded)
    env_a_status_events = [
        e
        for e in result
        if e.get("event_type") == "STATUS" and e.get("env_id") == env_a
    ]
    assert len(env_a_status_events) > 0, (
        "Expected env-A's STATUS events in resolve() result, but found none. "
        "Bridge env should be excluded, leaving env-A as the sole participant."
    )

    # Bridge env STATUS events should NOT drive the winner
    status_events = [e for e in result if e.get("event_type") == "STATUS"]
    last_status = status_events[-1] if status_events else None
    assert last_status is not None, "resolve() returned no STATUS events"
    # env-A's final status is "in_progress" (its only STATUS event)
    assert last_status["data"]["status"] == "in_progress", (
        f"Expected final status 'in_progress' (env-A wins; bridge-env excluded), "
        f"but got '{last_status['data']['status']}'."
    )


# ---------------------------------------------------------------------------
# Test 5: Single env — no conflict, returns latest STATUS unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_single_env_no_conflict(reducer: ModuleType) -> None:
    """Single env input — no conflict, resolve() returns events with latest STATUS unchanged.

    RED: MostStatusEventsWinsStrategy does not exist yet.

    When there is only one environment in the event list, there is no conflict to
    resolve. The strategy must pass through the events unchanged (deduped + sorted),
    and the final STATUS must be the last status from that env.
    """
    assert hasattr(reducer, "MostStatusEventsWinsStrategy"), (
        "MostStatusEventsWinsStrategy not found in ticket-reducer.py — "
        "this is the expected RED state before dso-b0ku implementation."
    )

    MostStatusEventsWinsStrategy = getattr(reducer, "MostStatusEventsWinsStrategy")
    strategy = MostStatusEventsWinsStrategy()

    env_a = "env-aaaa-0000-0000-0000-000000000001"

    events = [
        _make_create_event(env_a, uuid="create-aaa", ts=1000),
        _make_status_event(env_a, "status-a1", ts=2000, status="in_progress"),
        _make_status_event(
            env_a,
            "status-a2",
            ts=3000,
            status="closed",
            current_status="in_progress",
        ),
    ]

    result = strategy.resolve(events)

    assert result is not None, "resolve() must not return None for a single-env input"
    assert len(result) == 3, (
        f"Expected 3 events returned (no events dropped for single env), "
        f"but got {len(result)}. Full result: {result}"
    )

    status_events = [e for e in result if e.get("event_type") == "STATUS"]
    last_status = status_events[-1] if status_events else None
    assert last_status is not None, "resolve() returned no STATUS events"
    assert last_status["data"]["status"] == "closed", (
        f"Expected final status 'closed' (latest STATUS from single env), "
        f"but got '{last_status['data']['status']}'."
    )

    # Events must be in ascending timestamp order (consistent with strategy contract)
    timestamps = [e["timestamp"] for e in result]
    assert timestamps == sorted(timestamps), (
        f"Expected events sorted ascending by timestamp, but got: {timestamps}"
    )
