"""Protect caplog assertions from silent loss of rebar logger records.

The shared rebar parent covers library and reconciler logs. Disabling propagation can
hide records from caplog and must fail at the source. Raising the parent level can also
hide records. Restore it after each test because an entrypoint may legitimately change it.
Tests and the root conftest share these primitives.
"""

from __future__ import annotations

import logging

#: The shared parent logger every rebar record inherits from, and therefore the point that
#: decides whether ``caplog`` can see it. ``rebar`` covers the library/CLI/MCP surfaces and
#: the ``rebar.reconciler`` subpackage.
SHARED_LOGGER_NAMES: tuple[str, ...] = ("rebar",)

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
    """Restore shared logger levels and report whether they changed.

    Entry points may raise rebar to WARNING. Restoring the prior level after each test keeps
    later caplog assertions independent of run order.
    """
    moved = False
    for name, level in levels.items():
        lg = logging.getLogger(name)
        if lg.level != level:
            lg.setLevel(level)
            moved = True
    return moved
