from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from rebar._store import lock_kernel
from rebar.reducer import marker

pytestmark = pytest.mark.unit


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


def test_write_marker_returns_when_legacy_fcntl_name_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED guard for the original unguarded module-local fcntl dereference."""
    monkeypatch.setattr(marker, "fcntl", None, raising=False)

    marker.write_marker(str(tmp_path))

    assert marker.check_marker(str(tmp_path)) is True


def test_remove_marker_returns_when_legacy_fcntl_name_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED guard for the original finally-block fcntl unlock dereference."""
    (tmp_path / marker.ARCHIVE_MARKER_NAME).touch()
    monkeypatch.setattr(marker, "fcntl", None, raising=False)

    marker.remove_marker(str(tmp_path))

    assert marker.check_marker(str(tmp_path)) is False


def test_marker_lock_serializes_posix_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real locking path remains blocking: contender enters only after release."""
    entered = threading.Event()
    release_first = threading.Event()
    finished = threading.Event()
    events: list[str] = []
    real_open = open

    def delaying_open(path: object, *args: object, **kwargs: object):
        handle = real_open(path, *args, **kwargs)
        if os.fspath(path).endswith(marker.ARCHIVE_MARKER_NAME):
            events.append("first-has-lock")
            entered.set()
            assert release_first.wait(timeout=5)
        return handle

    def first() -> None:
        marker.write_marker(str(tmp_path))
        events.append("first-done")

    def second() -> None:
        entered.wait(timeout=5)
        marker.remove_marker(str(tmp_path))
        events.append("second-done")
        finished.set()

    monkeypatch.setattr(marker, "open", delaying_open, raising=False)
    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()

    assert entered.wait(timeout=5)
    time.sleep(0.1)
    assert events == ["first-has-lock"]
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert finished.is_set()
    assert events == ["first-has-lock", "first-done", "second-done"]
