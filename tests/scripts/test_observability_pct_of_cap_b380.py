"""``pct_of_cap`` must be able to say 109 (bug ``b380-3dfc-99fc-4a0e``).

Five metrics are derived through ``observability.sh``'s ``pct_of_cap`` helper —
``docker_storage_used_percent``, ``docker_buildkit_cache_used_percent``,
``journal_used_percent``, ``var_tmp_used_percent`` and ``container_writable_used_percent`` —
and every one of them exists for exactly one purpose: to show whether a storage cap is holding.
The helper used to clamp its output to 100, which made each of them **structurally incapable of
reporting the one condition it is deployed to detect**: "exactly at the cap" and "over the cap
by any amount" published the same number.

That is not a hypothetical. On 2026-09-05 the production host's ``docker system df`` reported a
Build Cache of 5.875 GB against a ``builder.gc.maxUsedSpace`` of 5.00 GiB (5.368 GB) — about
**109%**, roughly half a gigabyte past budget with 104 retained entries. The published
``docker_buildkit_cache_used_percent`` read **100**, and the operator reading it took the pinned
ceiling as evidence the cap was working.

Clamping is defensible for a gauge whose semantics stop at full: a progress bar, a disk that
cannot physically exceed its own size. It is wrong for a BUDGET, where exceeding the number is
not an impossible state but the specific failure being watched for. ``builder.gc.maxUsedSpace``
is documented as a best-effort reclamation target, not a hard wall, so the cache can and does
sit above it.

The tests here drive the SHIPPED function definition, lifted out of ``observability.sh`` by
text, so a clamp re-introduced into the helper turns them RED without any AWS, Docker, systemd
or CI provider in the loop. The per-metric end-to-end proofs that each call site publishes the
unclamped value live beside their own metrics, in
``test_observability_docker_storage.py``, ``test_observability_journal.py``,
``test_observability_var_tmp.py`` and ``test_observability_container_layers.py``.

Supersedes story ``910b-2d43-4482-4c64`` (S5) AC3's "percent clamped to 100", which was written
before the live overshoot was measured.
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

# The production reading of 2026-09-05, in bytes: `docker system df` Build Cache against
# `builder.gc.maxUsedSpace`. 5.875 GB / 5.00 GiB is 109%, and the metric published 100.
LIVE_BUILDKIT_BYTES = 5_875_000_000
LIVE_BUILDKIT_CAP = 5 * GIB


def _function_source(name: str) -> str:
    """The shipped definition of ``name``, lifted from ``observability.sh`` by text.

    Sourcing the script itself is not an option — it is a straight-line probe that publishes on
    import — so the unit under test is extracted rather than imported. The extraction is pinned
    by :func:`test_the_helper_is_extractable_from_the_shipped_script`, so a refactor that moves
    or renames the helper fails loudly here instead of silently testing nothing.
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
    """Over the cap the number keeps rising: 150 and 300 are different incidents and must not
    publish the same datapoint. The 2 GiB + 1 case is the floor-division boundary — a value
    barely over the cap still floors to 100, which is arithmetic, not a clamp; the cases either
    side of it are what distinguish the two."""
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
    """A structural guard beside the behavioural ones: the shipped body must contain no upper
    bound on ``pct``. Behaviour tests catch a clamp at 100; this catches one re-introduced at any
    other ceiling (say 200) that the sampled values above would step straight over."""
    body = _function_source("pct_of_cap")
    assert "-gt" not in body, f"pct_of_cap has regrown an upper bound:\n{body}"
