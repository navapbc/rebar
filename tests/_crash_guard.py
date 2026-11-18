"""Escalate xdist worker crashes into an explicit pytest failure.

It also makes a pytest-timeout expiry in an xdist worker name itself (bug d1de-f99f). With the
thread method, pytest-timeout writes its dump to fd 1, which execnet points at /dev/null in every
worker, then ``os._exit(1)``s. The announcement and thread stacks go to the worker's
``sys.stderr`` instead: fd 2 on POSIX, a dup on Windows, where execnet points fd 2 at NUL
(bug c691-bbc1-4217-4cf4).
"""

from __future__ import annotations

import faulthandler
import sys
import threading
from typing import Any

import pytest

# mechanism-ok: test_helper tests/_crash_guard.py — bab8-eb45-4dc2-4f02
_XDIST_FAILURES: list[tuple[str, object]] = []


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: object, error: object | None) -> None:
    if error is None:
        return
    worker_id = getattr(getattr(node, "workerinput", None), "get", lambda *_: None)("workerid")
    if worker_id is None:
        worker_id = getattr(node, "gateway", None)
    _XDIST_FAILURES.append((str(worker_id), error))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _XDIST_FAILURES:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    for worker_id, error in _XDIST_FAILURES:
        msg = f"rebar crash guard: xdist worker={worker_id} terminated abnormally: {error}"
        if reporter is not None:
            reporter.write_line("")
            reporter.write_line(msg, red=True, bold=True)
        else:  # pragma: no cover - terminalreporter is present in normal pytest runs
            print(msg)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_timeout_set_timer(item: pytest.Item, settings: Any) -> bool | None:
    """Arm a worker's thread-method timer whose expiry is announced on fd 2.

    Returns ``None`` (pytest-timeout's own implementation runs) outside an xdist worker or
    for any other method; the single-process terminal already shows pytest-timeout's dump.
    """
    workerinput = getattr(item.config, "workerinput", None)
    if workerinput is None or settings.method != "thread":
        return None
    timer = threading.Timer(settings.timeout, _announce_then_expire, (item, settings))
    timer.name = f"rebar crash guard timeout {item.nodeid}"

    def cancel() -> None:
        timer.cancel()
        timer.join()

    item.cancel_timeout = cancel  # type: ignore[attr-defined]  # pytest-timeout's protocol
    timer.start()
    return True


def _announce_then_expire(item: pytest.Item, settings: Any) -> None:
    import pytest_timeout

    if not settings.disable_debugger_detection and pytest_timeout.is_debugging():
        return
    capman = item.config.pluginmanager.getplugin("capturemanager")
    if capman is not None:
        capman.suspend_global_capture(item)  # restores the worker's own sys.stderr
    worker_id = item.config.workerinput.get("workerid")  # type: ignore[attr-defined]
    message = (
        f"\n+++ Timeout +++ pytest-timeout expired after {settings.timeout}s in "
        f"{item.nodeid} (xdist worker {worker_id}); the worker exits now\n"
    )
    stream = sys.stderr  # execnet's controller-connected stream on every platform
    stream.write(message)
    stream.flush()
    faulthandler.dump_traceback(file=stream, all_threads=True)
    pytest_timeout.timeout_timer(item, settings)
