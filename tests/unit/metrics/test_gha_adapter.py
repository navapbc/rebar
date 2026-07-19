"""Happy-path contract for the GitHub-Actions coverage/CI-health adapter (ticket 1f77).

Tier: unit (fixture log text + run-metadata; no network). Pins the core parse:
coverage % from a pytest-cov TOTAL line, and a red->green recovery interval from
two runs. Persistence path / isolation are held out.

Public surface (from ``rebar.metrics.adapters.github_actions``):
- ``parse_coverage(log_text: str) -> float | None``
- ``red_to_green_recovery(runs: list[dict]) -> int | None`` (seconds/units between a
  failing run and the next passing run)
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
