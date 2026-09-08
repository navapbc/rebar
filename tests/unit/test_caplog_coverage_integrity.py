"""Coverage checks for logging assertions that use ``caplog``.

The guard restores propagation and logger levels on both ``rebar`` and
``rebar_reconciler``. The subprocess cases prove that teardown makes a guarded run red even
when a negative assertion passed vacuously, and that leaked logger levels cannot affect later
tests.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import _log_integrity  # noqa: E402

pytest_plugins = ["pytester"]


# ── detection primitive ───────────────────────────────────────────────────────


def test_both_shared_roots_are_guarded() -> None:
    """The reconciler root is a SIBLING of ``rebar``, so it needs its own coverage."""
    assert set(_log_integrity.SHARED_LOGGER_NAMES) == {"rebar", "rebar_reconciler"}


def test_no_failure_while_propagation_is_healthy() -> None:
    for name in _log_integrity.SHARED_LOGGER_NAMES:  # the autouse guard's precondition
        assert logging.getLogger(name).propagate is True
    assert _log_integrity.propagation_failure("t::x", phase="setup") is None
    assert _log_integrity.propagation_failure("t::x", phase="teardown") is None


@pytest.mark.parametrize("root", ["rebar", "rebar_reconciler"])
def test_teardown_phase_blames_the_test_that_disabled_propagation(root: str) -> None:
    lg = logging.getLogger(root)
    lg.propagate = False
    try:
        msg = _log_integrity.propagation_failure("tests/unit/t.py::test_poisoner", phase="teardown")
    finally:
        _log_integrity.restore_propagation()
    assert msg is not None
    assert f'logging.getLogger("{root}").propagate is False' in msg
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


def test_restore_propagation_reenables_every_shared_root() -> None:
    for name in _log_integrity.SHARED_LOGGER_NAMES:
        logging.getLogger(name).propagate = False
    _log_integrity.restore_propagation()
    for name in _log_integrity.SHARED_LOGGER_NAMES:
        assert logging.getLogger(name).propagate is True


# Pytest attaches handlers to loggers that are already non-propagating. Change propagation
# after capture begins so this negative assertion would pass without the guard.
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

# A logger fixed at WARNING drops INFO before caplog's root-level override can capture it.
# Both shared logger roots must be reset after each test.
_LEVEL_POISONED_TESTS = """
import logging


def test_level_poisoner():
    logging.getLogger({root!r}).setLevel(logging.WARNING)


def test_info_assertion(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger({root!r} + ".probe").info("hello")
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


@pytest.mark.parametrize("root", ["rebar", "rebar_reconciler"])
def test_without_the_guard_a_leaked_level_breaks_a_later_info_assertion(pytester, root) -> None:
    """The level vector, reproduced: an unrelated later test goes red, far from the cause."""
    pytester.makeconftest("")
    pytester.makepyfile(_LEVEL_POISONED_TESTS.format(root=root))
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=1, failed=1)
    assert result.ret != 0


@pytest.mark.parametrize("root", ["rebar", "rebar_reconciler"])
def test_with_the_guard_a_leaked_level_is_contained(pytester, root) -> None:
    """Same file, guard installed: the leak does not survive the test that caused it."""
    pytester.makeconftest(_GUARDED_CONFTEST.format(tests_dir=str(_TESTS_DIR)))
    pytester.makepyfile(_LEVEL_POISONED_TESTS.format(root=root))
    result = pytester.runpytest_subprocess("-p", "no:randomly")
    result.assert_outcomes(passed=2)
    assert result.ret == 0


@pytest.mark.parametrize("root", ["rebar", "rebar_reconciler"])
def test_restore_level_reports_whether_it_moved(root: str) -> None:
    baseline = _log_integrity.current_level()
    assert set(baseline) == set(_log_integrity.SHARED_LOGGER_NAMES)
    try:
        assert _log_integrity.restore_level(baseline) is False  # no drift, no change
        logging.getLogger(root).setLevel(logging.CRITICAL)
        assert _log_integrity.restore_level(baseline) is True
        assert _log_integrity.current_level() == baseline
    finally:
        logging.getLogger(root).setLevel(baseline[root])


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
