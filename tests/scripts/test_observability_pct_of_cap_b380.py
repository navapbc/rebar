"""Regression tests for unclamped ``pct_of_cap`` (bug ``b380-3dfc-99fc-4a0e``).

All five storage-budget percentages use this helper. Its former 100% clamp hid real overruns,
including a 5.875 GB BuildKit cache against 5 GiB (109%); budgets are best-effort targets, not
physical maxima.

Tests extract the shipped shell function because sourcing the straight-line probe publishes
metrics. Per-metric end-to-end coverage lives in the four observability modules. This
supersedes story ``910b-2d43-4482-4c64`` (S5) AC3.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"

GIB = 1024**3
GB = 1000**3

# Live 2026-09-05 reading: 5.875 GB / 5 GiB = 109%, previously published as 100.
LIVE_BUILDKIT_BYTES = 5_875_000_000
LIVE_BUILDKIT_CAP = 5 * GIB


def _function_source(name: str) -> str:
    """Extract the shipped helper without sourcing the publishing probe.

    The extraction test fails if a refactor moves or renames it.
    """
    text = SCRIPT.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(?:.*?\n)*?\}}$", text, re.MULTILINE)
    assert match is not None, f"{name} not found in {SCRIPT}"
    return match.group(0)


def _pct_of_cap(used: int, cap: int) -> str:
    body = _function_source("pct_of_cap")
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{body}\npct_of_cap {used} {cap}"],
        capture_output=True,
        text=True,
        env=subprocess_env(),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_the_helper_is_extractable_from_the_shipped_script() -> None:
    """Guards the extraction itself: a moved or renamed helper must fail here, not vanish."""
    source = _function_source("pct_of_cap")
    assert source.startswith("pct_of_cap() {")
    assert source.endswith("}")


def test_a_cap_overrun_publishes_the_true_ratio_not_a_ceiling() -> None:
    """The defect, at the live numbers. Clamped this returns 100 and the breach is invisible."""
    assert int(_pct_of_cap(LIVE_BUILDKIT_BYTES, LIVE_BUILDKIT_CAP)) == 109


@pytest.mark.parametrize(
    ("used", "cap", "expected"),
    [
        (3 * GIB, 2 * GIB, 150),
        (6 * GIB, 2 * GIB, 300),
        (2 * GIB + 1, 2 * GIB, 100),  # barely over: floor division, but never CLAMPED
        (20 * GIB, 2 * GIB, 1000),
    ],
)
def test_every_overrun_reports_its_own_magnitude(used: int, cap: int, expected: int) -> None:
    """Overruns retain magnitude; the just-over-cap case floors to 100 without clamping."""
    assert int(_pct_of_cap(used, cap)) == expected


@pytest.mark.parametrize(
    ("used", "cap", "expected"),
    [
        (0, 4 * GIB, 0),
        (GIB, 4 * GIB, 25),
        (2 * GIB, 4 * GIB, 50),
        (4 * GIB, 4 * GIB, 100),  # exactly at the cap: still 100, no off-by-one introduced
    ],
)
def test_at_or_under_the_cap_is_unchanged(used: int, cap: int, expected: int) -> None:
    assert int(_pct_of_cap(used, cap)) == expected


def test_the_helper_carries_no_clamp() -> None:
    """Guard against ``-gt`` upper bounds that sampled behavior might miss."""
    body = _function_source("pct_of_cap")
    assert "-gt" not in body, f"pct_of_cap has regrown an upper bound:\n{body}"
