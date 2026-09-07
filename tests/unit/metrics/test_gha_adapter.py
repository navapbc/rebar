"""Contract tests for the GitHub Actions metrics adapter.

Local fixture logs and run metadata exercise both parsers without network access.
``parse_coverage`` reads the pytest-cov total percentage. ``red_to_green_recovery`` returns the
interval from a failed run to the next passing run. Missing evidence returns ``None``.
"""

from __future__ import annotations

import pytest

from rebar.metrics.adapters.github_actions import parse_coverage, red_to_green_recovery

pytestmark = pytest.mark.unit


def test_parse_coverage_from_total_line():
    log = "\n".join(
        [
            "some pytest output",
            "Name                       Stmts   Miss  Cover",
            "----------------------------------------------",
            "TOTAL                       1000    158    84.2%",
            "trailing line",
        ]
    )
    assert parse_coverage(log) == 84.2


def test_red_to_green_recovery_interval():
    runs = [
        {"conclusion": "failure", "created_at": 1000, "head_sha": "a"},
        {"conclusion": "success", "created_at": 1600, "head_sha": "a"},
    ]
    # recovery = time from the failing run to the next passing run.
    assert red_to_green_recovery(runs) == 600
