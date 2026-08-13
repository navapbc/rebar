"""Shared primitives for the caplog coverage-integrity guard (see tests/conftest.py).

A ``caplog`` assertion whose records never arrive DOES NOT FAIL — ``assert not
[unexpected]`` is trivially true against an empty list. So a test can lose 100% of its
coverage and stay green, with nothing in the run output distinguishing it from a test
that genuinely verified something (bug 9ac2-e2f1-bb6e-4436).

``caplog`` captures through a handler on the ROOT logger, so records only reach it if the
shared parent logger both *emits* them and *propagates* them. There are **two** such shared
parents, not one — ``rebar._logging`` names both: library code logs under ``rebar.*``, and the
reconciler subprocess is imported top-level so its modules log under the sibling
``rebar_reconciler.*`` root. :data:`SHARED_LOGGER_NAMES` is that set, and every primitive here
operates over all of it; covering only ``rebar`` leaves every ``rebar_reconciler.*`` log
assertion unprotected (bug 9151-907b-471d-4a38).

For each shared root there are two process-global mutations that silently sever capture, and
BOTH are real:

* ``logging.getLogger(<root>).propagate = False`` — nothing under that root reaches the
  root handler. ``rebar.review_bot.config.configure_logging()`` did exactly this (bug
  b718), at module-import time, and never restored it. On pytest >= 8.4 this is only
  PARTLY mitigated: ``_pytest.logging.catching_logs.__enter__`` attaches the capture
  handler to every logger that is ALREADY non-propagating, but its own comment records the
  hole — "will miss loggers that *become* non-propagating after the ``__enter__``". Code
  that flips it mid-test therefore voids the rest of that test's assertions silently, and
  the mitigation is an implementation detail we should not be relying on.
* ``logging.getLogger(<root>).setLevel(...)`` above the record's level — the record is
  dropped at the originating logger before any handler is consulted.
  ``rebar._logging.install_stderr_handler()`` pins it to WARNING; every in-process
  ``rebar._cli.main(...)`` call in the suite does that to ``rebar``, and every in-process
  ``rebar_reconciler.__main__.main(...)`` call does it to ``rebar_reconciler``.
  ``caplog.at_level(INFO)`` without a ``logger=`` argument does NOT rescue this: it raises
  the ROOT level, not the shared parent's.

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

#: The shared parent loggers every rebar record inherits from, and therefore the points that
#: decide whether ``caplog`` can see them. ``rebar`` covers the library/CLI/MCP surfaces;
#: ``rebar_reconciler`` is the sibling root the reconciler subprocess's modules log under
#: (they are imported top-level, so they are NOT children of ``rebar``).
SHARED_LOGGER_NAMES: tuple[str, ...] = ("rebar", "rebar_reconciler")

_REMEDY = (
    "Do not mutate propagation on a shared rebar logger: it is process-global and not "
    "restored, so it voids every later caplog assertion under that root. Attach a "
    "handler to the specific logger instead (see "
    "rebar.review_bot.config.configure_logging), or scope the change and restore it in a "
    "finally/fixture teardown."
)


def propagation_failure(nodeid: str, *, phase: str) -> str | None:
    """Return a failure message if ``caplog`` can no longer see one of the shared roots.

    ``None`` means healthy. *phase* is ``"setup"`` (propagation was already off before this
    test ran — an import/collection-time or non-test code path did it) or ``"teardown"``
    (this test's own body did it, which is the blame we want).
    """
    broken = [name for name in SHARED_LOGGER_NAMES if not logging.getLogger(name).propagate]
    if not broken:
        return None
    name = broken[0]
    if phase == "teardown":
        cause = f"{nodeid} disabled it during its own body"
    else:
        cause = (
            f"it was already off when {nodeid} started — something outside a test body "
            "(module import at collection time, or a non-test code path) disabled it"
        )
    return (
        "caplog coverage integrity: "
        f'logging.getLogger("{name}").propagate is False — {cause}. '
        "caplog captures through a handler on the ROOT logger, so while this is off NO "
        f"{name}.* record reaches caplog and every later log assertion passes VACUOUSLY "
        "while verifying nothing. " + _REMEDY
    )


def restore_propagation() -> None:
    """Re-enable propagation on every shared root, so one offender does not cascade."""
    for name in SHARED_LOGGER_NAMES:
        logging.getLogger(name).propagate = True


def current_level() -> dict[str, int]:
    """Each shared root's own level (``logging.NOTSET`` when it inherits), keyed by name."""
    return {name: logging.getLogger(name).level for name in SHARED_LOGGER_NAMES}


def restore_level(levels: dict[str, int]) -> bool:
    """Put every shared root's level back to *levels*; report whether any of them moved.

    Unlike propagation, raising these levels is a legitimate side effect of exercising a real
    entrypoint in-process (``rebar._cli.main`` and ``rebar_reconciler.__main__.main`` each
    install the stderr handler, which pins their own root to WARNING). The LEAK is what must
    not survive the test, so this contains it instead of blaming the test: restoring per-test
    is strictly stronger for coverage integrity than a failure would be, because every test
    then starts from the same levels and its log assertions are reproducible regardless of
    run order.
    """
    moved = False
    for name, level in levels.items():
        lg = logging.getLogger(name)
        if lg.level != level:
            lg.setLevel(level)
            moved = True
    return moved
