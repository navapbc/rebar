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


def test_review_bot_lifespan_marker_covers_autouse_setup() -> None:
    """The local marker must not be tighter than cold autouse fixture setup.

    The marker wraps setup/call/teardown, not only the measured TestClient body. Under coverage
    a cold import in an autouse fixture can exceed a tiny marker; with timeout_method=thread
    pytest-timeout kills the whole xdist worker, surfacing as "worker gwN crashed" instead of a
    timeout failure on the real culprit.
    """
    root = Path(__file__).resolve().parents[2]
    seconds = _timeout_marker_seconds(
        root / "tests" / "unit" / "test_review_bot.py",
        "test_lifespan_is_safe_by_default_without_per_test_stubs",
    )

    assert seconds is not None
    assert seconds >= 30
