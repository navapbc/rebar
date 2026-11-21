"""Detached compaction sweep and snapshot GC on Windows (bug uncommon-cleansable-wren).

Both triggers used to return early on win32. They now detach through
:func:`rebar._proc.spawn_detached` on every platform; the retire path signals nothing off
POSIX, where ``os.kill`` would be ``TerminateProcess`` on a pid the stamp cannot prove.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._commands import compact_trigger
from rebar._store import lock_owner as _owner


def test_stop_local_sweep_signals_nothing_off_posix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even a stamp the platform would call live is not signalled where SIGTERM terminates."""
    monkeypatch.setattr(compact_trigger.os, "name", "nt")
    # A stamp naming another pid: our own would be skipped by the pid == getpid() check, so
    # the test could not tell the off-POSIX guard from that early return.
    other = _owner._owner_stamp().replace(f" pid={os.getpid()} ", f" pid={os.getpid() + 1} ")
    assert other != _owner._owner_stamp()
    monkeypatch.setattr(compact_trigger, "_read_trigger_stamp", lambda _t: other)
    monkeypatch.setattr(_owner, "_describe_stamped_pid", lambda _fields: "live")

    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("no signal may be sent off POSIX")

    monkeypatch.setattr(compact_trigger.os, "kill", _forbidden)
    assert compact_trigger.stop_local_sweep(str(tmp_path)) is None


_WORKER = """
import time


def run(anchor, marker):
    time.sleep(2)  # outlast the spawner, which exits right after spawning
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("alive")
"""

_SPAWNER = """
import os, subprocess, sys
from rebar._proc import spawn_detached
anchor, marker = sys.argv[1], sys.argv[2]
spawn_detached("b4_detach_worker", "run", anchor, marker, env=dict(os.environ),
               stderr=subprocess.DEVNULL)
print("spawned", flush=True)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="the nt detach flags exist only on Windows")
def test_a_detached_child_outlives_the_process_that_spawned_it(tmp_path: Path) -> None:
    """The production shape: a short-lived process (a CLI close) detaches a worker through
    :func:`rebar._proc.spawn_detached` and EXITS. The worker sleeps past its spawner's exit and
    then writes a marker, proving the worker started by spawn_detached survives its parent's
    exit on Windows (it does not isolate the flags from Windows' default behaviour)."""
    (tmp_path / "b4_detach_worker.py").write_text(_WORKER, encoding="utf-8")
    marker = tmp_path / "marker.txt"
    env = subprocess_env(PYTHONPATH=str(tmp_path))
    spawner = subprocess.run(
        [sys.executable, "-c", _SPAWNER, str(tmp_path), str(marker)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert spawner.returncode == 0, spawner.stderr
    assert "spawned" in spawner.stdout
    assert not marker.exists(), "the worker must still be sleeping when its spawner exits"
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert marker.exists(), "the detached child did not outlive its exited spawner"
    assert marker.read_text(encoding="utf-8") == "alive"
