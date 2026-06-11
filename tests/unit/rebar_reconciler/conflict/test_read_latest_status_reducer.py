"""applier._read_latest_status must agree with the reducer (ticket vary-ion-fry).

Rec 7a of the 2026-06-09 architecture review (risk R6): the reconciler read the
"previous status" (used as the optimistic-concurrency `current_status` of the
STATUS event it pushes) via a raw "last STATUS json wins" scan. That scan ignores
two things the canonical reducer handles, so it can disagree with reduce_ticket on:

  (a) a COMPACTED ticket — its STATUS events are folded into a SNAPSHOT and the
      standalone STATUS files are gone, so the scan sees none and returns "open";
  (b) a STATUS FORK — two STATUS events sharing a parent are resolved by the
      lexically-lower event UUID, not by file order.

Both build a tracker as a plain directory of event files (reduce_ticket and
_read_latest_status both read files directly — no git needed) and assert
_read_latest_status == reduce_ticket(...)["status"].
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
_ENGINE_DIR = REPO_ROOT / "src" / "rebar" / "_engine"


@pytest.fixture(scope="module")
def applier():
    if not APPLIER_PATH.exists():
        pytest.fail(f"applier.py not found at {APPLIER_PATH}")
    spec = importlib.util.spec_from_file_location("applier_status_reducer", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_status_reducer"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def reduce_ticket():
    if str(_ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(_ENGINE_DIR))
    import ticket_reducer

    return ticket_reducer.reduce_ticket


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
        tdir, 5000, "5555-5555-5555-4555", "SNAPSHOT",
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
    # Two STATUS events branching from the SAME parent (both current_status="open").
    # The reducer resolves the fork by lexically-LOWER event UUID; the lower-UUID
    # event (…-4001, status=closed) wins. But a raw "last STATUS file wins" scan
    # returns the file-order-last event (…-4fff, status=in_progress) — the divergence.
    _write(
        tdir, 200, "0000-0000-0000-4001", "STATUS",
        {"current_status": "open", "status": "closed", "parent_status_uuid": parent},
    )
    _write(
        tdir, 300, "ffff-ffff-ffff-4fff", "STATUS",
        {"current_status": "open", "status": "in_progress", "parent_status_uuid": parent},
    )
    reducer_status = reduce_ticket(str(tdir))["status"]
    assert reducer_status == "closed"  # lower-UUID winner, not file-order-last
    assert applier._read_latest_status(tmp_path, tid) == reducer_status
