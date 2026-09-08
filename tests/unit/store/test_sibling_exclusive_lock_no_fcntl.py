from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit


_FAKE_WINDOWS_LOCKING = """
import errno
import sys
import threading
import types

sys.modules["fcntl"] = None
from rebar._store import lock_kernel as _kernel

_fake_lock = threading.Lock()
msvcrt = types.ModuleType("msvcrt")
msvcrt.LK_LOCK, msvcrt.LK_NBLCK, msvcrt.LK_UNLCK = 1, 2, 0


def locking(fd, mode, nbytes):
    if mode == msvcrt.LK_UNLCK:
        _fake_lock.release()
        return
    if mode == msvcrt.LK_NBLCK:
        if not _fake_lock.acquire(blocking=False):
            raise OSError(errno.EACCES, "Permission denied")
        return
    raise AssertionError(f"unexpected locking mode: {mode}")


msvcrt.locking = locking
_kernel.fcntl = None
_kernel.msvcrt = msvcrt
"""


def _run_without_fcntl(body: str, tmp_path: object) -> subprocess.CompletedProcess[str]:
    code = _FAKE_WINDOWS_LOCKING + textwrap.dedent(body).replace("<TMP>", repr(str(tmp_path)))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)


def _assert_child_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"child failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.stdout.strip().splitlines()[-1] == "OK", result.stdout


def test_sibling_exclusive_lock_uses_windows_leg_when_fcntl_is_absent(tmp_path) -> None:
    """A missing POSIX module must not make the runtime lock path fail."""
    _assert_child_ok(
        _run_without_fcntl(
            """
            from pathlib import Path
            from rebar._store.fsutil import sibling_exclusive_lock

            target = Path(<TMP>) / "sidecar.json"
            with sibling_exclusive_lock(target):
                target.write_text("locked", encoding="utf-8")
            assert target.read_text(encoding="utf-8") == "locked"
            print("OK")
            """,
            tmp_path,
        )
    )


def test_sibling_exclusive_lock_waits_for_a_contended_windows_leg(tmp_path) -> None:
    """The Windows fallback preserves blocking ``flock(LOCK_EX)`` semantics."""
    _assert_child_ok(
        _run_without_fcntl(
            """
            import threading
            import time
            from pathlib import Path
            from rebar._store.fsutil import sibling_exclusive_lock

            target = Path(<TMP>) / "sidecar.json"
            events = []
            release_first = threading.Event()

            def second():
                events.append("second-waiting")
                with sibling_exclusive_lock(target):
                    events.append("second-entered")

            with sibling_exclusive_lock(target):
                worker = threading.Thread(target=second)
                worker.start()
                while events != ["second-waiting"]:
                    time.sleep(0.01)
                time.sleep(0.1)
                assert events == ["second-waiting"]
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert events == ["second-waiting", "second-entered"]
            print("OK")
            """,
            tmp_path,
        )
    )


def test_hlc_next_tick_uses_sibling_lock_without_fcntl(tmp_path) -> None:
    """The HLC stamp path must keep working when only the Windows lock leg exists."""
    _assert_child_ok(
        _run_without_fcntl(
            """
            from rebar._store.hlc import next_tick

            tick = next_tick(<TMP>, "ticket-1")
            assert isinstance(tick, int) and tick > 0
            print("OK")
            """,
            tmp_path,
        )
    )
