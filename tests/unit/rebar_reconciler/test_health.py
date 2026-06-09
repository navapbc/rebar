"""Unit tests for rebar_reconciler/health.py.

Tests cover:
  - test_record_pass_creates_json_file: record_pass() writes a JSON file at
    bridge_state/health/<pass_id>.json under repo_root.
  - test_record_pass_schema_version: the JSON has schema_version=1.
  - test_record_pass_fields: the JSON contains all required fields with
    correct values.
  - test_record_pass_timestamp_ns_positive: timestamp_ns is a positive integer.
  - test_capture_baseline_is_callable: capture_baseline() is implemented and
    returns a Path (task 15b8 stub removed). Full baseline coverage lives in
    test_health_baseline.py.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
HEALTH_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "health.py"
)


def _load_health() -> ModuleType:
    spec = importlib.util.spec_from_file_location("health", HEALTH_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def health() -> ModuleType:
    return _load_health()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_record_pass_creates_json_file(health: ModuleType, tmp_path: Path) -> None:
    """record_pass() writes a JSON file to bridge_state/health/<pass_id>.json."""
    pass_id = "test-pass-001"
    out_path = health.record_pass(
        pass_id=pass_id,
        pre_fsck=10,
        post_fsck=8,
        per_type_counts={"epic": 2, "story": 3, "task": 2, "bug": 1},
        local_mutation_count=5,
        repo_root=tmp_path,
    )
    expected = tmp_path / "bridge_state" / "health" / f"{pass_id}.json"
    assert out_path == expected
    assert expected.exists(), f"Expected file not found: {expected}"


def test_record_pass_schema_version(health: ModuleType, tmp_path: Path) -> None:
    """The written JSON has schema_version=1."""
    pass_id = "test-pass-schema"
    health.record_pass(
        pass_id=pass_id,
        pre_fsck=0,
        post_fsck=0,
        per_type_counts={},
        local_mutation_count=0,
        repo_root=tmp_path,
    )
    data = json.loads(
        (tmp_path / "bridge_state" / "health" / f"{pass_id}.json").read_text()
    )
    assert data["schema_version"] == 1


def test_record_pass_fields(health: ModuleType, tmp_path: Path) -> None:
    """The JSON contains all required fields with the values passed in."""
    pass_id = "test-pass-fields"
    pre_fsck = 42
    post_fsck = 38
    per_type_counts = {"epic": 5, "story": 10, "task": 20, "bug": 7}
    local_mutation_count = 12

    health.record_pass(
        pass_id=pass_id,
        pre_fsck=pre_fsck,
        post_fsck=post_fsck,
        per_type_counts=per_type_counts,
        local_mutation_count=local_mutation_count,
        repo_root=tmp_path,
    )
    data = json.loads(
        (tmp_path / "bridge_state" / "health" / f"{pass_id}.json").read_text()
    )

    assert data["pass_id"] == pass_id
    assert data["pre_pass_fsck_total"] == pre_fsck
    assert data["post_pass_fsck_total"] == post_fsck
    assert data["per_type_open_counts"] == per_type_counts
    assert data["local_mutation_count_at_pass"] == local_mutation_count
    assert "timestamp_ns" in data


def test_record_pass_timestamp_ns_positive(
    health: ModuleType, tmp_path: Path
) -> None:
    """timestamp_ns is a positive integer."""
    pass_id = "test-pass-ts"
    health.record_pass(
        pass_id=pass_id,
        pre_fsck=1,
        post_fsck=1,
        per_type_counts={},
        local_mutation_count=0,
        repo_root=tmp_path,
    )
    data = json.loads(
        (tmp_path / "bridge_state" / "health" / f"{pass_id}.json").read_text()
    )
    ts = data["timestamp_ns"]
    assert isinstance(ts, int), f"timestamp_ns should be int, got {type(ts)}"
    assert ts > 0, f"timestamp_ns should be positive, got {ts}"


def test_capture_baseline_is_callable(
    health: ModuleType, tmp_path: Path
) -> None:
    """capture_baseline() is implemented and returns a Path (task 15b8 stub removed)."""
    result = health.capture_baseline(pass_id="test-baseline", repo_root=tmp_path)
    assert isinstance(result, Path)
    assert result.exists()


def test_count_open_by_type_empty_tracker(health: ModuleType, tmp_path: Path) -> None:
    """count_open_by_type returns {} when .tickets-tracker/ is absent."""
    result = health.count_open_by_type(repo_root=tmp_path)
    assert result == {}


def test_count_open_by_type_counts_correctly(health: ModuleType, tmp_path: Path) -> None:
    """count_open_by_type counts open tickets by type from .tickets-tracker/.

    Events use the canonical reducer shape: type/status live under event["data"],
    not the top level.
    """
    import json as _json
    import time

    tracker = tmp_path / ".tickets-tracker"
    for tid, ttype, tstatus in [
        ("t1", "story", "open"),
        ("t2", "task", "open"),
        ("t3", "story", "closed"),
        ("t4", "bug", "open"),
        ("t5", "task", "open"),
    ]:
        d = tracker / tid
        d.mkdir(parents=True)
        ts = time.time_ns()
        (d / f"{ts}-create.json").write_text(
            _json.dumps({"event_type": "CREATE", "data": {"ticket_type": ttype}})
        )
        (d / f"{ts + 1}-status.json").write_text(
            _json.dumps({"event_type": "STATUS", "data": {"status": tstatus}})
        )

    result = health.count_open_by_type(repo_root=tmp_path)
    assert result == {"story": 1, "task": 2, "bug": 1}


def test_count_open_by_type_defaults_open_when_only_create(
    health: ModuleType, tmp_path: Path
) -> None:
    """A ticket with only a CREATE event (no STATUS yet) counts as open.

    Matches ticket_reducer/_state.py:make_initial_state which initializes
    status="open" — newly-created tickets are canonically open before any
    transition event is recorded.
    """
    import json as _json
    import time

    tracker = tmp_path / ".tickets-tracker"
    d = tracker / "fresh-ticket"
    d.mkdir(parents=True)
    ts = time.time_ns()
    (d / f"{ts}-create.json").write_text(
        _json.dumps({"event_type": "CREATE", "data": {"ticket_type": "story"}})
    )
    # No STATUS event written

    result = health.count_open_by_type(repo_root=tmp_path)
    assert result == {"story": 1}


def test_count_open_by_type_skips_non_dict_events(
    health: ModuleType, tmp_path: Path
) -> None:
    """Non-dict JSON payloads in event files do not crash the walker."""
    import json as _json
    import time

    tracker = tmp_path / ".tickets-tracker"
    d = tracker / "weird-ticket"
    d.mkdir(parents=True)
    ts = time.time_ns()
    # A scalar JSON value (not a dict) — defensive: do not crash.
    (d / f"{ts}-garbage.json").write_text(_json.dumps(42))
    (d / f"{ts + 1}-create.json").write_text(
        _json.dumps({"event_type": "CREATE", "data": {"ticket_type": "task"}})
    )

    result = health.count_open_by_type(repo_root=tmp_path)
    assert result == {"task": 1}


# ---------------------------------------------------------------------------
# Regression: order-dependency tests.
# count_open_by_type and capture_baseline both rely on the canonical
# timestamp-prefixed event filename convention so that
# sorted(glob("*.json")) == chronological order. Verify that when CREATE
# is written with a filename that sorts BEFORE STATUS (alphabetical order
# == intended chronological order), the result is correct regardless of
# wall-clock timestamps in the filename. This guards against any future
# refactor that changes from sorted-by-filename to a different ordering.
# ---------------------------------------------------------------------------


def test_count_open_by_type_alphabetical_filename_order(
    health: ModuleType, tmp_path: Path
) -> None:
    """When filenames sort alphabetically (CREATE before STATUS by name),
    count_open_by_type still applies the latest STATUS correctly."""
    import json as _json

    tracker = tmp_path / ".tickets-tracker"
    d = tracker / "alpha-ticket"
    d.mkdir(parents=True)

    # Non-timestamp-prefixed names but alphabetically sorted: a-create < b-status.
    # CREATE encountered first, then STATUS overwrites latest_status to "closed".
    (d / "a-create.json").write_text(
        _json.dumps({"event_type": "CREATE", "data": {"ticket_type": "story"}})
    )
    (d / "b-status.json").write_text(
        _json.dumps({"event_type": "STATUS", "data": {"status": "closed"}})
    )

    result = health.count_open_by_type(repo_root=tmp_path)
    # Closed ticket is excluded from open counts.
    assert result == {}


def test_count_open_by_type_multiple_status_latest_wins(
    health: ModuleType, tmp_path: Path
) -> None:
    """With timestamp-prefixed filenames, sorted order equals chronological
    order, so the latest STATUS event (numerically-largest timestamp) wins."""
    import json as _json
    import time

    tracker = tmp_path / ".tickets-tracker"
    d = tracker / "multi-status"
    d.mkdir(parents=True)
    ts = time.time_ns()

    (d / f"{ts}-create.json").write_text(
        _json.dumps({"event_type": "CREATE", "data": {"ticket_type": "task"}})
    )
    (d / f"{ts + 1}-status.json").write_text(
        _json.dumps({"event_type": "STATUS", "data": {"status": "open"}})
    )
    (d / f"{ts + 2}-status.json").write_text(
        _json.dumps({"event_type": "STATUS", "data": {"status": "closed"}})
    )
    (d / f"{ts + 3}-status.json").write_text(
        _json.dumps({"event_type": "STATUS", "data": {"status": "open"}})
    )

    # Latest STATUS is "open" (ts+3), so the ticket counts as open.
    result = health.count_open_by_type(repo_root=tmp_path)
    assert result == {"task": 1}


def test_capture_baseline_alphabetical_filename_order(
    health: ModuleType, tmp_path: Path
) -> None:
    """capture_baseline uses has_create + default-open semantics, so a ticket
    whose CREATE filename sorts before its STATUS=closed filename is correctly
    counted as closed (not open)."""
    import json as _json

    tracker = tmp_path / ".tickets-tracker"
    d = tracker / "alpha-baseline"
    d.mkdir(parents=True)

    # Alphabetical order: a-create < b-status. CREATE seen first, then STATUS
    # overwrites latest_status to "closed".
    (d / "a-create.json").write_text(
        _json.dumps({"event_type": "CREATE", "data": {"ticket_type": "task"}})
    )
    (d / "b-status.json").write_text(
        _json.dumps({"event_type": "STATUS", "data": {"status": "closed"}})
    )

    out_path = health.capture_baseline(
        pass_id="alpha-pass", repo_root=tmp_path
    )
    import json as _json2

    data = _json2.loads(out_path.read_text())
    assert data["pre_pass_fsck_total"] == 0


def test_capture_baseline_create_only_counts_open(
    health: ModuleType, tmp_path: Path
) -> None:
    """capture_baseline counts a ticket with only a CREATE event as open
    (matches the canonical reducer initial state)."""
    import json as _json

    tracker = tmp_path / ".tickets-tracker"
    d = tracker / "create-only"
    d.mkdir(parents=True)
    # CREATE only, no STATUS. Default-open semantics should fire.
    (d / "create.json").write_text(
        _json.dumps({"event_type": "CREATE", "data": {"ticket_type": "bug"}})
    )

    out_path = health.capture_baseline(
        pass_id="create-only-pass", repo_root=tmp_path
    )
    import json as _json2

    data = _json2.loads(out_path.read_text())
    assert data["pre_pass_fsck_total"] == 1
