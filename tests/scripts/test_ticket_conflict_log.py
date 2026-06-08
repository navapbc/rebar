"""RED tests for ticket-conflict-log.py.

These tests are RED — they test functionality that does not yet exist.
All test functions must FAIL before ticket-conflict-log.py is implemented.

The logger is expected to expose a single callable:
    log_conflict_resolution(
        tracker_dir, ticket_id, env_ids, event_counts, winning_state,
        bridge_env_excluded=False
    ) -> None

Contract:
  - Appends one JSON object per call (JSONL format) to
    <tracker_dir>/conflict-resolutions.jsonl
  - Each record includes: ticket_id, env_ids, event_counts, winning_state,
    timestamp, resolution_method
  - When bridge_env_excluded=True, record includes bridge_env_excluded: true
  - Write failures are non-fatal (returns None without raising)

Test: python3 -m pytest tests/scripts/test_ticket_conflict_log.py -q
All tests must return non-zero until ticket-conflict-log.py is implemented.
"""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading — filename has hyphens so we use importlib
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket-conflict-log.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_conflict_log", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def conflict_log() -> ModuleType:
    """Return the ticket-conflict-log module, failing all tests if absent (RED)."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"ticket-conflict-log.py not found at {SCRIPT_PATH} — "
            "this is expected RED state; implement the script to make tests pass."
        )
    return _load_module()


# ---------------------------------------------------------------------------
# Test 1: log_conflict_resolution writes a record with required fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_conflict_log_records_resolution(
    tmp_path: Path, conflict_log: ModuleType
) -> None:
    """log_conflict_resolution writes a record with all required fields."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    env_ids = [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    ]
    event_counts = {
        "00000000-0000-4000-8000-000000000001": 3,
        "00000000-0000-4000-8000-000000000002": 5,
    }
    winning_state = {"status": "closed", "ticket_id": "tkt-abc"}

    conflict_log.log_conflict_resolution(
        tracker_dir=str(tracker_dir),
        ticket_id="tkt-abc",
        env_ids=env_ids,
        event_counts=event_counts,
        winning_state=winning_state,
    )

    log_file = tracker_dir / "conflict-resolutions.jsonl"
    assert log_file.exists(), "conflict-resolutions.jsonl must be created"

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, "Exactly one JSONL record expected"

    record = json.loads(lines[0])
    assert record["ticket_id"] == "tkt-abc"
    assert record["env_ids"] == env_ids
    assert record["event_counts"] == event_counts
    assert record["winning_state"] == winning_state
    assert "timestamp" in record, "record must include timestamp field"
    assert "resolution_method" in record, "record must include resolution_method field"


# ---------------------------------------------------------------------------
# Test 2: log file uses JSONL format — multiple appended records
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_conflict_log_format_is_jsonl(tmp_path: Path, conflict_log: ModuleType) -> None:
    """Multiple calls append one JSON object per line (JSONL)."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    env_ids = ["00000000-0000-4000-8000-000000000001"]
    event_counts = {"00000000-0000-4000-8000-000000000001": 2}

    conflict_log.log_conflict_resolution(
        tracker_dir=str(tracker_dir),
        ticket_id="tkt-001",
        env_ids=env_ids,
        event_counts=event_counts,
        winning_state={"status": "open"},
    )
    conflict_log.log_conflict_resolution(
        tracker_dir=str(tracker_dir),
        ticket_id="tkt-002",
        env_ids=env_ids,
        event_counts=event_counts,
        winning_state={"status": "closed"},
    )

    log_file = tracker_dir / "conflict-resolutions.jsonl"
    assert log_file.exists(), "conflict-resolutions.jsonl must be created"

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, "Two calls must produce exactly two JSONL lines"

    # Each line must be valid, independent JSON
    records = [json.loads(line) for line in lines]
    assert records[0]["ticket_id"] == "tkt-001"
    assert records[1]["ticket_id"] == "tkt-002"


# ---------------------------------------------------------------------------
# Test 3: default log path is <tracker_dir>/conflict-resolutions.jsonl
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_conflict_log_default_path(tmp_path: Path, conflict_log: ModuleType) -> None:
    """Log file defaults to <tracker_dir>/conflict-resolutions.jsonl."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    conflict_log.log_conflict_resolution(
        tracker_dir=str(tracker_dir),
        ticket_id="tkt-xyz",
        env_ids=["00000000-0000-4000-8000-000000000001"],
        event_counts={"00000000-0000-4000-8000-000000000001": 1},
        winning_state={"status": "open"},
    )

    expected_path = tracker_dir / "conflict-resolutions.jsonl"
    assert expected_path.exists(), (
        f"Log file must be at {expected_path}; "
        "no alternative path must be used by default"
    )


# ---------------------------------------------------------------------------
# Test 4: bridge_env_excluded=True adds field to record
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_conflict_log_bridge_env_noted(
    tmp_path: Path, conflict_log: ModuleType
) -> None:
    """When bridge_env_excluded=True, record includes bridge_env_excluded: true."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    conflict_log.log_conflict_resolution(
        tracker_dir=str(tracker_dir),
        ticket_id="tkt-bridge",
        env_ids=["00000000-0000-4000-8000-000000000001"],
        event_counts={"00000000-0000-4000-8000-000000000001": 4},
        winning_state={"status": "in_progress"},
        bridge_env_excluded=True,
    )

    log_file = tracker_dir / "conflict-resolutions.jsonl"
    record = json.loads(log_file.read_text(encoding="utf-8").strip())

    assert record.get("bridge_env_excluded") is True, (
        "record must include bridge_env_excluded: true when bridge env was excluded"
    )


# ---------------------------------------------------------------------------
# Test 5: write failure is non-fatal
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_conflict_log_write_failure_is_non_fatal(
    tmp_path: Path, conflict_log: ModuleType
) -> None:
    """Pass a non-writable tracker_dir; log_conflict_resolution returns None without raising."""
    tracker_dir = tmp_path / "readonly_tracker"
    tracker_dir.mkdir()
    # Remove write permission so the JSONL file cannot be created
    tracker_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)

    try:
        result = conflict_log.log_conflict_resolution(
            tracker_dir=str(tracker_dir),
            ticket_id="tkt-fail",
            env_ids=["00000000-0000-4000-8000-000000000001"],
            event_counts={"00000000-0000-4000-8000-000000000001": 1},
            winning_state={"status": "open"},
        )
        assert result is None, (
            "log_conflict_resolution must return None on write failure"
        )
    except Exception as exc:
        pytest.fail(
            f"log_conflict_resolution must not raise on write failure, got: {exc}"
        )
    finally:
        # Restore permissions so tmp_path cleanup can remove the directory
        tracker_dir.chmod(stat.S_IRWXU)
