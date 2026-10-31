"""Rebind rebar's injectable seams at CHILD-interpreter start.

``tests/_child_hooks.py::child_env`` prepends this directory to a child's
``PYTHONPATH``, so Python's ``site`` machinery imports this module before the child
runs any rebar code. It moves the clock and compaction failpoint injection that used
to be environment reads inside ``src/rebar`` into test-owned code.

The environment names below are the reader half of the contract documented in
``tests/_child_hooks.py``; ``tests/unit/test_child_hooks.py`` asserts the two agree.

Nothing happens unless the matching variable is set, so this module is inert for any
other child that happens to inherit the path entry.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

HLC_NOW_ENV = "REBAR_TEST_HLC_NOW"
RENAME_BARRIER_ENV = "REBAR_TEST_COMPACT_RENAME_BARRIER"

#: How often the rename barrier re-checks for its release file.
BARRIER_POLL_SECONDS = 0.02


def _install_pinned_clock(raw: str) -> None:
    """Freeze the child's physical HLC clock at *raw* nanoseconds."""
    from rebar._store import hlc

    pinned = int(raw.strip())
    hlc._clock = lambda: pinned


def _install_rename_barrier(barrier: str) -> None:
    """Pause the child's compactor in the mid-retirement window.

    After the FIRST source rename the child writes a ``reached`` marker (its PID) and
    blocks until a ``release`` file appears, so the parent can SIGKILL it at a
    deterministic point with no timing race.
    """
    from rebar._commands import compact_txn

    bdir = Path(barrier)

    def _pause_at_first_rename(n_renamed: int) -> None:
        if n_renamed != 1:
            return
        (bdir / "reached").write_text(str(os.getpid()), encoding="utf-8")
        release = bdir / "release"
        while not release.exists():
            time.sleep(BARRIER_POLL_SECONDS)

    compact_txn._rename_barrier = _pause_at_first_rename


def _install() -> None:
    pinned = os.environ.get(HLC_NOW_ENV)
    if pinned:
        _install_pinned_clock(pinned)
    barrier = os.environ.get(RENAME_BARRIER_ENV)
    if barrier:
        _install_rename_barrier(barrier)


_install()
