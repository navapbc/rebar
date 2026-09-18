"""Sandbox POSIX semaphore capability probes for bug 0eba-cfd8-34b0-4cb7."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

pytest_plugins = ("pytester",)


def test_posix_named_semaphore_probe_reports_unavailable_on_eperm() -> None:
    from _sandbox_capabilities import posix_named_semaphore_probe_with_factory

    def denied(_kind: int, _value: int, _maxvalue: int, _name: str, _unlink: bool) -> object:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    probe = posix_named_semaphore_probe_with_factory(denied)

    assert not probe.available
    assert "POSIX named semaphore unavailable" in probe.reason
    assert "Operation not permitted" in probe.reason


def test_posix_named_semaphore_probe_reports_available_when_sem_open_succeeds() -> None:
    from _sandbox_capabilities import posix_named_semaphore_probe_with_factory

    calls: list[tuple[int, int, int, str, bool]] = []

    def succeeds(kind: int, value: int, maxvalue: int, name: str, unlink: bool) -> object:
        calls.append((kind, value, maxvalue, name, unlink))
        return object()

    probe = posix_named_semaphore_probe_with_factory(succeeds)

    assert probe.available
    assert calls
    assert calls[0][0:3] == (1, 1, 1)
    assert calls[0][3].startswith("/rebar-")
    assert calls[0][4] is True


def test_posix_named_semaphore_skip_summary_reports_all_four(pytester: pytest.Pytester) -> None:
    tests_dir = Path(__file__).parent
    pytester.makeconftest(
        f"""
        import sys
        sys.path.insert(0, {str(tests_dir)!r})

        import pytest
        from _sandbox_capabilities import (
            CapabilityProbe,
            record_counted_skip,
            report_counted_skips,
        )

        def pytest_configure(config):
            config.addinivalue_line(
                "markers",
                "requires_posix_named_semaphore: needs POSIX named semaphores",
            )

        def pytest_runtest_setup(item):
            if item.get_closest_marker("requires_posix_named_semaphore") is None:
                return
            probe = CapabilityProbe(
                False,
                "POSIX named semaphore unavailable: Operation not permitted",
            )
            record_counted_skip(item.config, probe.reason)
            pytest.skip(probe.reason)

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            report_counted_skips(terminalreporter, config)
        """
    )
    pytester.makepyfile(
        test_sem="""
        import pytest

        pytestmark = pytest.mark.requires_posix_named_semaphore

        def test_one(): pass
        def test_two(): pass
        def test_three(): pass
        def test_four(): pass
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(skipped=4)
    result.stdout.fnmatch_lines(
        [
            "*sandbox compatibility skips: 4*",
            "*POSIX named semaphore unavailable: Operation not permitted: 4*",
        ]
    )


def test_posix_named_semaphore_marker_runs_when_probe_is_available(
    pytester: pytest.Pytester,
) -> None:
    tests_dir = Path(__file__).parent
    pytester.makeconftest(
        f"""
        import sys
        sys.path.insert(0, {str(tests_dir)!r})

        import pytest
        from _sandbox_capabilities import CapabilityProbe, report_counted_skips

        def pytest_configure(config):
            config.addinivalue_line(
                "markers",
                "requires_posix_named_semaphore: needs POSIX named semaphores",
            )

        def pytest_runtest_setup(item):
            if item.get_closest_marker("requires_posix_named_semaphore") is None:
                return
            probe = CapabilityProbe(True)
            if probe.available:
                return
            raise AssertionError("available probe must not skip")

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            report_counted_skips(terminalreporter, config)
        """
    )
    pytester.makepyfile(
        test_sem="""
        import pytest

        pytestmark = pytest.mark.requires_posix_named_semaphore

        def test_one(): pass
        def test_two(): pass
        def test_three(): pass
        def test_four(): pass
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=4)
    assert "sandbox compatibility skips" not in result.stdout.str()
