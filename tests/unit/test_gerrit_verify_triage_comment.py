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
    "classify",
    "docs-only",
    "build-and-test",
    "mutation",
    "scanner-integration",
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
    cast = _step(steps, "Cast Verified from the CI conclusion")
    assert cast["env"]["VOTE_TYPE"] == "${{ steps.normalize.outputs.vote-type }}"
    assert cast["run"].strip().endswith("python3 scripts/cast_gerrit_verified_vote.py")


def test_vote_needs_cover_every_verify_route(vote_job: dict[str, Any]) -> None:
    assert set(vote_job["needs"]) == EXPECTED_VOTE_NEEDS


def test_the_final_vote_uses_the_tolerant_local_helper(steps: list[dict[str, Any]]) -> None:
    """The final vote must be able to classify Gerrit's closed-change race."""
    casts = [s for s in steps if s.get("name") == "Cast Verified from the CI conclusion"]
    assert len(casts) == 1
    assert "gerrit-review-action" not in str(casts[0].get("uses", ""))
    assert "cast_gerrit_verified_vote.py" in casts[0]["run"]


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
    cast_index = next(i for i, n in enumerate(names) if "Cast Verified from the CI conclusion" in n)
    post_index = next(i for i, n in enumerate(names) if "Post the triage detail" in n)
    assert cast_index < post_index


# --- the ssh call has to actually reach Gerrit ----------------------------------------


def test_the_comment_step_uses_the_gerrit_ssh_port(steps: list[dict[str, Any]]) -> None:
    """Gerrit's ssh API is on 29418; a bare `ssh` would default to 22, which is closed."""
    post = _step(steps, "Post the triage detail")
    assert "-p 29418" in post["run"]


def test_the_comment_step_installs_the_key_and_known_hosts(steps: list[dict[str, Any]]) -> None:
    """gerrit-review-action does this internally; a raw ssh step must do it itself."""
    run = _step(steps, "Post the triage detail")["run"]
    assert "GERRIT_SSH_PRIVKEY" in str(_step(steps, "Post the triage detail")["env"])
    assert "chmod 600" in run
    assert "known_hosts" in run


def test_the_private_key_is_removed_afterwards(steps: list[dict[str, Any]]) -> None:
    cleanup = _step(steps, "Remove the Gerrit comment key")
    assert "rm -f" in cleanup["run"]
    assert cleanup["if"].strip().startswith("${{ always()")


def test_the_change_and_patchset_are_validated_before_the_remote_call(
    steps: list[dict[str, Any]],
) -> None:
    """Mirrors gerrit-review-action's own guard: only positive integers reach the command."""
    run = _step(steps, "Post the triage detail")["run"]
    assert "^[1-9][0-9]*$" in run


# --- annotations are actually fetched -------------------------------------------------


def test_the_summary_step_fetches_check_run_annotations(steps: list[dict[str, Any]]) -> None:
    """Without the annotations there is no way to tell a timeout from a cancellation."""
    run = _step(steps, "Summarize the jobs")["run"]
    assert "check-runs" in run
    assert "annotations" in run
    assert "ANNOTATIONS_JSON" in run


def test_the_summary_step_fetches_this_runs_jobs(steps: list[dict[str, Any]]) -> None:
    run = _step(steps, "Summarize the jobs")["run"]
    assert "actions/runs/${GITHUB_RUN_ID}/jobs" in run
    assert "JOBS_JSON" in run


# --- the trusted-script boundary ------------------------------------------------------


def test_the_summarizer_is_checked_out_from_the_default_branch(steps: list[dict[str, Any]]) -> None:
    """The vote job must never run patchset-controlled code; it sparse-checks out from main."""
    checkout = _step(steps, "Fetch the trusted conclusion-normalization script")
    assert "scripts/summarize_ci_failures.py" in checkout["with"]["sparse-checkout"]
    assert "scripts/cast_gerrit_verified_vote.py" in checkout["with"]["sparse-checkout"]
    assert checkout["with"]["persist-credentials"] is False


def test_the_summarizer_script_exists() -> None:
    assert (REPO_ROOT / "scripts" / "summarize_ci_failures.py").is_file()


def test_the_workflow_can_read_annotations() -> None:
    """Telling a timeout from a cancellation needs the check-run annotations."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"]["checks"] == "read"
