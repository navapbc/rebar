"""Escalate xdist worker crashes into an explicit pytest failure."""

from __future__ import annotations

import pytest

# mechanism-ok: test_helper tests/_crash_guard.py — bab8-eb45-4dc2-4f02
_XDIST_FAILURES: list[tuple[str, object]] = []


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
