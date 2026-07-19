"""Contract for scripts/normalize_ci_conclusion.sh (ticket bad2).

Tier: scripts (drives the shell script via subprocess).

The Verified vote job aggregates this run's jobs with im-open/workflow-conclusion,
which sets WORKFLOW_CONCLUSION to one of {success, failure, cancelled, skipped} —
`skipped` being its fallback when none of success/failure/cancelled is observed.
That value used to be piped VERBATIM into lfreleng-actions/gerrit-review-action's
`vote-type`, whose domain is ONLY {clear, success, failure, cancelled}. On `skipped`
the action hit its default branch (`::error::Unknown vote-type ...; exit 1`) BEFORE
posting any label — so a green CI change got NO Verified vote and was un-landable.

This script normalizes WORKFLOW_CONCLUSION into the action's vote-type domain, given
the raw conclusion plus a signal of whether any needed job actually failed/cancelled:
  success              -> success
  failure              -> failure
  cancelled            -> cancelled
  skipped (no failure) -> success   (benign fallback: nothing failed)
  skipped (failure obs)-> failure   (fail-closed: a real failure was observed)
  empty / unknown      -> failure   (fail-closed anomaly; never out-of-domain)

The KEY invariant: the emitted vote-type is ALWAYS in {success, failure, cancelled}
— `skipped` (or anything else) must NEVER reach the action verbatim.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "normalize_ci_conclusion.sh"

_VALID_VOTE_TYPES = {"success", "failure", "cancelled"}


def _run(conclusion: str, failure_observed: str = "false") -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_SCRIPT)],
        env={
            "CONCLUSION": conclusion,
            "FAILURE_OBSERVED": failure_observed,
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
    )


def _vote(conclusion: str, failure_observed: str = "false") -> str:
    cp = _run(conclusion, failure_observed)
    assert cp.returncode == 0, f"script failed: rc={cp.returncode} stderr={cp.stderr!r}"
    return cp.stdout.strip()


def test_script_exists_and_is_executable() -> None:
    assert _SCRIPT.exists(), f"missing normalization script: {_SCRIPT}"


@pytest.mark.parametrize(
    ("conclusion", "failure_observed", "expected"),
    [
        ("success", "false", "success"),
        ("failure", "false", "failure"),
        ("cancelled", "false", "cancelled"),
        # `skipped` with nothing failed is the benign im-open fallback -> success.
        ("skipped", "false", "success"),
        # `skipped` while a needed job failed/cancelled -> fail-closed to failure.
        ("skipped", "true", "failure"),
        # An empty conclusion is an anomaly -> fail-closed to failure.
        ("", "false", "failure"),
    ],
)
def test_normalization_mapping(conclusion: str, failure_observed: str, expected: str) -> None:
    assert _vote(conclusion, failure_observed) == expected


def test_key_invariant_skipped_never_out_of_domain() -> None:
    # The regression that stranded green CI: `skipped` reaching vote-type verbatim.
    for fo in ("false", "true"):
        vote = _vote("skipped", fo)
        assert vote in _VALID_VOTE_TYPES, f"skipped -> out-of-domain vote-type {vote!r}"


def test_all_inputs_stay_in_domain() -> None:
    for conclusion in ("success", "failure", "cancelled", "skipped", "", "weird-value"):
        for fo in ("false", "true"):
            vote = _vote(conclusion, fo)
            assert vote in _VALID_VOTE_TYPES, (
                f"conclusion={conclusion!r} failure_observed={fo!r} "
                f"-> out-of-domain vote-type {vote!r}"
            )
