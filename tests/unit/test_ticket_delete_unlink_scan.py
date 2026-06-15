"""Unit tests for the delete-time UNLINK scan.

Tier E E7d: the bash-era ``ticket-delete-unlink-scan.py`` standalone helper was
a thin CLI wrapper over the in-process logic, which now lives in
``rebar._commands.delete.scan_and_write_unlinks``. These tests call that function
directly instead of subprocessing the (deleted) helper.

Verifies that the scan:
  (a) writes UNLINK events for inbound LINKs pointing at the deleted ticket
  (b) writes UNLINK events for outbound LINKs from the deleted ticket
  (c) does NOT write duplicate UNLINKs when a LINK is already cancelled
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from rebar._commands.delete import scan_and_write_unlinks

_ENV_ID = "eeee-0000-4000-8000-000000000001"
_AUTHOR = "test-user"


def _write_event(ticket_dir: Path, event_type: str, data: dict) -> Path:
    ts = time.time_ns()
    ev_uuid = str(uuid.uuid4())
    fname = f"{ts}-{ev_uuid}-{event_type}.json"
    event = {
        "event_type": event_type,
        "timestamp": ts,
        "uuid": ev_uuid,
        "env_id": _ENV_ID,
        "author": _AUTHOR,
        "data": data,
    }
    dest = ticket_dir / fname
    dest.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    return dest


def _make_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    return tracker


def _make_ticket(tracker: Path, ticket_id: str) -> Path:
    d = tracker / ticket_id
    d.mkdir()
    _write_event(d, "CREATE", {"ticket_type": "task", "title": f"Ticket {ticket_id}"})
    return d


def _run_helper(tracker: Path, deleted_id: str) -> tuple[int, list[str]]:
    """Run the in-process UNLINK scan and return (exit_code, list_of_written_paths).

    The deleted ticket-delete-unlink-scan.py CLI printed one written path per line
    on stdout and exited 0 on success; scan_and_write_unlinks returns that same
    list of paths directly, so we map a successful return to rc=0.
    """
    paths = scan_and_write_unlinks(str(tracker), deleted_id, _ENV_ID, _AUTHOR)
    return 0, [p for p in paths if p]


@pytest.mark.unit
def test_unlink_scan_inbound_link_writes_unlink_file(tmp_path: Path) -> None:
    """Inbound link (A -> deleted_ticket) produces an UNLINK in A's directory.

    RED marker — test_unlink_scan_inbound_link_writes_unlink_file
    Must FAIL before ticket-delete-unlink-scan.py is created.
    """
    tracker = _make_tracker(tmp_path)
    ticket_a = _make_ticket(tracker, "aaaa-0001-0000-0000")
    _make_ticket(tracker, "bbbb-0002-0000-0000")
    deleted_id = "bbbb-0002-0000-0000"

    link_uuid = str(uuid.uuid4())
    ts = time.time_ns()
    (ticket_a / f"{ts}-{link_uuid}-LINK.json").write_text(
        json.dumps(
            {
                "event_type": "LINK",
                "timestamp": ts,
                "uuid": link_uuid,
                "env_id": _ENV_ID,
                "author": _AUTHOR,
                "data": {"target_id": deleted_id, "relation": "relates_to"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc, written_paths = _run_helper(tracker, deleted_id)

    assert rc == 0, f"Helper exited {rc}"
    assert len(written_paths) >= 1, f"Expected at least 1 UNLINK path printed; got {written_paths}"
    unlink_files = list(ticket_a.glob("*-UNLINK.json"))
    assert len(unlink_files) >= 1, (
        f"Expected UNLINK file in ticket_a dir; found: {list(ticket_a.iterdir())}"
    )


@pytest.mark.unit
def test_unlink_scan_outbound_link_writes_unlink_file(tmp_path: Path) -> None:
    """Outbound link (deleted_ticket -> B) produces an UNLINK in deleted_ticket's directory."""
    tracker = _make_tracker(tmp_path)
    deleted_ticket = _make_ticket(tracker, "cccc-0003-0000-0000")
    _make_ticket(tracker, "dddd-0004-0000-0000")
    deleted_id = "cccc-0003-0000-0000"
    target_id = "dddd-0004-0000-0000"

    link_uuid = str(uuid.uuid4())
    ts = time.time_ns()
    link_fname = f"{ts}-{link_uuid}-LINK.json"
    (deleted_ticket / link_fname).write_text(
        json.dumps(
            {
                "event_type": "LINK",
                "timestamp": ts,
                "uuid": link_uuid,
                "env_id": _ENV_ID,
                "author": _AUTHOR,
                "data": {"target_id": target_id, "relation": "blocks"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc, written_paths = _run_helper(tracker, deleted_id)

    assert rc == 0, f"Helper exited {rc}"
    assert len(written_paths) >= 1, f"Expected at least 1 UNLINK path printed; got {written_paths}"
    unlink_files = list(deleted_ticket.glob("*-UNLINK.json"))
    assert len(unlink_files) >= 1, (
        f"Expected UNLINK file in deleted_ticket dir; found: {list(deleted_ticket.iterdir())}"
    )


@pytest.mark.unit
def test_unlink_scan_already_cancelled_link_is_skipped(tmp_path: Path) -> None:
    """A LINK that already has a matching UNLINK is not re-unlinked (no duplicate)."""
    tracker = _make_tracker(tmp_path)
    ticket_a = _make_ticket(tracker, "eeee-0005-0000-0000")
    _make_ticket(tracker, "ffff-0006-0000-0000")
    deleted_id = "ffff-0006-0000-0000"

    link_uuid = str(uuid.uuid4())
    ts1 = time.time_ns()
    (ticket_a / f"{ts1}-{link_uuid}-LINK.json").write_text(
        json.dumps(
            {
                "event_type": "LINK",
                "timestamp": ts1,
                "uuid": link_uuid,
                "env_id": _ENV_ID,
                "author": _AUTHOR,
                "data": {"target_id": deleted_id, "relation": "relates_to"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ts2 = time.time_ns() + 1
    unlink_uuid = str(uuid.uuid4())
    (ticket_a / f"{ts2}-{unlink_uuid}-UNLINK.json").write_text(
        json.dumps(
            {
                "event_type": "UNLINK",
                "timestamp": ts2,
                "uuid": unlink_uuid,
                "env_id": _ENV_ID,
                "author": _AUTHOR,
                "data": {"link_uuid": link_uuid, "target_id": deleted_id},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc, written_paths = _run_helper(tracker, deleted_id)

    assert rc == 0, f"Helper exited {rc}"
    new_unlinks = [p for p in written_paths if Path(p).name != f"{ts2}-{unlink_uuid}-UNLINK.json"]
    assert len(new_unlinks) == 0, (
        f"Expected no new UNLINK files for already-cancelled link; got {new_unlinks}"
    )
