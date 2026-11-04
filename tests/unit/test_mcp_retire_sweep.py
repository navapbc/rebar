"""A retiring MCP server stops its own detached compaction sweep before it exits.

``docker stop`` signals only the container's PID 1 (the MCP server), and the sweep worker runs
in its own session, so the server must forward the stop itself (bug topnotch-brazen-slug). It
finds the worker through the stamp on the sweep's worker lock and signals it only when that
stamp names a live process in this server's own PID namespace: the tickets clone is shared by
every container on the host, so a stamp can name another container's worker.

Every child here is bounded by ``wait(timeout=...)`` and killed in ``finally``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rebar._commands import compact_trigger
from rebar._store import lock_owner

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery"),
]

# A stand-in sweep worker: holds the real worker lock, honours the real stop flag the way the
# sweep loop does, and releases the lock on its way out exactly as run_sweep's finally does.
_WORKER_SRC = """
import sys, time
from rebar._commands import compact as c
from rebar._commands import compact_trigger as t

t.install_stop_on_sigterm()
tracker = sys.argv[1]
fd = t._acquire_trigger_lock(tracker)
assert fd is not None
print("holding", flush=True)
deadline = time.monotonic() + 60
while not c.stop_requested() and time.monotonic() < deadline:
    time.sleep(0.05)
t.release_trigger_lock(tracker, fd)
print("stopped", flush=True)
"""

_IDLE_SRC = "import time; print('up', flush=True); time.sleep(60)"


def _spawn(src: str, *args: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", src, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    proc.stdout.readline()  # its ready line
    return proc


def _reap(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def _git_store(tmp_path: Path) -> str:
    """A git-backed store: the sweep's worker lock lives in the tracker's git dir."""
    tracker = str(tmp_path / "store")
    os.makedirs(tracker)
    subprocess.run(["git", "init", "-q"], cwd=tracker, check=True, capture_output=True, timeout=60)
    return tracker


def test_retire_stops_a_live_local_sweep_and_waits_for_it(tmp_path: Path) -> None:
    tracker = _git_store(tmp_path)
    worker = _spawn(_WORKER_SRC, tracker)
    try:
        finish = compact_trigger.retire_local_sweep(tracker)
        finish(time.monotonic() + 20)
        rc = worker.wait(timeout=20)
        out = worker.stdout.read() if worker.stdout else ""
    finally:
        _reap(worker)

    assert rc == 0
    assert "stopped" in out
    assert not os.path.exists(compact_trigger._trigger_lock_path(tracker)), (
        "retire returned before the worker released its lock"
    )


def test_a_stamp_from_another_pid_namespace_is_never_signalled(tmp_path: Path) -> None:
    tracker = _git_store(tmp_path)
    bystander = _spawn(_IDLE_SRC)
    try:
        lock_path = compact_trigger._trigger_lock_path(tracker)
        with open(lock_path, "w", encoding="utf-8") as fh:
            fh.write(
                f"{lock_owner._STAMP_V2_PREFIX} host={lock_owner._host_identity()} "
                f"ns=4026999999 pid={bystander.pid} start=- op=compact-sweep"
            )

        assert compact_trigger.stop_local_sweep(tracker) is None
        time.sleep(0.5)
        assert bystander.poll() is None, "a process in another PID namespace was signalled"
    finally:
        _reap(bystander)


def test_the_sigterm_handler_retires_the_sweep_before_setting_should_exit() -> None:
    from rebar._mcp_health import InFlightGauge
    from rebar._mcp_serving import make_sigterm_handler

    class _Server:
        should_exit = False

    server = _Server()
    events: list[str] = []

    def retire():
        events.append("signal-sweep")

        def finish(deadline: float) -> None:
            assert deadline > time.monotonic()
            events.append(f"finish should_exit={server.should_exit}")

        return finish

    handler = make_sigterm_handler(
        server, InFlightGauge(), grace_seconds=5, poll_interval=0.01, retire=retire
    )
    handler(15, None)

    assert events == ["signal-sweep", "finish should_exit=False"]
    assert server.should_exit is True
