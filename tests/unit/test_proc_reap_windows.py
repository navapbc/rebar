"""The Windows reaper ends a timed-out child's descendants too (bug aerial-flameproof-antbear).

Windows has no process groups, so ``proc.kill()`` ends only the direct child and a grandchild
holding the inherited stdout pipe survives the timeout. ``rebar._proc`` walks the parent-pid
chain from the child and terminates every descendant. The walk and its best-effort fallback
are checked here on every platform with a fake process table; the real Toolhelp/Terminate
calls run in ``tests/unit/grounding/test_harness.py::test_timeout_reaps_pipe_holding_grandchild``
on the Windows sweep.
"""

from __future__ import annotations

import logging

import pytest

from rebar import _proc

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test_proc_reap_windows")


def test_descendants_are_the_parent_pid_closure_deepest_first() -> None:
    table = [(10, 1), (11, 10), (12, 11), (13, 10), (20, 1), (21, 20)]
    order = _proc._descendants(10, table)
    assert set(order) == {11, 12, 13}
    assert order.index(12) < order.index(11), "a grandchild is ended before its parent"


def test_an_exited_parents_orphan_is_still_a_descendant() -> None:
    """Windows never re-parents: the orphan still names the exited pid as its parent."""
    table = [(10, 1), (12, 11)]  # 11 exited and is gone from the table; 12 still points at it
    assert _proc._descendants(11, table) == [12]


def test_a_self_parented_entry_never_loops() -> None:
    assert _proc._descendants(0, [(0, 0), (4, 0)]) == [4]


class _FakeProc:
    pid = 4321

    def __init__(self) -> None:
        self.calls: list[str] = []

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        return 0


def _force_windows(monkeypatch: pytest.MonkeyPatch, table, ended: list[int]) -> None:
    monkeypatch.setattr(_proc.os, "name", "nt")
    monkeypatch.setattr(_proc, "_windows_process_table", lambda: list(table))

    def terminate(pid: int) -> None:
        ended.append(pid)
        table[:] = [row for row in table if row[0] != pid]

    monkeypatch.setattr(_proc, "_windows_terminate", terminate)


def test_the_windows_reap_kills_the_child_then_its_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = [(4321, 1), (5000, 4321), (5001, 5000), (6000, 1)]
    ended: list[int] = []
    _force_windows(monkeypatch, table, ended)
    proc = _FakeProc()

    _proc.reap_process_group(proc, grace=0.01, drain=0.01, label="t", logger=_LOG)

    assert proc.calls == ["kill", "wait"]
    assert ended == [5001, 5000], "every descendant, deepest first, and nothing unrelated"


def test_a_failed_walk_falls_back_to_the_plain_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_proc.os, "name", "nt")

    def no_snapshot() -> list[tuple[int, int]]:
        raise OSError(5, "access denied")

    monkeypatch.setattr(_proc, "_windows_process_table", no_snapshot)
    proc = _FakeProc()

    _proc.reap_process_group(proc, grace=0.01, drain=0.01, label="t", logger=_LOG)

    assert proc.calls == ["kill", "wait"]


def test_a_descendant_that_cannot_be_ended_does_not_stop_the_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = [(5000, 4321), (5001, 4321)]
    monkeypatch.setattr(_proc.os, "name", "nt")
    monkeypatch.setattr(_proc, "_windows_process_table", lambda: list(table))
    tried: list[int] = []

    def terminate(pid: int) -> None:
        tried.append(pid)
        raise OSError(5, "access denied")

    monkeypatch.setattr(_proc, "_windows_terminate", terminate)
    proc = _FakeProc()

    _proc.reap_process_group(proc, grace=0.01, drain=0.01, label="t", logger=_LOG)

    assert proc.calls == ["kill", "wait"]
    assert sorted(set(tried)) == [5000, 5001]
