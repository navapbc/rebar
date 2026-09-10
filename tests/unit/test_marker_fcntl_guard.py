from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from rebar._store import lock_kernel
from rebar.reducer import marker

pytestmark = pytest.mark.unit


def _write_marker_while_body_open(
    ticket_dir: str,
    entered: Any,
    release_first: Any,
    events: Any,
) -> None:
    real_open = open

    def delaying_open(path: object, *args: object, **kwargs: object):
        handle = real_open(path, *args, **kwargs)
        if os.fspath(path).endswith(marker.ARCHIVE_MARKER_NAME):
            events.send("first-has-lock")
            entered.set()
            assert release_first.wait(timeout=5)
        return handle

    marker.open = delaying_open  # type: ignore[attr-defined]
    marker.write_marker(ticket_dir)
    events.send("first-done")


def _remove_marker_after_writer_enters(
    ticket_dir: str,
    entered: Any,
    events: Any,
) -> None:
    assert entered.wait(timeout=5)
    events.send("second-started")
    marker.remove_marker(ticket_dir)
    events.send("second-done")


def test_write_and_remove_marker_return_when_no_lock_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No platform lock primitive must not break marker best-effort semantics."""
    monkeypatch.setattr(lock_kernel, "fcntl", None)
    monkeypatch.setattr(lock_kernel, "msvcrt", None)

    marker.write_marker(str(tmp_path))
    assert marker.check_marker(str(tmp_path)) is True

    marker.remove_marker(str(tmp_path))
    assert marker.check_marker(str(tmp_path)) is False


def test_no_primitive_fallback_does_not_release_unacquired_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback path must not run unlock cleanup for an unacquired lock."""
    (tmp_path / marker.ARCHIVE_MARKER_NAME).touch()
    monkeypatch.setattr(lock_kernel, "fcntl", None)
    monkeypatch.setattr(lock_kernel, "msvcrt", None)

    def fail_release(fd: int) -> None:
        pytest.fail(f"release_exclusive called for unacquired fd {fd}")

    monkeypatch.setattr(lock_kernel, "release_exclusive", fail_release)

    marker.remove_marker(str(tmp_path))

    assert marker.check_marker(str(tmp_path)) is False


def test_marker_lock_serializes_posix_access(
    tmp_path: Path,
) -> None:
    """The POSIX locking path blocks a separate process until the holder releases."""
    if lock_kernel.fcntl is None:
        pytest.skip("POSIX fcntl lock primitive is not available")
    try:
        ctx = multiprocessing.get_context("spawn")
    except ValueError:
        pytest.skip("spawn context is required for this POSIX lock test")

    entered = ctx.Event()
    release_first = ctx.Event()
    event_rx, event_tx = ctx.Pipe(duplex=False)
    first_process = ctx.Process(
        target=_write_marker_while_body_open,
        args=(str(tmp_path), entered, release_first, event_tx),
    )
    second_process = ctx.Process(
        target=_remove_marker_after_writer_enters,
        args=(str(tmp_path), entered, event_tx),
    )
    try:
        first_process.start()
        second_process.start()
        event_tx.close()

        assert event_rx.poll(timeout=5)
        assert event_rx.recv() == "first-has-lock"
        assert event_rx.poll(timeout=5)
        assert event_rx.recv() == "second-started"
        assert not event_rx.poll(timeout=0.2)

        release_first.set()
        first_process.join(timeout=5)
        second_process.join(timeout=5)

        assert first_process.exitcode == 0
        assert second_process.exitcode == 0
        completed = []
        for _ in range(2):
            assert event_rx.poll(timeout=5)
            completed.append(event_rx.recv())
        assert sorted(completed) == ["first-done", "second-done"]
    finally:
        release_first.set()
        for process in (first_process, second_process):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        event_rx.close()
