"""Shared event-append module: filename contract (I2) + write-lock (I5).

Ticket pokey-matte-flute. Pins that concurrent writers — e.g. the reconciler
applier and a local agent — serialize on ``.ticket-write.lock`` and lose no
events: every append lands as a distinct, valid, atomically-written event file.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

# The unit-tier conftest puts <repo>/src/rebar/_engine on sys.path.
import event_append

_I2 = re.compile(r"^\d+-[0-9a-f-]+-[A-Z_]+\.json$")


def test_event_filename_is_the_i2_contract() -> None:
    assert event_append.event_filename(123, "u-1", "STATUS") == "123-u-1-STATUS.json"


def test_append_event_writes_valid_event_atomically(tmp_path: Path) -> None:
    tdir = tmp_path / "aaaa-aaaa-aaaa-4aaa"
    ts, uid = event_append.new_event_id()
    out = event_append.append_event(
        tdir, "COMMENT", {"body": "hi"}, timestamp=ts, uuid_str=uid, author="a", env_id="e"
    )
    assert _I2.match(out.name)
    ev = json.loads(out.read_text())
    assert ev["event_type"] == "COMMENT" and ev["data"] == {"body": "hi"}
    assert ev["uuid"] == uid and ev["timestamp"] == ts
    # No temp file left behind.
    assert not list(tdir.glob(".tmp-*"))


def test_concurrent_writers_serialize_and_lose_no_events(tmp_path: Path) -> None:
    tracker = tmp_path
    ticket_dir = tracker / "bbbb-bbbb-bbbb-4bbb"
    n = 12
    overlap = []          # records any concurrent entry into the critical section
    in_critical = {"n": 0}
    guard = threading.Lock()  # protects the in_critical counter (test bookkeeping only)
    start = threading.Barrier(n)

    def worker(i: int) -> None:
        start.wait()  # maximize contention
        ts, uid = event_append.new_event_id()
        # Distinct timestamps so filenames never collide even on a fast clock.
        ts += i
        with event_append.write_lock(tracker, timeout=30.0):
            with guard:
                in_critical["n"] += 1
                if in_critical["n"] != 1:
                    overlap.append(in_critical["n"])
            time.sleep(0.005)  # widen the window the write-lock must protect
            event_append.append_event(
                ticket_dir, "COMMENT", {"i": i}, timestamp=ts, uuid_str=uid, author="w"
            )
            with guard:
                in_critical["n"] -= 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The write lock must have prevented any concurrent critical-section entry.
    assert not overlap, f"write_lock allowed concurrent writers: {overlap}"
    # Every append landed as a distinct, valid event file — none lost.
    files = [p for p in ticket_dir.glob("*.json")]
    assert len(files) == n, f"expected {n} events, found {len(files)}"
    uuids = {json.loads(p.read_text())["uuid"] for p in files}
    assert len(uuids) == n
    assert not list(ticket_dir.glob(".tmp-*"))
