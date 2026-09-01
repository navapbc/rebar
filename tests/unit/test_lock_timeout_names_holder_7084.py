"""A ``LockTimeout`` must NAME the holder (bug 7084 / remediation R5).

Field evidence on 7084: a 48s ``compact-on-close`` starved six concurrent writers, and
none of them could say what had blocked them — the holder was identified afterwards by
inferring from the tracker's commit timeline, because ``flock: could not acquire lock
after Ns`` threw away the ownership stamp the mkdir leg had already written.

R5 surfaces that EXISTING stamp; it adds no fields and no machinery. These tests pin the
reported identity, the degraded cases (absent / unrecognised / incomplete stamp — stated
plainly, never guessed), the liveness verdict that separates "a live process holds it"
from "a stale stamp", and the end-to-end behaviour under real contention.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from rebar._store import lock as _lock
from rebar._store import lock_owner as _owner


def _seed_stamp(tmp_path, stamp: str) -> str:
    lock_dir = tmp_path / _lock.MKDIR_LOCK_NAME
    lock_dir.mkdir(exist_ok=True)
    (lock_dir / _owner._MKDIR_OWNER_FILE).write_text(stamp)
    return str(lock_dir)


# --------------------------------------------------------------- what the stamp reports


def test_describes_a_held_lock_from_the_existing_stamp(tmp_path):
    """The fields the v2 stamp already carries — host, pid, start — plus hold age and a
    liveness verdict."""
    handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
    try:
        holder = _lock.describe_lock_holder(str(tmp_path))
    finally:
        handle.release()

    assert f"pid={os.getpid()}" in holder
    assert f"host={_owner._host_identity()}" in holder
    assert "held=" in holder
    assert "pid_state=" in holder


def test_describe_reports_live_only_when_start_time_corroborates_the_stamp(tmp_path, monkeypatch):
    """A pid-number hit is a live-holder diagnostic only when start time matches too."""
    monkeypatch.setattr(_owner, "_process_start_time", lambda _pid: "111")
    stamp = (
        f"{_owner._STAMP_V2_PREFIX} host={_owner._host_identity()} "
        f"ns={_owner._read_pid_namespace_id() or _owner._STAMP_UNKNOWN} "
        "pid=4321 start=111"
    )
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: True)
    _seed_stamp(tmp_path, stamp)

    assert "pid_state=live" in _lock.describe_lock_holder(str(tmp_path))


def test_describe_does_not_report_unqualified_pid_match_as_live(tmp_path, monkeypatch):
    """On no-/proc platforms a live pid number is unverified, not proof of ownership."""
    monkeypatch.setattr(_owner, "_process_start_time", lambda _pid: None)
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: True)
    stamp = (
        f"{_owner._STAMP_V2_PREFIX} host={_owner._host_identity()} "
        f"ns={_owner._read_pid_namespace_id() or _owner._STAMP_UNKNOWN} "
        "pid=4321 start=-"
    )
    _seed_stamp(tmp_path, stamp)

    holder = _lock.describe_lock_holder(str(tmp_path))
    assert "pid_state=live" not in holder
    assert "pid_state=unverified-live (start unknown)" in holder


def test_describe_does_not_report_unknown_stamped_start_as_live(tmp_path, monkeypatch):
    """A known current process still needs a stamped start time to prove ownership."""
    monkeypatch.setattr(_owner, "_process_start_time", lambda _pid: "222")
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: True)
    stamp = (
        f"{_owner._STAMP_V2_PREFIX} host={_owner._host_identity()} "
        f"ns={_owner._read_pid_namespace_id() or _owner._STAMP_UNKNOWN} "
        "pid=4321 start=-"
    )
    _seed_stamp(tmp_path, stamp)

    holder = _lock.describe_lock_holder(str(tmp_path))
    assert "pid_state=live" not in holder
    assert "pid_state=unverified-live (stamp start unknown)" in holder


def test_describe_reports_known_recycled_pid_as_not_owner(tmp_path, monkeypatch):
    """Known but differing start times prove the pid number has been recycled."""
    monkeypatch.setattr(_owner, "_process_start_time", lambda _pid: "222")
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: True)
    stamp = (
        f"{_owner._STAMP_V2_PREFIX} host={_owner._host_identity()} "
        f"ns={_owner._read_pid_namespace_id() or _owner._STAMP_UNKNOWN} "
        "pid=4321 start=111"
    )
    _seed_stamp(tmp_path, stamp)

    assert "pid_state=not-owner (recycled pid)" in _lock.describe_lock_holder(str(tmp_path))


def test_a_dead_stamped_pid_reports_not_running(tmp_path, monkeypatch):
    """The other half of the distinction: an orphaned stamp says so instead of implying a
    live holder."""
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: False)
    _seed_stamp(tmp_path, _owner._owner_stamp())

    assert "pid_state=not-running" in _lock.describe_lock_holder(str(tmp_path))


def test_a_foreign_host_stamp_is_reported_unprobeable(tmp_path):
    """We cannot observe another host's processes, so no liveness claim is made — the
    same refusal-without-proof rule reclamation follows (bug yaw-gravel-linen), so the
    message can never suggest a reclaim the lock itself would refuse."""
    _seed_stamp(tmp_path, "rebar-lock v2 host=boot-someone-else ns=1 pid=4321 start=99")

    holder = _lock.describe_lock_holder(str(tmp_path))
    assert "host=boot-someone-else" in holder
    assert "pid=4321" in holder
    assert "unprobeable (foreign host)" in holder


# ---------------------------------------------------------------------- degraded cases


def test_no_stamp_says_so_plainly(tmp_path):
    """An unheld lock, an fcntl-only (``dual_window=False``) holder, a bash-era lock, or
    the window between mkdir and the stamp write: all report absence, not a guess."""
    assert _lock.describe_lock_holder(str(tmp_path)) == "unknown (no ownership stamp)"


def test_unrecognised_stamp_shape_says_so_plainly(tmp_path):
    """The forward-compat path the module docstring promises — a shape the reader
    DECLINES (e.g. a legacy ``host:pid`` stamp, or one written by a newer rebar) is
    reported as unrecognised rather than half-parsed into a partial line."""
    _seed_stamp(tmp_path, "somehost:1234")
    assert _lock.describe_lock_holder(str(tmp_path)) == "unknown (unrecognised ownership stamp)"

    _seed_stamp(tmp_path, "rebar-lock v9 host=name-h pid=1 something=else")
    assert _lock.describe_lock_holder(str(tmp_path)) == "unknown (unrecognised ownership stamp)"


def test_incomplete_stamp_says_so_plainly(tmp_path):
    """A torn mid-write read must not be rendered as a holder with missing fields."""
    _seed_stamp(tmp_path, "rebar-lock v2 host=name-h")
    assert _lock.describe_lock_holder(str(tmp_path)) == "unknown (incomplete ownership stamp)"


def test_describe_never_raises_on_an_unreadable_stamp(tmp_path, monkeypatch):
    """Naming the holder must never become a second failure on top of the timeout being
    reported."""

    def boom(*_a, **_kw):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(_owner, "_parse_v2_stamp", boom)
    _seed_stamp(tmp_path, _owner._owner_stamp())

    assert _lock.describe_lock_holder(str(tmp_path)).startswith("unknown (")


# ------------------------------------------------------------------- the timeout itself


def test_contended_timeout_names_the_holder(tmp_path):
    """The end-to-end property 7084 asks for: a REAL contended acquire — a separate
    process holding the lock — times out with a message naming what holds it."""
    tracker = str(tmp_path)
    holder_src = (
        "import sys, time\n"
        "from rebar._store import lock\n"
        "h = lock.acquire(sys.argv[1], timeout=10, attempts=1)\n"
        "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_src, tracker], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"  # blocking wait: the lock IS taken

        with pytest.raises(_lock.LockTimeout) as excinfo:
            _lock.acquire(tracker, timeout=1, attempts=1)
    finally:
        proc.kill()
        proc.wait()

    message = str(excinfo.value)
    # The bash-parity prefix is unchanged, so anything keyed on it still matches...
    assert message.startswith("flock: could not acquire lock after 1s")
    # ...and the holder is NAMED, so diagnosis needs no commit-timeline inference.
    assert f"pid={proc.pid}" in message
    assert "pid_state=live" in message or "pid_state=unverified-live" in message
    assert excinfo.value.holder is not None


def test_timeout_without_a_readable_holder_keeps_the_original_message(tmp_path, monkeypatch):
    """An absent stamp must not fabricate a holder: the message degrades to exactly what
    it said before this change."""
    monkeypatch.setattr(_lock, "describe_lock_holder", lambda _t: "")
    handle = _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    try:
        with pytest.raises(_lock.LockTimeout) as excinfo:
            _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    finally:
        handle.release()

    assert str(excinfo.value) == "flock: could not acquire lock after 1s"
