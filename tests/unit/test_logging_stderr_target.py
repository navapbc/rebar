"""The entrypoint stderr handler writes to the process's stderr, never a swapped stream.

Regression for bug bald-diagnostic-cricket: ``install_stderr_handler`` used to build
``StreamHandler(sys.stderr)``, freezing whatever object ``sys.stderr`` was at install time.
Because the install is idempotent, a later in-process ``rebar._cli.main`` call never rebound it,
so once that first stream was closed (pytest ``capsys``, an embedding application) every rebar
diagnostic became a ``--- Logging error ---`` traceback. The handler must not follow an
in-process ``redirect_stderr`` either: rebar uses one to discard command output, and a
detached sweep's diagnostics must still reach its worker log.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys

import pytest

from rebar._logging import _HANDLER_MARKER, install_stderr_handler


@pytest.fixture
def process_stderr(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Stand-in for the process's stderr, with the ``rebar`` logger's entrypoint handler
    removed for the test; all of it is restored on teardown."""
    logger = logging.getLogger("rebar")
    kept = [h for h in logger.handlers if not getattr(h, _HANDLER_MARKER, False)]
    monkeypatch.setattr(logger, "handlers", kept)
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.delenv("REBAR_LOG_LEVEL", raising=False)
    stream = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", stream)
    return stream


def test_a_closed_install_time_stderr_does_not_break_later_diagnostics(
    process_stderr: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install under one ``sys.stderr``, close it, swap in another: the record reaches the
    process stderr, and no logging-error traceback lands anywhere."""
    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    install_stderr_handler("rebar")
    first.close()
    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)

    logging.getLogger("rebar.probe").warning("after the swap")

    assert "--- Logging error ---" not in second.getvalue(), second.getvalue()
    assert process_stderr.getvalue() == "WARNING rebar.probe: after the swap\n"


def test_diagnostics_bypass_an_in_process_stderr_redirect(process_stderr: io.StringIO) -> None:
    """A ``redirect_stderr`` that discards command output must not swallow diagnostics."""
    install_stderr_handler("rebar")
    discarded = io.StringIO()
    with contextlib.redirect_stderr(discarded):
        logging.getLogger("rebar.probe").warning("inside the redirect")

    assert discarded.getvalue() == ""
    assert process_stderr.getvalue() == "WARNING rebar.probe: inside the redirect\n"


def test_reinstall_keeps_exactly_one_handler(
    process_stderr: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated entrypoint install after a swap stays idempotent."""
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    install_stderr_handler("rebar")
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    install_stderr_handler("rebar")

    marked = [h for h in logging.getLogger("rebar").handlers if getattr(h, _HANDLER_MARKER, False)]
    assert len(marked) == 1
    logging.getLogger("rebar.probe").error("second install")
    assert process_stderr.getvalue() == "ERROR rebar.probe: second install\n"
