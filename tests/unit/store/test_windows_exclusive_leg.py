"""Store writes require a kernel-mediated exclusive leg before mkdir reclamation.

When neither ``fcntl`` nor ``msvcrt`` is available, acquisition raises
``NoExclusiveLegError`` without creating the mkdir lock. Advisory probes degrade to not busy,
and ``rebar doctor --locks`` reports ``unknown``. POSIX tests exercise the Windows branch with
a ``flock``-backed ``msvcrt`` substitute. Windows-only tests prove native cross-process
exclusion and release after process death. PID liveness remains conservative where signal zero
cannot probe safely.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
import types

import pytest

from rebar._commands import doctor_locks as _doctor
from rebar._store import lock as _lock
from rebar._store import lock_kernel as _kernel
from rebar._store import lock_owner as _owner

pytestmark = pytest.mark.unit


# No-leg behavior must fail loudly.

#: The child hides ``fcntl`` through ``sys.modules`` and clears rebar's ``msvcrt`` handle.
#: Clearing only rebar's handle preserves Windows standard library imports.
#: ``leg_name()`` verifies the no-leg premise on every host.
_NO_LEG = """
import sys
sys.modules["fcntl"] = None   # `import fcntl` now raises ImportError, as on Windows
from rebar._store import lock_kernel as _kernel
_kernel.msvcrt = None         # and no Windows leg either: this platform offers NOTHING
assert _kernel.leg_name() == "none", "premise broken: a leg is still available"
"""


def _run_without_any_leg(body: str, tracker: str) -> subprocess.CompletedProcess[str]:
    code = _NO_LEG + textwrap.dedent(body).replace("<TRACKER>", repr(tracker))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)


def _assert_child_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"child failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.stdout.strip().splitlines()[-1] == "OK", result.stdout


def test_no_leg_acquire_refuses_loudly_and_never_takes_the_mkdir_leg(tmp_path) -> None:
    """Without an exclusive leg, ``acquire`` raises ``NoExclusiveLegError`` before creating
    the mkdir lock."""
    _assert_child_ok(
        _run_without_any_leg(
            """
            import os
            from rebar._store import lock as _lock
            from rebar._store import lock_kernel as _kernel

            tracker = <TRACKER>
            lock_dir = os.path.join(tracker, _lock.MKDIR_LOCK_NAME)
            try:
                _lock.acquire(tracker, timeout=1, attempts=1)
            except _kernel.NoExclusiveLegError as exc:
                assert "fcntl" in str(exc) and "msvcrt" in str(exc), str(exc)
            except AttributeError as exc:      # the production defect
                raise AssertionError(f"unguarded fcntl dereference: {exc}") from None
            else:
                raise AssertionError("acquire succeeded with NO exclusive leg held")
            assert not os.path.exists(lock_dir), (
                "the mkdir leg was taken without an exclusive leg — fcntl_held=True "
                "would be a lie and a live holder's lock could be reclaimed"
            )
            print("OK")
            """,
            str(tmp_path),
        )
    )


def test_no_leg_busy_probe_fails_open(tmp_path) -> None:
    """Without an exclusive leg, advisory ``write_lock_is_busy`` degrades to not busy."""
    _assert_child_ok(
        _run_without_any_leg(
            """
            from rebar._store import lock as _lock
            assert _lock.write_lock_is_busy(<TRACKER>) is False
            print("OK")
            """,
            str(tmp_path),
        )
    )


def test_no_leg_doctor_probe_reports_unknown(tmp_path) -> None:
    """Without an exclusive leg, ``rebar doctor --locks`` reports ``unknown`` instead of free."""
    _assert_child_ok(
        _run_without_any_leg(
            """
            import os
            from rebar._commands import doctor_locks as _doctor
            from rebar._store import lock as _lock

            path = os.path.join(<TRACKER>, _lock.WRITE_LOCK_NAME)
            open(path, "a").close()
            assert _doctor._probe_fcntl(path) == _doctor.STATE_UNKNOWN
            print("OK")
            """,
            str(tmp_path),
        )
    )


# POSIX simulates the Windows locking branch.


def _windows_shaped_msvcrt() -> types.ModuleType:
    """Return an ``msvcrt`` substitute backed by the host's ``flock``.

    Its ``locking(fd, mode, nbytes)`` signature, ``EACCES`` conflict, and
    per-open-file-description exclusion match the Windows branch contract.
    """
    import fcntl as host

    module = types.ModuleType("msvcrt")
    module.LK_LOCK, module.LK_NBLCK, module.LK_UNLCK = 1, 2, 0

    def locking(fd: int, mode: int, nbytes: int) -> None:
        if mode == module.LK_UNLCK:
            host.flock(fd, host.LOCK_UN)
            return
        try:
            host.flock(fd, host.LOCK_EX | host.LOCK_NB)
        except OSError:
            raise OSError(errno.EACCES, "Permission denied") from None

    module.locking = locking
    return module


@pytest.fixture
def windows_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select the Windows leg on POSIX with the ``flock``-backed substitute.

    Patching only ``lock_kernel`` preserves standard library platform detection. Native
    Windows skips this simulation because its default tests use ``msvcrt`` directly.
    """
    if os.name == "nt":
        pytest.skip(
            "POSIX-host simulation of the Windows leg: the stub delegates to the host's "
            "fcntl, which does not exist on Windows. The real primitive is exercised "
            "natively here and proved by test_native_* below."
        )
    monkeypatch.setattr(_kernel, "fcntl", None)
    monkeypatch.setattr(_kernel, "msvcrt", _windows_shaped_msvcrt())


def test_windows_leg_write_lock_round_trips(tmp_path, windows_leg: None) -> None:
    """The Windows leg releases its kernel and mkdir locks."""
    tracker = str(tmp_path)
    lock_dir = os.path.join(tracker, _lock.MKDIR_LOCK_NAME)

    handle = _lock.acquire(tracker, timeout=5, attempts=1)
    assert os.path.isdir(lock_dir), "the mkdir leg was not taken"
    handle.release()
    assert not os.path.isdir(lock_dir), "the mkdir leg was not released"


def test_windows_leg_excludes_a_second_acquirer(tmp_path, windows_leg: None) -> None:
    """The Windows kernel leg excludes a second acquirer before mkdir reclamation."""
    tracker = str(tmp_path)
    handle = _lock.acquire(tracker, timeout=1, attempts=1)
    try:
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(tracker, timeout=1, attempts=1)
    finally:
        handle.release()

    # Release permits a later acquisition.
    _lock.acquire(tracker, timeout=1, attempts=1).release()


def test_windows_leg_exclusion_survives_the_mkdir_leg_being_removed(
    tmp_path, windows_leg: None
) -> None:
    """With ``dual_window=False``, the Windows kernel leg still excludes a second acquirer."""
    tracker = str(tmp_path)
    handle = _lock.acquire(tracker, timeout=1, attempts=1, dual_window=False)
    try:
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(tracker, timeout=1, attempts=1, dual_window=False)
    finally:
        handle.release()


def test_windows_leg_busy_probe_answers_truthfully(tmp_path, windows_leg: None) -> None:
    """``write_lock_is_busy`` reports free, held, and released Windows-leg states."""
    tracker = str(tmp_path)
    assert _lock.write_lock_is_busy(tracker) is False, "free store reported busy"
    handle = _lock.acquire(tracker, timeout=1, attempts=1)
    try:
        assert _lock.write_lock_is_busy(tracker) is True, "held store reported free"
    finally:
        handle.release()
    assert _lock.write_lock_is_busy(tracker) is False, "released store reported busy"


def test_windows_leg_doctor_probe_answers_truthfully(tmp_path, windows_leg: None) -> None:
    """``rebar doctor --locks`` reports held and free states through the Windows leg."""
    tracker = str(tmp_path)
    lock_path = os.path.join(tracker, _lock.WRITE_LOCK_NAME)

    handle = _lock.acquire(tracker, timeout=1, attempts=1)
    try:
        assert _doctor._probe_fcntl(lock_path) == _doctor.STATE_HELD
    finally:
        handle.release()
    assert _doctor._probe_fcntl(lock_path) == _doctor.STATE_FREE


# PID liveness remains conservative without signal zero.


def test_pid_probe_never_signals_where_signal_zero_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where signal zero is unsafe, PID liveness sends no signal and reports alive."""
    monkeypatch.setattr(_owner, "_signal_zero_probes_liveness", lambda: False)

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("os.kill was called on a platform where it would terminate")

    monkeypatch.setattr(os, "kill", _forbidden)

    assert _owner._pid_alive(os.getpid()) is True, (
        "with no liveness signal available the answer must be the conservative one"
    )


def test_narrowed_pid_probe_does_not_reclaim_a_fresh_lock_without_proof(tmp_path) -> None:
    """Uncertain PID liveness cannot reclaim a fresh same-host lock without exclusive proof."""
    lock_dir = tmp_path / _lock.MKDIR_LOCK_NAME
    lock_dir.mkdir()
    (lock_dir / _owner._MKDIR_OWNER_FILE).write_text(_owner._owner_stamp(), encoding="utf-8")

    assert _owner._mkdir_lock_is_stale(str(lock_dir), fcntl_held=False) is False


# Native Windows tests prove cross-process exclusion and release after process death.

_needs_windows = pytest.mark.skipif(
    os.name != "nt", reason="proves the real msvcrt.locking primitive; Windows only"
)

_CHILD_HOLDS_THE_LOCK = """
import sys, time
from rebar._store import lock as _lock
handle = _lock.acquire(sys.argv[1], timeout=10, attempts=1)
print("HELD", flush=True)
time.sleep(600)
"""


def _spawn_holder(tracker: str) -> subprocess.Popen[str]:
    """Start a child that takes the write lock and holds it. Caller MUST reap it."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLDS_THE_LOCK, tracker],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "HELD", "child never took the lock"
    return proc


@_needs_windows
def test_native_lock_is_visible_across_processes(tmp_path) -> None:
    """Native ``msvcrt.locking`` excludes another process while its holder remains alive."""
    tracker = str(tmp_path)
    proc = _spawn_holder(tracker)
    try:
        assert _lock.write_lock_is_busy(tracker) is True, (
            "another process holds the lock but this process does not see it"
        )
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(tracker, timeout=2, attempts=1)
    finally:
        proc.kill()
        proc.wait(timeout=30)


@_needs_windows
def test_native_lock_dies_with_its_holder(tmp_path) -> None:
    """Native ``msvcrt.locking`` releases on process death so acquisition can reclaim the
    orphaned mkdir lock."""
    tracker = str(tmp_path)
    lock_dir = os.path.join(tracker, _lock.MKDIR_LOCK_NAME)
    proc = _spawn_holder(tracker)
    try:
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover - the kill above normally settles it
            proc.kill()
            proc.wait(timeout=30)

    assert os.path.isdir(lock_dir), "the killed holder should have left its mkdir lock"
    handle = _lock.acquire(tracker, timeout=15, attempts=1)
    handle.release()
