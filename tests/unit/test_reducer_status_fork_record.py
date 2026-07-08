"""The reducer records a resolved STATUS fork in PURE derived state (story 3003 /
audit reliability #1).

A cross-clone claim race resolves deterministically at replay (lower-UUID-wins). This
test asserts the resolution is recorded as a dict entry in `status_fork_resolutions` on
the reduced ticket, and that the record is PURE — byte-identical across repeated replays
(a growing side effect would break idempotency and the fsck/show surfacing).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit

_UUID = "3f2a1b4c-5e6d-7f8a-9b0c-1d2e3f4a5b6c"
_UUID2 = "aabbccdd-1122-3344-5566-778899aabbcc"


def _write_event(ticket_dir: Path, timestamp: int, uuid: str, event_type: str, data: dict) -> None:
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test User",
        "data": data,
    }
    (ticket_dir / f"{timestamp}-{uuid}-{event_type}.json").write_text(json.dumps(payload))


def _forked_ticket(tmp_path: Path) -> Path:
    ticket_dir = tmp_path / "tkt-fork"
    ticket_dir.mkdir()
    _write_event(
        ticket_dir,
        1742605200,
        _UUID,
        "CREATE",
        {"ticket_type": "task", "title": "Fork test", "parent_id": None},
    )
    # A STATUS event whose current_status disagrees with the compiled status triggers
    # fork detection + tie-break resolution (same shape as the reducer's own fork test).
    _write_event(
        ticket_dir,
        1742605300,
        _UUID2,
        "STATUS",
        {"status": "closed", "current_status": "in_progress"},
    )
    return ticket_dir


def test_resolved_fork_is_recorded_as_a_dict_entry(tmp_path: Path):
    state = reduce_ticket(_forked_ticket(tmp_path))
    assert state is not None
    forks = state.get("status_fork_resolutions")
    assert isinstance(forks, list) and len(forks) == 1, f"expected one fork record, got {forks!r}"
    entry = forks[0]
    # A DICT with these keys (not a set of key-name strings).
    assert isinstance(entry, dict)
    assert set(entry) == {"winner_uuid", "dropped_uuid"}
    # loser_env_id is intentionally NOT stored (claim-loss uses assignee instead).
    assert "loser_env_id" not in entry


def test_fork_record_is_pure_and_idempotent_across_replays(tmp_path: Path):
    ticket_dir = _forked_ticket(tmp_path)
    first = reduce_ticket(ticket_dir)["status_fork_resolutions"]
    second = reduce_ticket(ticket_dir)["status_fork_resolutions"]
    # Rebuilt identically on every replay — no growing side effect.
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert len(second) == 1
