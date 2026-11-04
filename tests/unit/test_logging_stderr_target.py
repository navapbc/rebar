"""The entrypoint stderr handler writes to the process's *current* ``sys.stderr``.

Regression for bug bald-diagnostic-cricket: ``install_stderr_handler`` used to build
``StreamHandler(sys.stderr)``, freezing whatever object ``sys.stderr`` was at install time.
Because the install is idempotent, a later in-process ``rebar._cli.main`` call never rebound it,
so once that first stream was closed (pytest ``capsys``, ``contextlib.redirect_stderr``, an
embedding application) every rebar diagnostic became a ``--- Logging error ---`` traceback.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest

from rebar._logging import _HANDLER_MARKER, install_stderr_handler


@pytest.fixture
def rebar_logger(monkeypatch: pytest.MonkeyPatch) -> logging.Logger:
    """The ``rebar`` logger with any entrypoint handler removed; restored on teardown."""
    logger = logging.getLogger("rebar")
    kept = [h for h in logger.handlers if not getattr(h, _HANDLER_MARKER, False)]
    monkeypatch.setattr(logger, "handlers", kept)
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.delenv("REBAR_LOG_LEVEL", raising=False)
    return logger


def _marked(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if getattr(h, _HANDLER_MARKER, False)]


def test_diagnostics_follow_a_replaced_and_closed_stderr(
    rebar_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install under one stderr, close it, swap in another: the record lands in the new one."""
    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    install_stderr_handler("rebar")
    first.close()

    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)
    logging.getLogger("rebar.probe").warning("after the swap")

    written = second.getvalue()
    assert "--- Logging error ---" not in written, written
    assert written == "WARNING rebar.probe: after the swap\n"


def test_reinstall_after_a_swap_keeps_one_handler_on_the_current_stderr(
    rebar_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated entrypoint install stays idempotent and still targets the live stderr."""
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    install_stderr_handler("rebar")
    current = io.StringIO()
    monkeypatch.setattr(sys, "stderr", current)
    install_stderr_handler("rebar")

    assert len(_marked(rebar_logger)) == 1
    logging.getLogger("rebar.probe").error("second install")
    assert current.getvalue() == "ERROR rebar.probe: second install\n"
