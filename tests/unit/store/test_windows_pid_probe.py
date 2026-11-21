"""Windows pid liveness without signalling (bug smoking-bereft-boar).

``os.kill(pid, 0)`` on Windows is ``TerminateProcess``, so :func:`lock_owner._pid_alive` asks
``OpenProcess``/``WaitForSingleObject`` instead. The decision table runs against a fake
``kernel32`` on every host; the native probe and the consumers that changed with it are
checked where they apply.
"""

from __future__ import annotations

import os
import sys
import types

import pytest
from _python_child import dead_pid

from rebar._commands import compact_trigger
from rebar._store import lock_owner as _owner

_WAIT_TIMEOUT = 0x102
_WAIT_FAILED = 0xFFFFFFFF
_ACCESS = _owner._SYNCHRONIZE | _owner._PROCESS_QUERY_LIMITED_INFORMATION


class _FakeKernel32:
    """Records every call; answers OpenProcess/WaitForSingleObject as configured."""

    def __init__(self, handle: int, error: int = 0, wait: int = _WAIT_TIMEOUT) -> None:
        self.handle, self.error, self.wait = handle, error, wait
        self.opened: list[tuple[int, bool, int]] = []
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        self.opened.append((access, inherit, pid))
        return self.handle

    def WaitForSingleObject(self, handle: int, ms: int) -> int:
        assert (handle, ms) == (self.handle, 0), "the wait must be a zero-timeout poll"
        return self.wait

    def CloseHandle(self, handle: int) -> int:
        self.closed.append(handle)
        return 1


def _ask(pid: int, k32: _FakeKernel32) -> bool:
    return _owner._windows_pid_alive(pid, kernel32=k32, get_last_error=lambda: k32.error)


def test_no_such_process_is_dead() -> None:
    k32 = _FakeKernel32(handle=0, error=_owner._ERROR_INVALID_PARAMETER)
    assert _ask(4321, k32) is False
    assert k32.opened == [(_ACCESS, False, 4321)]


def test_an_unopenable_process_is_alive() -> None:
    """Access denied (5) proves the process EXISTS; never reclaim on it."""
    assert _ask(4321, _FakeKernel32(handle=0, error=5)) is True


@pytest.mark.parametrize(
    ("wait", "alive"),
    [(_owner._WAIT_OBJECT_0, False), (_WAIT_TIMEOUT, True), (_WAIT_FAILED, True)],
)
def test_the_zero_wait_tells_exited_from_running_and_closes_the_handle(
    wait: int, alive: bool
) -> None:
    k32 = _FakeKernel32(handle=77, wait=wait)
    assert _ask(4321, k32) is alive
    assert k32.closed == [77], "the process handle must always be closed"


@pytest.mark.parametrize("pid", [0, -1, -4321])
def test_a_non_positive_pid_is_alive_without_a_call(pid: int) -> None:
    """OpenProcess also fails with 87 for pid 0, so a corrupt stamp must not read dead."""
    k32 = _FakeKernel32(handle=0, error=_owner._ERROR_INVALID_PARAMETER)
    assert _ask(pid, k32) is True
    assert k32.opened == []


def test_a_failing_kernel32_is_alive() -> None:
    class _Broken(_FakeKernel32):
        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            raise OSError("no kernel32 here")

    assert _ask(4321, _Broken(handle=0)) is True


def test_pid_alive_routes_to_the_windows_probe_on_nt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_owner, "_signal_zero_probes_liveness", lambda: False)
    monkeypatch.setattr(_owner.os, "name", "nt")
    seen: list[int] = []
    monkeypatch.setattr(_owner, "_windows_pid_alive", lambda pid: seen.append(pid) or False)

    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("os.kill must never be called where it would terminate")

    monkeypatch.setattr(_owner.os, "kill", _forbidden)
    assert _owner._pid_alive(4321) is False
    assert seen == [4321]


@pytest.mark.skipif(sys.platform != "win32", reason="native kernel32 exists only on Windows")
def test_the_native_probe_sees_a_reaped_child_dead_and_itself_alive() -> None:
    assert _owner._pid_alive(os.getpid()) is True
    assert _owner._pid_alive(dead_pid()) is False


def test_sweep_exited_needs_no_wnohang(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Windows has no os.WNOHANG; the exit check must fall through to the liveness probe.

    Only the module under test loses the attribute: deleting it from the real ``os`` would
    break subprocess's own waits in this process."""
    no_wnohang = types.SimpleNamespace(
        **{name: getattr(os, name) for name in dir(os) if name != "WNOHANG"}
    )
    monkeypatch.setattr(compact_trigger, "os", no_wnohang)

    def _no_waitpid(*_a: object) -> None:
        raise AssertionError("waitpid must not run without WNOHANG")

    no_wnohang.waitpid = _no_waitpid
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: False)
    assert compact_trigger._sweep_exited(str(tmp_path), 4321) is True


def test_a_dead_sweep_holder_is_never_signalled(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A not-running holder is left alone: on Windows os.kill would be TerminateProcess."""
    monkeypatch.setattr(compact_trigger, "_read_trigger_stamp", lambda _t: _owner._owner_stamp())
    monkeypatch.setattr(_owner, "_describe_stamped_pid", lambda _fields: "not-running")

    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("a dead holder must not be signalled")

    monkeypatch.setattr(os, "kill", _forbidden)
    assert compact_trigger.stop_local_sweep(str(tmp_path)) is None
