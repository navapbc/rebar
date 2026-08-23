"""The per-test hang budget must sit ABOVE the band of legitimate test costs.

Bug ``soppy-logophilic-husky`` (3fa7-94ba-42aa-4623). Story ``bold-abeyant-indri`` lowered
the budget from 300 s to 20 s on the strength of a local unit-tier run. On the gating
``ubuntu-latest, py3.13`` leg — the only one that adds ``--cov=rebar`` — roughly fourteen
whole-tree scanning gates legitimately cost 18.7-29.9 s of ``call`` time, so a 20 s budget
lands INSIDE that band and expires tests that were going to pass.

That expiry is not survivable and does not announce itself. ``timeout_method = "thread"``
means pytest-timeout's watchdog calls ``os._exit(1)`` (``pytest_timeout.py:542``), killing
the xdist worker process outright; only the ``signal`` method reports ``Failed: Timeout``
(``pytest.fail`` at ``pytest_timeout.py:502``). So an over-tight budget does not surface as
a timeout verdict on the offending test — it surfaces as ``[gwN] node down: Not properly
terminated`` plus ``worker 'gwN' crashed``, eleven times a run, on a drifting set of
innocent tests, with zero ``Failed: Timeout`` lines to explain it.

``test_the_configured_budget_is_the_measured_one`` pins the budget to whatever value is
currently written down; it cannot tell a measured value from a wrong one. This module
holds the invariant that value has to satisfy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

pytestmark = pytest.mark.unit

# The slowest legitimate test BODY on the gating leg, measured under the CI command
# (`-n 4 --dist worksteal --cov=rebar`) on the last green Verified run before the budget was
# lowered — a full pass with zero worker crashes, under the then-current `--timeout=300`.
# Bug soppy-logophilic-husky records the run identifiers and the full durations table.
#
#     29.93s call  test_subprocess_env_repr_security.py::
#                       test_safe_boundary_is_not_unwrapped_in_repository_tests
#     28.72s call  test_env_registry_helper_coverage.py::
#                       test_dropping_a_used_helpers_row_fails_loudly_instead_of_shrinking
#     22.97s call  test_binding_lifecycle_grace_seam_clean_heldout.py::
#                       test_gate_reports_no_findings_whole_tree
#     ... eleven more between 18.7s and 22.7s ...
#
# These are `call` times: the body IS the whole-tree scan, so no fixture-phase accounting
# (the ini has no `timeout_func_only`; bug 797b-bbc4-01cf-42d5) can discount them. Raise
# this number only against a fresh measurement on the gating leg.
_MEASURED_SLOWEST_LEGITIMATE_CALL_SECS = 29.93

# Clearing the worst case by a whisker is not enough. Per-test cost on the runner moves with
# load, with coverage, and — once workers start dying — with the restarts themselves: in the
# failing runs (32583448507, 32585303586) the SURVIVING scanners were pushed from ~16 s to
# 19.0-19.95 s because eleven worker restarts re-paid process and coverage startup. A budget
# without real headroom converts that ordinary variance into a worker death.
_REQUIRED_HEADROOM = 2.0


def _ini() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["tool"]["pytest"]["ini_options"]


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
