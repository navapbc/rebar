"""The live-DC pass-health oracle must not fail OPEN on a signal-killed child.

Bug 0e1d-c698-c38d-4c3e.

The live Data Center tier is the epic-exit evidence that the reconciler mutates a real
instance correctly. Its pass-health assertions were written as a bare stderr scan::

    assert "Traceback" not in cp.stderr, f"inbound pass raised:\n{cp.stderr[-2000:]}"

A child killed by a signal is terminated by the kernel before it can write, so its stderr
is EMPTY -- ``"Traceback" not in ""`` is True and the assertion PASSES. A reaped or
OOM-killed reconciler pass was therefore recorded as proof the pass succeeded. That is the
dangerous direction: a spuriously red test costs time, a spuriously GREEN one destroys the
evidence someone is relying on it to produce.

These tests live in the UNIT tier on purpose. The live-DC tier needs a Jira DC harness and
self-skips without one, so a regression test placed there could never gate this in CI --
and a gate that cannot run is the same fail-open one layer up. Killing a child needs no
Jira at all, so the mechanism is exercised here, for real, on every run.

Sibling of bug f0fb-de7a-b315-4508, which fixed the same construct in ``tests/e2e``.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _child_diag import assert_child_ran_clean  # noqa: E402

#: A child that is killed by a signal rather than exiting. It writes NOTHING first, which is
#: the whole point: the empty stderr is what the old assertion mistook for a healthy pass.
_SUICIDE = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"


def _killed_child() -> subprocess.CompletedProcess[str]:
    """Really run and really kill a child -- do not fake the CompletedProcess.

    A hand-built ``CompletedProcess(returncode=-9, stderr="")`` would encode this test's
    own belief about what a signal-killed child looks like. Spawning one proves it.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _SUICIDE], text=True, capture_output=True, check=False
    )
    assert proc.returncode == -signal.SIGKILL, (
        f"fixture precondition: the child was meant to die on SIGKILL, got {proc.returncode}"
    )
    assert proc.stderr == "", (
        f"fixture precondition: a signal-killed child writes no stderr, got {proc.stderr!r}"
    )
    return proc


def test_a_signal_killed_pass_cannot_satisfy_the_pass_health_oracle() -> None:
    """The fail-open itself: empty stderr must NOT read as a healthy pass."""
    proc = _killed_child()

    with pytest.raises(AssertionError) as excinfo:
        assert_child_ran_clean(proc, what="inbound pass")

    assert "inbound pass" in str(excinfo.value)


def test_the_failure_names_the_signal_that_killed_the_child() -> None:
    """AC2: after the fix the message must NAME the signal, not just fail.

    A bare "the pass failed" sends the reader hunting for a product regression. Naming
    SIGKILL tells them an external process reaped it -- the whole reason bug
    f0fb-de7a-b315-4508 built ``child_failure_detail``.
    """
    proc = _killed_child()

    with pytest.raises(AssertionError) as excinfo:
        assert_child_ran_clean(proc, what="outbound pass")

    message = str(excinfo.value)
    assert "SIGKILL" in message, f"the message must name the signal; got: {message}"
    assert "-9" in message or "returncode" in message


def test_a_plain_nonzero_exit_is_also_not_a_healthy_pass() -> None:
    """A pass that exits 1 without a traceback is not evidence either."""
    proc = subprocess.run(
        [sys.executable, "-c", "raise SystemExit(1)"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 1 and "Traceback" not in proc.stderr

    with pytest.raises(AssertionError) as excinfo:
        assert_child_ran_clean(proc, what="outbound create pass")

    assert "1" in str(excinfo.value)


def test_a_clean_pass_still_passes() -> None:
    """The refactoring litmus: a healthy pass must not start failing."""
    proc = subprocess.run(
        [sys.executable, "-c", "print('RECON: converged')"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0

    assert_child_ran_clean(proc, what="inbound pass")


def test_a_traceback_still_fails_even_when_the_child_exits_zero() -> None:
    """The behaviour the old assertion DID have must survive the fix.

    A reconciler that swallows an exception can print a traceback and still exit 0; that was
    the original point of the stderr scan and it must not be traded away for the returncode
    check.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('Traceback (most recent call last):\\n  boom\\n')",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0 and "Traceback" in proc.stderr

    with pytest.raises(AssertionError) as excinfo:
        assert_child_ran_clean(proc, what="inbound link pass")

    assert "inbound link pass" in str(excinfo.value)


def test_a_documented_sentinel_exit_can_be_accepted_explicitly() -> None:
    """``ok_codes`` carries the reconciler's legacy 3/4/75 sentinels where a site allows them.

    ``rebar_reconciler/__main__.py:65-72`` maps RESCHEDULE to legacy exit 75, and
    ``test_reconcile_pass.py:200`` already accepts ``(0, 75)``. A site that means it says so;
    the DEFAULT stays 0, matching ``_dc_support.py:658``.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "raise SystemExit(75)"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 75

    assert_child_ran_clean(proc, what="reschedulable pass", ok_codes=(0, 75))

    with pytest.raises(AssertionError):
        assert_child_ran_clean(proc, what="reschedulable pass")
