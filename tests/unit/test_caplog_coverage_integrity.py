"""Self-tests for the caplog coverage-integrity guard (tests/conftest.py + tests/_log_integrity.py).

The guard exists because an unreachable ``caplog`` assertion FAILS OPEN: once something
sets ``logging.getLogger("rebar").propagate = False`` — a process-global mutation that is
never restored — no ``rebar.*`` record reaches ``caplog`` again, and every later negative
log assertion passes VACUOUSLY while verifying nothing (bug 9ac2). Nothing in the run
output distinguishes that from a real verification, so the defect can only be caught by a
guard, not by reading a test result.

These tests prove the guard actually fires, and prove it in BOTH directions:

* the detection primitive is silent while propagation is healthy and, once it is off,
  returns a message that names the offending test;
* end-to-end via ``pytester``, on one and the same poisoned test file:
  - WITHOUT the guard the void negative assertion PASSES (the fail-open, reproduced);
  - WITH the guard the poisoner is blamed by name and the previously-green assertion
    goes RED, because propagation was restored and the record it forbids now arrives.

The second pair is the discriminating evidence: the only difference between the runs is
the guard, and it flips a green no-op into a red failure.

The same pytester pairing covers the LEVEL vector — a shared ``rebar`` logger pinned to
WARNING drops INFO records at the source, which ``caplog.at_level(INFO)`` cannot undo —
where the guard's job is containment rather than blame, since raising the level is a
legitimate side effect of running a real entrypoint in-process.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import _log_integrity  # noqa: E402

pytest_plugins = ["pytester"]


# ── detection primitive ───────────────────────────────────────────────────────


def test_no_failure_while_propagation_is_healthy() -> None:
    assert logging.getLogger("rebar").propagate is True  # the autouse guard's precondition
    assert _log_integrity.propagation_failure("t::x", phase="setup") is None
    assert _log_integrity.propagation_failure("t::x", phase="teardown") is None


def test_teardown_phase_blames_the_test_that_disabled_propagation() -> None:
    lg = logging.getLogger("rebar")
    lg.propagate = False
    try:
        msg = _log_integrity.propagation_failure("tests/unit/t.py::test_poisoner", phase="teardown")
    finally:
        _log_integrity.restore_propagation()
    assert msg is not None
    assert "tests/unit/t.py::test_poisoner disabled it during its own body" in msg
    assert "VACUOUSLY" in msg


def test_setup_phase_reports_an_out_of_band_poisoning() -> None:
    lg = logging.getLogger("rebar")
    lg.propagate = False
    try:
        msg = _log_integrity.propagation_failure("tests/unit/t.py::test_victim", phase="setup")
    finally:
        _log_integrity.restore_propagation()
    assert msg is not None
    # A victim must not be blamed as the culprit: the setup message says the damage
    # predates this test rather than attributing it to the test body.
    assert "already off when tests/unit/t.py::test_victim started" in msg
    assert "disabled it during its own body" not in msg


def test_restore_propagation_reenables_it() -> None:
    logging.getLogger("rebar").propagate = False
    _log_integrity.restore_propagation()
    assert logging.getLogger("rebar").propagate is True


# ── end-to-end wiring (pytester): the same poisoned file, with and without the guard ──

# A test that kills propagation the way production code did (bug b718) and then makes a
# NEGATIVE caplog assertion — the shape that passes vacuously when records never arrive.
#
# The kill has to happen INSIDE the capture window to void anything on pytest >= 8.4:
# `_pytest.logging.catching_logs.__enter__` attaches the capture handler to every logger
# that is ALREADY non-propagating, so a logger poisoned before the phase started is still
# captured. Its own comment records the remaining hole — "will miss loggers that *become*
# non-propagating after the `__enter__`" — and that hole is exactly what production code
# calling `configure_logging()` mid-test falls into. The window is one test phase wide
# rather than process-wide, and inside it the assertion is silently void.
_POISONED_TESTS = """
import logging


def test_kills_propagation_mid_capture(caplog):
    with caplog.at_level(logging.WARNING, logger="rebar.probe"):
        logging.getLogger("rebar").propagate = False
        logging.getLogger("rebar.probe").warning("boom")
    # Reads as a verification; verifies nothing, because the record never arrived.
    assert not any("boom" in r.getMessage() for r in caplog.records)
"""

_GUARDED_CONFTEST = """
import sys
sys.path.insert(0, {tests_dir!r})
from typing import Iterator

import pytest

import _log_integrity


@pytest.fixture(autouse=True)
def _rebar_log_propagation_guard(request) -> Iterator[None]:
    nodeid = request.node.nodeid
    problem = _log_integrity.propagation_failure(nodeid, phase="setup")
    if problem is not None:
        _log_integrity.restore_propagation()
        pytest.fail(problem, pytrace=False)
    baseline_level = _log_integrity.current_level()
    try:
        yield
    finally:
        _log_integrity.restore_level(baseline_level)
    problem = _log_integrity.propagation_failure(nodeid, phase="teardown")
    if problem is not None:
        _log_integrity.restore_propagation()
        pytest.fail(problem, pytrace=False)
"""

# The second vector: the level, not propagation. An INFO record is dropped at the
# originating logger when the shared `rebar` logger is pinned to WARNING, and
# caplog.at_level(INFO) with no `logger=` argument cannot rescue it because it raises the
# ROOT level. This is what `rebar._logging.install_stderr_handler` — reached by every
# in-process `rebar._cli.main(...)` call — leaks today.
_LEVEL_POISONED_TESTS = """
import logging


def test_level_poisoner():
    logging.getLogger("rebar").setLevel(logging.WARNING)


def test_info_assertion(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("rebar.probe").info("hello")
    assert any("hello" in r.getMessage() for r in caplog.records)
"""


def test_without_the_guard_the_void_assertion_passes_vacuously(pytester) -> None:
    """The fail-open, reproduced: the run is GREEN and says nothing about the void."""
    pytester.makeconftest("")
    pytester.makepyfile(_POISONED_TESTS)
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=1)
    assert result.ret == 0


def test_with_the_guard_the_void_assertion_is_failed_and_named(pytester) -> None:
    """Same file, guard installed: the run goes RED and blames the test that did it."""
    pytester.makeconftest(_GUARDED_CONFTEST.format(tests_dir=str(_TESTS_DIR)))
    pytester.makepyfile(_POISONED_TESTS)
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    # The body still "passes" — a vacuous assertion cannot be made to raise after the fact —
    # but the guard's teardown errors on that very test, so the assertion no longer reports
    # success and the report names the SOURCE rather than a downstream victim.
    result.assert_outcomes(passed=1, errors=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(
        ["*test_kills_propagation_mid_capture*disabled it during its own body*"]
    )


def test_the_guard_does_not_cascade_onto_an_innocent_later_test(pytester) -> None:
    """Only the source is blamed: the test after the poisoner runs against healthy logging."""
    pytester.makeconftest(_GUARDED_CONFTEST.format(tests_dir=str(_TESTS_DIR)))
    pytester.makepyfile(
        _POISONED_TESTS
        + """

def test_innocent_bystander(caplog):
    assert logging.getLogger("rebar").propagate is True  # restored, not inherited broken
    with caplog.at_level(logging.WARNING, logger="rebar.probe"):
        logging.getLogger("rebar.probe").warning("boom")
    assert any("boom" in r.getMessage() for r in caplog.records)
"""
    )
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=2, errors=1)


def test_without_the_guard_a_leaked_level_breaks_a_later_info_assertion(pytester) -> None:
    """The level vector, reproduced: an unrelated later test goes red, far from the cause."""
    pytester.makeconftest("")
    pytester.makepyfile(_LEVEL_POISONED_TESTS)
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=1, failed=1)
    assert result.ret != 0


def test_with_the_guard_a_leaked_level_is_contained(pytester) -> None:
    """Same file, guard installed: the leak does not survive the test that caused it."""
    pytester.makeconftest(_GUARDED_CONFTEST.format(tests_dir=str(_TESTS_DIR)))
    pytester.makepyfile(_LEVEL_POISONED_TESTS)
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=2)
    assert result.ret == 0


def test_restore_level_reports_whether_it_moved() -> None:
    baseline = _log_integrity.current_level()
    try:
        assert _log_integrity.restore_level(baseline) is False  # no drift, no change
        logging.getLogger("rebar").setLevel(logging.CRITICAL)
        assert _log_integrity.restore_level(baseline) is True
        assert _log_integrity.current_level() == baseline
    finally:
        logging.getLogger("rebar").setLevel(baseline)


def test_a_clean_run_is_unaffected(pytester) -> None:
    pytester.makeconftest(_GUARDED_CONFTEST.format(tests_dir=str(_TESTS_DIR)))
    pytester.makepyfile(
        """
        import logging


        def test_logs_normally(caplog):
            with caplog.at_level(logging.WARNING, logger="rebar.probe"):
                logging.getLogger("rebar.probe").warning("fine")
            assert any("fine" in r.getMessage() for r in caplog.records)
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=1)
    assert result.ret == 0
