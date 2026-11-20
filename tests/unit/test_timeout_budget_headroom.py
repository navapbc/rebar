"""Keep the per-test hang budget above legitimate CI test costs (3fa7).

The coverage-enabled Ubuntu 3.13 gate has about 14 whole-tree scans taking
18.7 to 29.9 seconds. A 20-second budget killed passing tests. With thread
mode, pytest-timeout exits the xdist worker instead of reporting Failed:
Timeout, leaving misleading worker-crashed errors. A config-value test
cannot prove headroom, so this module checks the measured floor and required
2x margin."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import tomllib
from _isolation import GIT_PROBE_ATTEMPTS, GIT_PROBE_TIMEOUT_SECONDS

pytestmark = pytest.mark.unit

# Slowest legitimate call: 29.93s on a green, crash-free gating run with
# `-n 4 --dist worksteal --cov=rebar`. Whole-tree scans spend this time in the
# body, so fixture accounting cannot discount it. Change the floor only after
# fresh gating-leg measurement (soppy-logophilic-husky).
_MEASURED_SLOWEST_LEGITIMATE_CALL_SECS = 29.93

# Allow 2x headroom for load, coverage, and worker restarts. In failed runs,
# restart costs moved surviving scans from about 16s to nearly 20s.
_REQUIRED_HEADROOM = 2.0


def _ini() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["tool"]["pytest"]["ini_options"]


def _timeout_marker_seconds(path: Path, test_name: str) -> int | float | None:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == test_name:
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "timeout"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, int | float)
                ):
                    return decorator.args[0].value
    return None


def test_the_budget_clears_the_slowest_legitimate_test_with_headroom() -> None:
    """The budget must exceed the measured worst legitimate body by the headroom factor."""
    budget = float(_ini()["timeout"])  # type: ignore[arg-type]
    floor = _MEASURED_SLOWEST_LEGITIMATE_CALL_SECS * _REQUIRED_HEADROOM

    assert budget >= floor, (
        f"the per-test hang budget is {budget:g}s, but the slowest LEGITIMATE test body "
        f"measured on the gating ubuntu py3.13 leg is "
        f"{_MEASURED_SLOWEST_LEGITIMATE_CALL_SECS:g}s, so the budget must be at least "
        f"{floor:g}s to carry {_REQUIRED_HEADROOM:g}x headroom. At {budget:g}s the budget "
        f"sits inside the band of costs that passing tests legitimately incur. Because "
        f"timeout_method is 'thread', pytest-timeout expires them with os._exit(1) and takes "
        f"the whole xdist worker down, so this shows up as `worker 'gwN' crashed` / `node "
        f"down: Not properly terminated` on innocent tests — never as `Failed: Timeout`."
    )


def test_the_budget_is_still_a_real_hang_guard() -> None:
    """Headroom must not be bought by disabling the guard (bug 89d5-61da-b621-47f8).

    The opposite failure to the one above: a budget raised to absurdity, or a dropped
    `thread` method, would satisfy the headroom check while restoring the 60-minute silent
    hang that ticket 89d5 was filed for. `thread` is load-bearing because the default
    `signal` method cannot fire while a worker is blocked in a C-level flock/socket/
    subprocess call.
    """
    ini = _ini()
    budget = float(ini["timeout"])  # type: ignore[arg-type]

    assert budget <= 600, (
        f"the per-test hang budget is {budget:g}s. A budget that large stops bounding "
        "anything useful: the CI job cap is 60 minutes, and ticket 89d5-61da-b621-47f8 was "
        "filed precisely because a job-level cap lets one blocked test burn the whole hour "
        "without naming itself."
    )
    assert ini["timeout_method"] == "thread", (
        "timeout_method must stay 'thread': the default 'signal' method arms SIGALRM in the "
        "main thread and cannot fire while a worker is blocked in a C-level call, which is "
        "the exact hang shape this suite exercises."
    )


# The autouse `_no_repo_commits` guard (tests/conftest.py) samples this checkout's HEAD with
# `git rev-parse` in setup and again in teardown, each sample bounded by
# GIT_PROBE_TIMEOUT_SECONDS and re-tried up to GIT_PROBE_ATTEMPTS times. A per-test marker
# covers setup + call + teardown (no timeout_func_only), so a marker below that bound can
# kill the worker inside the guard before the test body runs (stinky-blushing-gelding).
_AUTOUSE_GIT_PROBE_BOUND_SECS = 2 * GIT_PROBE_TIMEOUT_SECONDS * GIT_PROBE_ATTEMPTS


def _timeout_marker_values(source: str) -> list[tuple[int, int | float]]:
    """(line, seconds) for every literal ``pytest.mark.timeout(N)`` call in *source*.

    Covers decorators, ``pytestmark = ...`` assignments and lists of marks alike, because
    each is the same call node. A non-literal argument is not a number this census can
    check, so it is skipped rather than guessed.
    """
    found: list[tuple[int, int | float]] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "timeout"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "mark"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            continue
        value = node.args[0].value
        if isinstance(value, int | float) and not isinstance(value, bool):
            found.append((node.lineno, value))
    return sorted(found)


def test_the_marker_census_reads_every_literal_marker_shape() -> None:
    """The census sees decorator and ``pytestmark`` markers and skips non-literals."""
    source = (
        "import pytest\n"
        "pytestmark = [pytest.mark.unit, pytest.mark.timeout(4)]\n"
        "LIMIT = 7\n"
        "@pytest.mark.timeout(2.5)\n"
        "def test_a(): pass\n"
        "@pytest.mark.timeout(LIMIT)\n"
        "def test_b(): pass\n"
        "@pytest.mark.timeout(seconds=9)\n"
        "def test_c(): pass\n"
    )

    assert _timeout_marker_values(source) == [(2, 4), (4, 2.5)]


def test_no_timeout_marker_is_tighter_than_the_autouse_git_probe_bound() -> None:
    """Every per-test marker must leave room for the autouse repo-isolation git probes.

    Under ``timeout_method = "thread"`` an expired marker kills the whole xdist worker, so a
    too-tight marker shows up as ``worker 'gwN' crashed`` on a loaded host, never in CI.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = [
        f"{path.relative_to(root)}:{line} timeout({seconds:g})"
        for path in sorted((root / "tests").rglob("*.py"))
        for line, seconds in _timeout_marker_values(path.read_text(encoding="utf-8"))
        if seconds < _AUTOUSE_GIT_PROBE_BOUND_SECS
    ]

    assert not offenders, (
        f"these pytest timeout markers are below {_AUTOUSE_GIT_PROBE_BOUND_SECS:g}s, the bound "
        "of the autouse _no_repo_commits git probes (2 samples x GIT_PROBE_ATTEMPTS x "
        "GIT_PROBE_TIMEOUT_SECONDS in tests/_isolation.py) that the marker also covers. On a "
        f"loaded host they expire inside fixture setup and crash the worker: {offenders}"
    )


# The tortoise test runs 14 signer tests serially in one nested pytest: 82.07 s alone at load
# 35 on an 18-core host, and about 10x that in the 3 crashed make-verify runs (3 h 45 min to
# 4 h 51 min against the usual 25 min), so the marker must cover 82 s x 10.
_TORTOISE_NESTED_RUN_ALONE_SECS = 82.07
_LOADED_HOST_SLOWDOWN = 10.0


def test_tortoise_nested_signer_run_marker_covers_loaded_cost() -> None:
    """The nested signer run must carry its own budget (bug uneducated-conveyable-nabarlek).

    Under the 300 s ini budget it killed its xdist worker on loaded hosts with no traceback.
    """
    root = Path(__file__).resolve().parents[2]
    seconds = _timeout_marker_seconds(
        root / "tests" / "unit" / "test_opcert_key_copy_leak_tortoise.py",
        "test_signer_test_modules_leave_no_key_copy_in_the_temp_dir",
    )

    floor = _TORTOISE_NESTED_RUN_ALONE_SECS * _LOADED_HOST_SLOWDOWN
    assert seconds is not None, (
        "the tortoise nested signer run has no @pytest.mark.timeout(N), so the 300 s ini "
        f"budget applies; it needs at least {floor:g}s on a loaded host"
    )
    assert seconds >= floor
