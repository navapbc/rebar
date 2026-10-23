"""The applier reads latest status through reducer semantics: compacted snapshots retain
status, and fork winners follow event UUID order rather than file order."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"


@pytest.fixture(scope="module")
def applier():
    if not APPLIER_PATH.exists():
        pytest.fail(f"applier.py not found at {APPLIER_PATH}")
    return importlib.import_module("rebar.reconciler.applier")


@pytest.fixture(scope="module")
def reduce_ticket():
    import rebar.reducer

    return rebar.reducer.reduce_ticket


def _write(ticket_dir: Path, ts: int, uuid: str, event_type: str, data: dict) -> None:
    event = {
        "timestamp": ts,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": "00000000-0000-4000-8000-000000000000",
        "author": "t",
        "data": data,
    }
    (ticket_dir / f"{ts}-{uuid}-{event_type}.json").write_text(
        json.dumps(event, ensure_ascii=False), encoding="utf-8"
    )


def test_compacted_ticket_status_matches_reducer(applier, reduce_ticket, tmp_path: Path) -> None:
    tid = "aaaa-aaaa-aaaa-4aaa"
    tdir = tmp_path / tid
    tdir.mkdir()
    # A compacted ticket: a single SNAPSHOT whose compiled_state is closed; the
    # original CREATE/STATUS files were retired into it (none remain on disk).
    _write(
        tdir,
        5000,
        "5555-5555-5555-4555",
        "SNAPSHOT",
        {
            "compiled_state": {
                "ticket_id": tid,
                "ticket_type": "task",
                "title": "Compacted",
                "status": "closed",
                "priority": 2,
            },
            "source_event_uuids": ["1111-1111-1111-4111", "2222-2222-2222-4222"],
        },
    )
    reducer_status = reduce_ticket(str(tdir))["status"]
    assert reducer_status == "closed"  # sanity: the reducer reads the SNAPSHOT
    assert applier._read_latest_status(tmp_path, tid) == reducer_status


def test_status_fork_resolution_matches_reducer(applier, reduce_ticket, tmp_path: Path) -> None:
    tid = "bbbb-bbbb-bbbb-4bbb"
    tdir = tmp_path / tid
    tdir.mkdir()
    create_uuid = "0000-0000-0000-4000"
    parent = "aaaa-aaaa-aaaa-4aaa"
    _write(tdir, 100, create_uuid, "CREATE", {"ticket_type": "task", "title": "Forked"})
    # Forked STATUS events share a parent; the lower event UUID wins, regardless of file order.
    _write(
        tdir,
        200,
        "0000-0000-0000-4001",
        "STATUS",
        {"current_status": "open", "status": "closed", "parent_status_uuid": parent},
    )
    _write(
        tdir,
        300,
        "ffff-ffff-ffff-4fff",
        "STATUS",
        {"current_status": "open", "status": "in_progress", "parent_status_uuid": parent},
    )
    reducer_status = reduce_ticket(str(tdir))["status"]
    assert reducer_status == "closed"  # lower-UUID winner, not file-order-last
    assert applier._read_latest_status(tmp_path, tid) == reducer_status
