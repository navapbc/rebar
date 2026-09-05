"""The non-blocking write-lock busy probe (bug 7084 / remediation R3).

Compaction is the store's longest lock holder and its work is optional, so
compact-on-close now stands aside when the lock is already busy. Detection needs no new
mechanism: ``_acquire_fcntl`` already polls ``fcntl.flock(LOCK_EX|LOCK_NB)`` and
``_acquire_mkdir`` already polls ``mkdir`` — the probe is those same two legs with a zero
deadline.

These tests pin the properties that make the probe safe to build a skip on: it covers
BOTH legs, it releases the first when the second is unavailable, it never leaves a lock
behind, and it fails OPEN (an unexpected error reports "not busy", degrading to today's
behaviour rather than suppressing work on a free store).
"""

from __future__ import annotations

import errno

import pytest

from rebar._store import lock as _lock
from rebar._store import lock_kernel as _kernel

pytestmark = pytest.mark.unit


def test_free_lock_is_not_busy(tmp_path):
    assert _lock.write_lock_is_busy(str(tmp_path)) is False


def test_held_lock_is_busy(tmp_path):
    handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
    try:
        assert _lock.write_lock_is_busy(str(tmp_path)) is True
    finally:
        handle.release()
    assert _lock.write_lock_is_busy(str(tmp_path)) is False


def test_the_mkdir_leg_alone_counts_as_busy(tmp_path):
    """BOTH legs must be probed. The mkdir leg is the portable window a bash-era or
    ``flock``-less writer holds, so an fcntl-only probe would report a busy store free —
    and compaction would take the lock anyway, defeating the skip."""
    (tmp_path / _lock.MKDIR_LOCK_NAME).mkdir()

    assert _lock.write_lock_is_busy(str(tmp_path)) is True


def test_probe_leaves_no_lock_behind(tmp_path):
    """The probe TAKES the mkdir leg to answer the question and must give it straight
    back — a leaked dir would wedge every later writer for the full stale ceiling."""
    assert _lock.write_lock_is_busy(str(tmp_path)) is False

    assert not (tmp_path / _lock.MKDIR_LOCK_NAME).exists()
    # And the lock is genuinely still acquirable, both legs.
    handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
    handle.release()


def test_probe_releases_the_fcntl_leg_when_the_mkdir_leg_is_busy(tmp_path):
    """The probe takes fcntl FIRST. If the mkdir leg then turns out to be held, the fcntl
    leg must be released — otherwise a probe would itself become a holder and the store
    would wedge behind a read-only question."""
    (tmp_path / _lock.MKDIR_LOCK_NAME).mkdir()
    assert _lock.write_lock_is_busy(str(tmp_path)) is True

    (tmp_path / _lock.MKDIR_LOCK_NAME).rmdir()
    # If the probe had kept the fd, this acquire in the SAME process would still pass
    # (fcntl is per-open-file-description), so assert on a fresh probe AND an acquire.
    assert _lock.write_lock_is_busy(str(tmp_path)) is False
    handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
    handle.release()


def test_probe_does_not_starve_a_real_acquire(tmp_path):
    """Repeated probing must never make the lock harder to take."""
    for _ in range(20):
        assert _lock.write_lock_is_busy(str(tmp_path)) is False
    handle = _lock.acquire(str(tmp_path), timeout=1, attempts=1)
    handle.release()


def test_probe_fails_open_on_an_unexpected_error(tmp_path, monkeypatch):
    """A probe that cannot answer must report NOT busy: the caller then behaves exactly as
    it did before R3 (attempt the work, let the real acquire arbitrate). Reporting "busy"
    on a fault would silently suppress compaction on a perfectly free store."""

    def enolck(*_a, **_kw):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(_kernel.fcntl, "flock", enolck)
    assert _lock.write_lock_is_busy(str(tmp_path)) is False


def test_probe_fails_open_when_the_lock_file_cannot_be_opened(tmp_path, monkeypatch):
    def eacces(*_a, **_kw):
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(_lock.os, "open", eacces)
    assert _lock.write_lock_is_busy(str(tmp_path)) is False


def test_probe_reports_busy_for_a_contended_fcntl_leg(tmp_path, monkeypatch):
    """EAGAIN/EACCES from flock is genuine contention — the one errno class that answers
    the question rather than signalling a fault."""

    def eagain(*_a, **_kw):
        raise OSError(errno.EAGAIN, "would block")

    monkeypatch.setattr(_kernel.fcntl, "flock", eagain)
    assert _lock.write_lock_is_busy(str(tmp_path)) is True


def test_probe_answers_a_lock_held_by_another_process(tmp_path):
    """The case that matters in the field: the holder is a DIFFERENT process, so the
    fcntl leg is genuinely contended rather than re-entrantly ours."""
    import subprocess
    import sys

    src = (
        "import sys, time\n"
        "from rebar._store import lock\n"
        "h = lock.acquire(sys.argv[1], timeout=10, attempts=1)\n"
        "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", src, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        assert _lock.write_lock_is_busy(str(tmp_path)) is True
    finally:
        proc.kill()
        proc.wait()

    # The killed holder leaves its mkdir stamp behind, so the store still reads busy —
    # reclamation of an orphan is acquire()'s job (and unchanged by R3), not the probe's.
    assert _lock.write_lock_is_busy(str(tmp_path)) is True
    assert "pid_state=not-running" in _lock.describe_lock_holder(str(tmp_path))
