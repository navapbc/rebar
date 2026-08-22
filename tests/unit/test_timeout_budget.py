"""The per-test hang budget in ``[tool.pytest.ini_options]`` really bites, and is overridable.

Story ``73b8-2d79-3046-44ce``. Before this, the budget existed ONLY on the CI command lines,
so ``make test`` and a bare local ``pytest`` had none: a test that deadlocked locally hung
until the developer noticed, and the CI-only guard could rot without any local signal (bugs
``e394-9433-c839-4c9f``, ``9bb7-4430-1202-4d93``).

Both arms are needed and neither can be the other:

* The POSITIVE arm proves the ini value is not merely *present* but *overridable* — a test
  that must legitimately run past the budget passes when it carries
  ``@pytest.mark.timeout(N, func_only=True)``. If the marker stopped winning over the ini,
  every deliberately-slow test in the tree would start failing and this arm goes red first.
* The NEGATIVE arm proves the three ini keys actually EXPIRE an over-budget test on the
  installed pytest-timeout, rather than merely being present in ``pyproject.toml``. It
  cannot be written in-process — a test that must fail cannot be committed — so it runs
  pytest on a generated project in a subprocess and asserts the ``Timeout`` failure THERE.
  Without it a pytest-timeout upgrade that renamed or dropped a key would leave the config
  assertions green while the budget silently stopped biting.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_INI_BUDGET_SECS = 20


def _ini_timeout() -> int:
    import tomllib

    root = Path(__file__).resolve().parents[2]
    ini = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return int(ini["tool"]["pytest"]["ini_options"]["timeout"])


def test_the_configured_budget_is_the_measured_one() -> None:
    """The ini carries the three keys, at the value the CI lanes were lowered to."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    ini = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]
    assert ini["timeout"] == _INI_BUDGET_SECS
    # `thread` is load-bearing: the default `signal` method arms SIGALRM in the main thread
    # and cannot fire while a worker is blocked in a C-level flock/socket/subprocess call.
    assert ini["timeout_method"] == "thread"
    # func_only charges the budget to the test BODY, so a slow fixture cannot spend a test's
    # allowance (and a session fixture cannot blow every test's).
    assert ini["timeout_func_only"] is True


# timeout: this test deliberately runs PAST the ini budget to prove the marker overrides it;
# that is the whole point of the arm, so it carries its own generous allowance.
@pytest.mark.timeout(_INI_BUDGET_SECS * 3, func_only=True)
def test_a_marked_test_may_outrun_the_ini_budget() -> None:
    """A per-test marker beats the ini, so a legitimately slow test has a supported escape."""
    budget = _ini_timeout()
    started = time.monotonic()
    # Sleeping just past the budget is the only way to exercise the override; the marker
    # above bounds it, so this cannot become an unbounded hang.
    time.sleep(budget + 2)
    assert time.monotonic() - started > budget, (
        "the body did not actually outrun the ini budget, so the override was never exercised"
    )


@pytest.mark.timeout(180, func_only=True)
def test_an_unmarked_over_budget_test_is_killed_by_the_ini_alone(tmp_path: Path) -> None:
    """The negative arm: with ONLY the ini keys configured, an over-budget test fails.

    Run out-of-process against a generated project, because an in-tree test that must fail
    cannot be committed. The generated ini mirrors this repo's keys (including
    ``--strict-config``, which would reject them if pytest-timeout did not register them as
    real ini options) but uses a 2 s budget so the arm costs seconds, not a minute.
    """
    project = tmp_path / "budget_probe"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [tool.pytest.ini_options]
            addopts = "--strict-markers --strict-config"
            timeout = 2
            timeout_method = "thread"
            timeout_func_only = true
            """
        ),
        encoding="utf-8",
    )
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

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "test_over_budget.py"],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, (
        "the over-budget test PASSED with only the ini keys configured — the budget is not "
        f"being applied, so the whole gate is vacuous:\n{combined}"
    )
    assert "Timeout" in combined, (
        "the run failed, but not with a pytest-timeout Timeout — something else broke and the "
        f"budget is unproven:\n{combined}"
    )
