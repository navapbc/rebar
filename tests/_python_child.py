# mechanism-ok: test_helper tests/_python_child.py — c7b2-e4ed-37fd-4f60 shared dead-pid helper
"""Portable child-process helpers for lock-holder tests (bug smoking-bereft-boar).

Two Windows facts break the obvious ``subprocess.Popen`` idioms these tests used:

* A ``uv venv`` interpreter is a launcher. ``Scripts/python.exe`` starts the real interpreter
  as its child, so ``Popen([sys.executable, ...]).pid`` is the LAUNCHER's pid, while the
  lock the child takes is stamped with the interpreter's ``os.getpid()``. Killing the
  launcher does not wait for the interpreter to exit.
* Windows reuses a freed pid quickly. A "dead" pid stays reserved only while some handle to
  the exited process is open.

:func:`spawn_python` hands back the interpreter's own pid; :func:`stop` waits until that pid
reads dead; :func:`dead_pid` keeps the reaped child's ``Popen`` (and so its handle) alive for
the rest of the session so the pid cannot be recycled under the test.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from rebar._store import lock_owner

#: Reaped children, kept referenced so Windows cannot hand their pids to a new process.
_REAPED: list[subprocess.Popen] = []

_PID_PRELUDE = "import os, sys; print(os.getpid(), flush=True)\n"


def dead_pid() -> int:
    """A pid that is provably not running: a child we started, waited for, and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    _REAPED.append(proc)
    return proc.pid


def spawn_python(code: str, *args: str) -> tuple[subprocess.Popen[str], int]:
    """Start ``python -c code args...`` and return ``(proc, pid)``.

    *pid* is the interpreter's own ``os.getpid()``, read from the child's first stdout line,
    so it matches what the child stamps into a lock even behind a venv launcher. The rest of
    the child's stdout stays readable on ``proc.stdout``."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _PID_PRELUDE + code, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line.isdigit(), f"child did not report its pid: {line!r}"
    return proc, int(line)


def stop(proc: subprocess.Popen, pid: int, *, timeout: float = 30.0) -> None:
    """End the child started by :func:`spawn_python` and wait until *pid* reads dead.

    Windows termination is asynchronous, so a caller asserting ``not-running`` right after a
    kill would race the exit without this wait. Fails the test if the pid is still alive at
    the deadline."""
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        pass
    proc.kill()
    proc.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while lock_owner._pid_alive(pid):
        if time.monotonic() > deadline:
            raise AssertionError(f"child interpreter pid {pid} still alive after {timeout}s")
        time.sleep(0.05)
