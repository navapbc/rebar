"""Shared primitives for the caplog coverage-integrity guard (see tests/conftest.py).

A ``caplog`` assertion whose records never arrive DOES NOT FAIL — ``assert not
[unexpected]`` is trivially true against an empty list. So a test can lose 100% of its
coverage and stay green, with nothing in the run output distinguishing it from a test
that genuinely verified something (bug 9ac2-e2f1-bb6e-4436).

``caplog`` captures through a handler on the ROOT logger, so records only reach it if the
shared ``rebar`` parent logger both *emits* them and *propagates* them. There are
therefore two process-global mutations that silently sever it, and BOTH are real:

* ``logging.getLogger("rebar").propagate = False`` — nothing under ``rebar`` reaches the
  root handler. ``rebar.review_bot.config.configure_logging()`` did exactly this (bug
  b718), at module-import time, and never restored it. On pytest >= 8.4 this is only
  PARTLY mitigated: ``_pytest.logging.catching_logs.__enter__`` attaches the capture
  handler to every logger that is ALREADY non-propagating, but its own comment records the
  hole — "will miss loggers that *become* non-propagating after the ``__enter__``". Code
  that flips it mid-test therefore voids the rest of that test's assertions silently, and
  the mitigation is an implementation detail we should not be relying on.
* ``logging.getLogger("rebar").setLevel(...)`` above the record's level — the record is
  dropped at the originating logger before any handler is consulted.
  ``rebar._logging.install_stderr_handler()`` pins it to WARNING, and every in-process
  ``rebar._cli.main(...)`` call in the suite goes through it. ``caplog.at_level(INFO)``
  without a ``logger=`` argument does NOT rescue this: it raises the ROOT level, not the
  ``rebar`` logger's.

Neither is restored, both are invisible, and — because the victim is never the culprit —
the red (when there is one at all) lands on an unrelated test far from the code that did
it, in an order-dependent way that ``-n <N> --dist worksteal`` makes non-reproducible.

These primitives let the conftest guard fail CLOSED at the SOURCE for the propagation
vector, and CONTAIN the level vector per-test so no assertion inherits a poisoned logger.
Keeping them here lets the guard's self-test
(``tests/unit/test_caplog_coverage_integrity.py``) exercise the *same* logic instead of a
copy that could drift.
"""

from __future__ import annotations

import logging

#: The shared parent logger every ``rebar.*`` logger inherits from, and therefore the
#: single point that decides whether ``caplog`` can see rebar's records.
REBAR_LOGGER_NAME = "rebar"

_REMEDY = (
    "Do not mutate propagation on the shared 'rebar' logger: it is process-global and not "
    "restored, so it voids every later caplog assertion on a rebar.* logger. Attach a "
    "handler to the specific logger instead (see "
    "rebar.review_bot.config.configure_logging), or scope the change and restore it in a "
    "finally/fixture teardown."
)


def propagation_failure(nodeid: str, *, phase: str) -> str | None:
    """Return a failure message if ``caplog`` can no longer see ``rebar.*`` records.

    ``None`` means healthy. *phase* is ``"setup"`` (propagation was already off before this
    test ran — an import/collection-time or non-test code path did it) or ``"teardown"``
    (this test's own body did it, which is the blame we want).
    """
    if logging.getLogger(REBAR_LOGGER_NAME).propagate:
        return None
    if phase == "teardown":
        cause = f"{nodeid} disabled it during its own body"
    else:
        cause = (
            f"it was already off when {nodeid} started — something outside a test body "
            "(module import at collection time, or a non-test code path) disabled it"
        )
    return (
        "caplog coverage integrity: "
        f'logging.getLogger("{REBAR_LOGGER_NAME}").propagate is False — {cause}. '
        "caplog captures through a handler on the ROOT logger, so while this is off NO "
        "rebar.* record reaches caplog and every later log assertion passes VACUOUSLY "
        "while verifying nothing. " + _REMEDY
    )


def restore_propagation() -> None:
    """Re-enable propagation so one offender does not cascade into unrelated victims."""
    logging.getLogger(REBAR_LOGGER_NAME).propagate = True


def current_level() -> int:
    """The shared ``rebar`` logger's own level (``logging.NOTSET`` when it inherits)."""
    return logging.getLogger(REBAR_LOGGER_NAME).level


def restore_level(level: int) -> bool:
    """Put the shared ``rebar`` logger's level back to *level*; report whether it moved.

    Unlike propagation, raising this level is a legitimate side effect of exercising a real
    entrypoint in-process (``rebar._cli.main`` installs the stderr handler, which pins the
    logger to WARNING). The LEAK is what must not survive the test, so this contains it
    instead of blaming the test: restoring per-test is strictly stronger for coverage
    integrity than a failure would be, because every test then starts from the same level
    and its log assertions are reproducible regardless of run order.
    """
    lg = logging.getLogger(REBAR_LOGGER_NAME)
    if lg.level == level:
        return False
    lg.setLevel(level)
    return True
