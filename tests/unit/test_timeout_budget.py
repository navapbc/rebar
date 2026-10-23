"""Enforce a local per-test budget across setup, call, and teardown (73b8, 797b).

The ini setting makes bare pytest and make test enforce the same hang guard
as CI. timeout_func_only must stay absent. Its true value once left fixtures
unguarded, including a teardown hang. Slow tests and fixtures can raise
their own limit with @pytest.mark.timeout(N).

Config checks pin the keys. Generated-project subprocess tests show body,
setup, and teardown timeouts actually fire, and a marker overrides the ini
for a slow body or fixture. A tiny generated budget keeps the proof fast
independent of this repo's configured value. Subprocesses contain expected
failures. These arms catch plugin/config drift that text checks miss."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from _nested_pytest import run_nested_pytest
from _subprocess_env import subprocess_env

_INI_BUDGET_SECS = 300

# Every generated project mirrors this repo's ini keys (--strict-config would reject them if
# pytest-timeout did not register them as real ini options) at a deliberately TINY budget so
# each arm costs seconds, not minutes. No `timeout_func_only`: like the real ini (bug
# 797b-bbc4-01cf-42d5), the budget must cover setup and teardown, not just the body.
_NESTED_INI = textwrap.dedent(
    """\
    [tool.pytest.ini_options]
    addopts = "--strict-markers --strict-config"
    timeout = 2
    timeout_method = "thread"
    """
)


def test_the_configured_budget_is_the_measured_one() -> None:
    """The ini carries the budget keys — and does NOT carry ``timeout_func_only``."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    ini = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]
    assert ini["timeout"] == _INI_BUDGET_SECS
    # `thread` is load-bearing: the default `signal` method arms SIGALRM in the main thread
    # and cannot fire while a worker is blocked in a C-level flock/socket/subprocess call.
    assert ini["timeout_method"] == "thread"
    # ABSENT on purpose (bug 797b-bbc4-01cf-42d5): `timeout_func_only = true` confined the
    # budget to the test body, leaving fixture setup/teardown hangs unguarded — the exact
    # phase the guard was built for (89d5-61da-b621-47f8 was a teardown hang). pytest-timeout's
    # upstream default is False; a slow-fixture test raises its OWN budget with a plain
    # @pytest.mark.timeout(N) marker instead.
    assert "timeout_func_only" not in ini, (
        "timeout_func_only reappeared in [tool.pytest.ini_options] — that key exempts fixture "
        "setup/teardown from the hang budget (bug 797b-bbc4-01cf-42d5); raise the slow test's "
        "own budget with @pytest.mark.timeout(N) instead"
    )


@pytest.mark.timeout(180)
def test_a_marked_test_may_outrun_the_ini_budget(tmp_path: Path) -> None:
    """A per-test marker beats the ini, so a legitimately slow test has a supported escape.

    Out-of-process against a generated project carrying a deliberately TINY ini budget, so the
    arm's cost is a few seconds regardless of this repo's own budget. The generated test sleeps
    well past that project's ini budget and carries a generous ``@pytest.mark.timeout(N)``; a
    returncode of 0 can only mean the marker won.
    """
    project = tmp_path / "override_probe"
    project.mkdir()
    (project / "pyproject.toml").write_text(_NESTED_INI, encoding="utf-8")
    (project / "test_marked_over_budget.py").write_text(
        textwrap.dedent(
            """\
            import time

            import pytest


            @pytest.mark.timeout(120)
            def test_outruns_the_ini_but_not_the_marker():
                started = time.monotonic()
                time.sleep(5)
                assert time.monotonic() - started > 2
            """
        ),
        encoding="utf-8",
    )

    # Same shared helper as the negative arms below: story 25b4's uniqueness guard keeps the
    # nested launch in exactly one place.
    completed = run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "test_marked_over_budget.py",
        env=subprocess_env(),
        cwd=project,
        timeout=120,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, (
        "a test sleeping past the project's 2 s ini budget did NOT pass under a generous "
        "@pytest.mark.timeout(120) — the marker no longer overrides the ini, so every "
        f"deliberately-slow test in this tree is at risk:\n{combined}"
    )


def _assert_nested_run_timed_out(completed, phase: str) -> None:
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, (
        f"the over-budget {phase} PASSED with only the ini keys configured — the budget is "
        f"not being applied to {phase}, so the fixture-phase guard (bug 797b-bbc4-01cf-42d5) "
        f"is vacuous:\n{combined}"
    )
    assert "Timeout" in combined, (
        f"the run failed, but not with a pytest-timeout Timeout naming the hung {phase} — "
        f"something else broke and the budget is unproven:\n{combined}"
    )


@pytest.mark.timeout(180)
def test_an_unmarked_over_budget_test_is_killed_by_the_ini_alone(tmp_path: Path) -> None:
    """The body-phase negative arm: with ONLY the ini keys configured, an over-budget test fails.

    Run out-of-process against a generated project, because an in-tree test that must fail
    cannot be committed.
    """
    project = tmp_path / "budget_probe"
    project.mkdir()
    (project / "pyproject.toml").write_text(_NESTED_INI, encoding="utf-8")
    (project / "test_over_budget.py").write_text(
        textwrap.dedent(
            """\
            import time


            def test_sleeps_past_the_budget():
                time.sleep(30)
            """
        ),
        encoding="utf-8",
    )

    # Routed through the shared helper rather than launching pytest directly: story 25b4's
    # uniqueness guard (tests/unit/test_nested_pytest_uniqueness.py) allows exactly one nested
    # launch site, and a second copy here would re-open the --basetemp drift it just closed.
    completed = run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "test_over_budget.py",
        env=subprocess_env(),
        cwd=project,
        timeout=120,
    )
    _assert_nested_run_timed_out(completed, "test body")


@pytest.mark.timeout(180)
def test_a_hanging_fixture_setup_is_bounded_by_the_ini(tmp_path: Path) -> None:
    """The setup-phase negative arm: a fixture that hangs during SETUP is expired and named.

    This is the coverage ``timeout_func_only = true`` silently removed (bug
    ``797b-bbc4-01cf-42d5``): under that key this nested run would sleep to the subprocess
    ceiling instead of failing at the 2 s budget.
    """
    project = tmp_path / "setup_probe"
    project.mkdir()
    (project / "pyproject.toml").write_text(_NESTED_INI, encoding="utf-8")
    (project / "test_hanging_setup.py").write_text(
        textwrap.dedent(
            """\
            import time

            import pytest


            @pytest.fixture
            def stuck_setup():
                time.sleep(30)
                yield "never reached in time"


            def test_never_gets_to_run(stuck_setup):
                pass
            """
        ),
        encoding="utf-8",
    )

    completed = run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "test_hanging_setup.py",
        env=subprocess_env(),
        cwd=project,
        timeout=120,
    )
    _assert_nested_run_timed_out(completed, "fixture setup")


@pytest.mark.timeout(180)
def test_a_hanging_teardown_is_bounded_by_the_ini(tmp_path: Path) -> None:
    """The teardown-phase negative arm: a fixture that hangs during TEARDOWN is expired.

    Teardown is the original incident shape: ``89d5-61da-b621-47f8`` was a ``TestClient``
    context exit awaiting ``queue.join()`` under a 1200 s drain, invisible behind the last
    ``-q`` dot for the full 60-minute job cap.
    """
    project = tmp_path / "teardown_probe"
    project.mkdir()
    (project / "pyproject.toml").write_text(_NESTED_INI, encoding="utf-8")
    (project / "test_hanging_teardown.py").write_text(
        textwrap.dedent(
            """\
            import time

            import pytest


            @pytest.fixture
            def stuck_teardown():
                yield "fine until the exit"
                time.sleep(30)


            def test_body_is_fast_but_teardown_hangs(stuck_teardown):
                pass
            """
        ),
        encoding="utf-8",
    )

    completed = run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "test_hanging_teardown.py",
        env=subprocess_env(),
        cwd=project,
        timeout=120,
    )
    _assert_nested_run_timed_out(completed, "fixture teardown")


@pytest.mark.timeout(180)
def test_a_marker_covers_legitimately_slow_fixture_time(tmp_path: Path) -> None:
    """The fixture-remedy arm: ``@pytest.mark.timeout(N)`` covers fixture time too.

    With ``func_only`` dropped, fixture time is charged to the test; the sanctioned remedy for
    a legitimately slow fixture is to RAISE that test's own budget with a plain marker (the
    pytest-timeout README's first-line advice), never to restore the body-only exemption. The
    generated fixture sleeps past the project's 2 s ini budget, and the test passes because its
    marker budget covers setup + call.
    """
    project = tmp_path / "remedy_probe"
    project.mkdir()
    (project / "pyproject.toml").write_text(_NESTED_INI, encoding="utf-8")
    (project / "test_slow_fixture_marked.py").write_text(
        textwrap.dedent(
            """\
            import time

            import pytest


            @pytest.fixture
            def slow_but_legitimate():
                time.sleep(4)
                return "worth the wait"


            @pytest.mark.timeout(30)
            def test_marker_budget_absorbs_the_fixture(slow_but_legitimate):
                assert slow_but_legitimate == "worth the wait"
            """
        ),
        encoding="utf-8",
    )

    completed = run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "test_slow_fixture_marked.py",
        env=subprocess_env(),
        cwd=project,
        timeout=120,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, (
        "a test whose fixture sleeps past the 2 s ini budget did NOT pass under "
        "@pytest.mark.timeout(30) — the marker no longer covers fixture time, so slow-fixture "
        f"tests have lost their sanctioned remedy (bug 797b-bbc4-01cf-42d5):\n{combined}"
    )
