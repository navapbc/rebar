"""The Verified gate's triage comment must stay observability-only (ticket be9d-2f2d-47ca-45d2).

A `Verified -1` used to read exactly `CANCELLED: <run URL>`, so a job that exceeded its
`timeout-minutes` and a job that failed on stale-base version skew were indistinguishable from
the vote — though only the second needs a rebase. The vote job now posts a companion comment
naming the jobs and their outcomes.

The danger in that change is scope creep: anything that shifts WHEN the gate votes, or that
lets the new steps influence the gate's outcome, would turn an observability fix into a
correctness change. These tests pin the boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "gerrit-verify.yaml"

# The jobs the Verified vote aggregates. Pinned so a future edit cannot quietly narrow the
# gate while adding to the comment.
EXPECTED_VOTE_NEEDS = {
    "clear-vote",
    "require-ticket",
    "build-and-test",
    "mutation",
    "optionality",
    "artifact-probe",
    "eval-discipline",
    "golden-path",
    "verify-identity",
}


@pytest.fixture(scope="module")
def vote_job() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["vote"]


@pytest.fixture(scope="module")
def steps(vote_job: dict[str, Any]) -> list[dict[str, Any]]:
    return vote_job["steps"]


def _step(steps: list[dict[str, Any]], needle: str) -> dict[str, Any]:
    for step in steps:
        if needle in str(step.get("name", "")) or needle in str(step.get("uses", "")):
            return step
    raise AssertionError(f"no step matching {needle!r}")


# --- vote semantics are untouched ----------------------------------------------------


def test_cast_step_still_votes_the_normalized_conclusion(steps: list[dict[str, Any]]) -> None:
    """The single source of the vote decision must remain the normalize step's output."""
    cast = _step(steps, "gerrit-review-action")
    assert cast["with"]["vote-type"] == "${{ steps.normalize.outputs.vote-type }}"


def test_vote_needs_are_unchanged(vote_job: dict[str, Any]) -> None:
    assert set(vote_job["needs"]) == EXPECTED_VOTE_NEEDS


def test_only_one_step_casts_a_vote(steps: list[dict[str, Any]]) -> None:
    """Exactly one gerrit-review-action invocation in `vote`, so no second label is cast."""
    casts = [s for s in steps if "gerrit-review-action" in str(s.get("uses", ""))]
    assert len(casts) == 1


# --- the triage comment is comment-only ----------------------------------------------


def test_the_comment_step_casts_no_label(steps: list[dict[str, Any]]) -> None:
    """A Gerrit review with no --label is a comment; adding one would change the gate."""
    post = _step(steps, "Post the triage detail")
    assert "--label" not in post["run"]
    assert "Verified=" not in post["run"]
    assert "Code-Review=" not in post["run"]


def test_the_comment_step_only_runs_on_a_non_success_vote(steps: list[dict[str, Any]]) -> None:
    post = _step(steps, "Post the triage detail")
    assert "steps.normalize.outputs.vote-type != 'success'" in post["if"]


def test_the_summary_step_only_runs_on_a_non_success_vote(steps: list[dict[str, Any]]) -> None:
    summary = _step(steps, "Summarize the jobs")
    assert "steps.normalize.outputs.vote-type != 'success'" in summary["if"]


def test_the_new_steps_cannot_fail_the_vote_job(steps: list[dict[str, Any]]) -> None:
    """Observability must never be able to redden a run that CI itself passed."""
    for name in ("Summarize the jobs", "Post the triage detail"):
        assert _step(steps, name)["continue-on-error"] is True


def test_the_comment_is_posted_after_the_vote_is_cast(steps: list[dict[str, Any]]) -> None:
    """Cast first: if the comment path breaks, the vote is already on the change."""
    names = [f"{s.get('name', '')} {s.get('uses', '')}" for s in steps]
    cast_index = next(i for i, n in enumerate(names) if "gerrit-review-action" in n)
    post_index = next(i for i, n in enumerate(names) if "Post the triage detail" in n)
    assert cast_index < post_index


# --- the trusted-script boundary ------------------------------------------------------


def test_the_summarizer_is_checked_out_from_the_default_branch(steps: list[dict[str, Any]]) -> None:
    """The vote job must never run patchset-controlled code; it sparse-checks out from main."""
    checkout = _step(steps, "Fetch the trusted conclusion-normalization script")
    assert "scripts/summarize_ci_failures.py" in checkout["with"]["sparse-checkout"]
    assert checkout["with"]["persist-credentials"] is False


def test_the_summarizer_script_exists() -> None:
    assert (REPO_ROOT / "scripts" / "summarize_ci_failures.py").is_file()


def test_the_workflow_can_read_annotations() -> None:
    """Telling a timeout from a cancellation needs the check-run annotations."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"]["checks"] == "read"
