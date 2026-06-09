"""Unit tests for capture_baseline() in rebar_reconciler/health.py.

Tests cover:
  - test_capture_baseline_creates_baseline_file: capture_baseline() writes a
    JSON file at bridge_state/health/<pass_id>_baseline.json under repo_root.
  - test_capture_baseline_fields: the baseline JSON has pass_id,
    pre_pass_fsck_total (int >= 0), and timestamp_ns.
  - test_capture_baseline_no_tickets_dir_returns_zero: when no .tickets-tracker/
    dir exists in repo_root, pre_pass_fsck_total is 0.
  - test_capture_baseline_counts_open_tickets: with a fake .tickets-tracker/,
    counts only tickets whose latest STATUS event has status 'open'.
  - test_capture_baseline_not_implemented_error_not_raised: capture_baseline()
    does NOT raise NotImplementedError.
"""

from __future__ import annotations

import importlib.util
import json
import time
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
# Helpers
# ---------------------------------------------------------------------------


def _make_ticket_event(ticket_dir: Path, filename: str, status: str) -> None:
    """Write a minimal STATUS event JSON file into ticket_dir.

    Uses the canonical reducer shape: status lives under event["data"]["status"].
    """
    ticket_dir.mkdir(parents=True, exist_ok=True)
    event = {"event_type": "STATUS", "data": {"status": status}}
    (ticket_dir / filename).write_text(json.dumps(event))


def _make_create_event(ticket_dir: Path, filename: str, ticket_type: str = "task") -> None:
    """Write a minimal CREATE event JSON file into ticket_dir."""
    ticket_dir.mkdir(parents=True, exist_ok=True)
    event = {"event_type": "CREATE", "data": {"ticket_type": ticket_type}}
    (ticket_dir / filename).write_text(json.dumps(event))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_capture_baseline_creates_baseline_file(
    health: ModuleType, tmp_path: Path
) -> None:
    """capture_baseline() writes a JSON file to bridge_state/health/<pass_id>_baseline.json."""
    pass_id = "baseline-pass-001"
    out_path = health.capture_baseline(pass_id=pass_id, repo_root=tmp_path)
    expected = tmp_path / "bridge_state" / "health" / f"{pass_id}_baseline.json"
    assert out_path == expected
    assert expected.exists(), f"Expected baseline file not found: {expected}"


def test_capture_baseline_fields(health: ModuleType, tmp_path: Path) -> None:
    """The baseline JSON has pass_id, pre_pass_fsck_total (int >= 0), and timestamp_ns."""
    pass_id = "baseline-pass-fields"
    before_ns = time.time_ns()
    health.capture_baseline(pass_id=pass_id, repo_root=tmp_path)
    after_ns = time.time_ns()

    data = json.loads(
        (tmp_path / "bridge_state" / "health" / f"{pass_id}_baseline.json").read_text()
    )

    assert data["pass_id"] == pass_id
    assert isinstance(data["pre_pass_fsck_total"], int)
    assert data["pre_pass_fsck_total"] >= 0
    assert isinstance(data["timestamp_ns"], int)
    assert before_ns <= data["timestamp_ns"] <= after_ns


def test_capture_baseline_no_tickets_dir_returns_zero(
    health: ModuleType, tmp_path: Path
) -> None:
    """When no .tickets-tracker/ dir exists, pre_pass_fsck_total is 0."""
    pass_id = "baseline-no-tracker"
    # tmp_path has no .tickets-tracker directory
    health.capture_baseline(pass_id=pass_id, repo_root=tmp_path)
    data = json.loads(
        (tmp_path / "bridge_state" / "health" / f"{pass_id}_baseline.json").read_text()
    )
    assert data["pre_pass_fsck_total"] == 0


def test_capture_baseline_counts_open_tickets(
    health: ModuleType, tmp_path: Path
) -> None:
    """With a fake .tickets-tracker/, counts only tickets with latest STATUS='open'."""
    tracker_dir = tmp_path / ".tickets-tracker"

    # Ticket A: open
    _make_ticket_event(tracker_dir / "ticket-aaa", "0001.json", "open")

    # Ticket B: open (two events — latest is still open)
    _make_ticket_event(tracker_dir / "ticket-bbb", "0001.json", "in_progress")
    _make_ticket_event(tracker_dir / "ticket-bbb", "0002.json", "open")

    # Ticket C: closed — should NOT be counted
    _make_ticket_event(tracker_dir / "ticket-ccc", "0001.json", "open")
    _make_ticket_event(tracker_dir / "ticket-ccc", "0002.json", "closed")

    # Ticket D: in_progress — should NOT be counted
    _make_ticket_event(tracker_dir / "ticket-ddd", "0001.json", "in_progress")

    pass_id = "baseline-counted"
    health.capture_baseline(pass_id=pass_id, repo_root=tmp_path)
    data = json.loads(
        (tmp_path / "bridge_state" / "health" / f"{pass_id}_baseline.json").read_text()
    )
    # Tickets A and B are open; C and D are not
    assert data["pre_pass_fsck_total"] == 2


def test_capture_baseline_not_implemented_error_not_raised(
    health: ModuleType, tmp_path: Path
) -> None:
    """capture_baseline() does NOT raise NotImplementedError."""
    # This will raise if the stub is still in place
    result = health.capture_baseline(pass_id="baseline-not-stub", repo_root=tmp_path)
    assert result is not None


def test_capture_baseline_counts_create_only_as_open(
    health: ModuleType, tmp_path: Path
) -> None:
    """A ticket with only a CREATE event (no STATUS yet) counts as open.

    Matches ticket_reducer/_state.py:make_initial_state which initializes
    status="open" — newly-created tickets are canonically open before any
    transition event is recorded.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    _make_create_event(tracker_dir / "fresh-ticket", "0001.json", "story")
    # No STATUS event

    pass_id = "baseline-create-only"
    health.capture_baseline(pass_id=pass_id, repo_root=tmp_path)
    data = json.loads(
        (tmp_path / "bridge_state" / "health" / f"{pass_id}_baseline.json").read_text()
    )
    assert data["pre_pass_fsck_total"] == 1
