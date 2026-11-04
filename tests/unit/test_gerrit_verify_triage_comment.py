"""Keep Verified failure triage comments observability-only (be9d).

A companion comment names failed jobs so timeout and stale-base skew can be
distinguished. Its steps must not delay the vote or alter the gate outcome."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "gerrit-verify.yaml"

# The jobs the Verified vote aggregates. Pinned so a future edit cannot quietly narrow the
# gate while adding to the comment. scanner-integration is now voting, so it must stay
# inside this set and inside the normalize route expression.
EXPECTED_VOTE_NEEDS = {
    "clear-vote",
    "require-ticket",
    "classify",
    "docs-only",
    "build-and-test",
    "mutation",
    "optionality",
    "artifact-probe",
    "eval-discipline",
    "golden-path",
    "verify-identity",
    "scanner-integration",
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


def test_scanner_integration_runs_full_route_and_votes(vote_job: dict[str, Any]) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    scanner = workflow["jobs"]["scanner-integration"]

    assert "scanner-integration" in vote_job["needs"]
    assert scanner["uses"] == "./.github/workflows/_scanner-integration.yml"
    assert scanner["needs"] == ["clear-vote", "require-ticket", "classify"]
    assert "route == 'full'" in scanner["if"]
    assert scanner["with"] == {
        "gerrit-refspec": "${{ inputs.GERRIT_REFSPEC }}",
        "gerrit-project": "${{ inputs.GERRIT_PROJECT }}",
        "gerrit-url": "https://${{ vars.GERRIT_SERVER }}",
    }


def test_vote_normalization_requires_the_voting_scanner_on_both_routes(
    steps: list[dict[str, Any]],
) -> None:
    normalize = _step(steps, "Normalize the conclusion")
    conclusion = normalize["env"]["CONCLUSION"]
    assert "needs.scanner-integration.result == 'success'" in conclusion
    assert "needs.scanner-integration.result == 'skipped'" in conclusion
    assert "env.WORKFLOW_CONCLUSION" not in conclusion
    assert "needs.build-and-test.result == 'success'" in conclusion
    assert "needs.docs-only.result == 'success'" in conclusion


def test_vote_normalization_preserves_cancelled_as_no_verdict(
    steps: list[dict[str, Any]],
) -> None:
    normalize = _step(steps, "Normalize the conclusion")
    conclusion = normalize["env"]["CONCLUSION"]
    assert "contains(needs.*.result, 'failure') && 'failure'" in conclusion
    assert "contains(needs.*.result, 'cancelled')" in conclusion
    assert "&& 'cancelled'" in conclusion
    assert conclusion.index("contains(needs.*.result, 'failure')") < conclusion.index(
        "contains(needs.*.result, 'cancelled')"
    )


def _run_normalize_step(
    steps: list[dict[str, Any]], tmp_path: Path, gh_script: str, conclusion: str = "failure"
) -> tuple[int, str]:
    """Run the normalize step's own shell with a stub ``gh`` and return (rc, vote-type)."""
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\n" + gh_script, encoding="utf-8")
    gh.chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    normalize = _step(steps, "Normalize the conclusion")
    # The step runs from a sparse checkout holding only the trusted script; mirror that.
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "normalize_ci_conclusion.py", tmp_path / "scripts")
    cp = subprocess.run(
        ["bash", "-c", normalize["run"]],
        cwd=tmp_path,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "CONCLUSION": conclusion,
            "FAILURE_OBSERVED": "false",
            "JOBS_FILE": str(tmp_path / "verify-jobs.json"),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": "navapbc/rebar",
            "GITHUB_RUN_ID": "1",
            "REBAR_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    votes = [ln for ln in output.read_text(encoding="utf-8").splitlines() if ln]
    return cp.returncode, votes[-1] if votes else ""


def test_normalize_fetches_the_jobs_list_itself(steps: list[dict[str, Any]]) -> None:
    """The jobs list is read inside the normalize step, not a separate workflow step."""
    normalize = _step(steps, "Normalize the conclusion")
    assert "actions/runs/${GITHUB_RUN_ID}/jobs" in normalize["run"]
    assert normalize["env"]["JOBS_FILE"] == "${{ runner.temp }}/verify-jobs.json"
    assert normalize["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert not any("Snapshot" in str(s.get("name", "")) for s in steps)


def test_a_failed_jobs_fetch_keeps_the_fail_closed_vote(
    steps: list[dict[str, Any]], tmp_path: Path
) -> None:
    rc, vote = _run_normalize_step(steps, tmp_path, "exit 1\n")
    assert rc == 0
    assert vote == "vote-type=failure"


def test_a_not_acquired_jobs_list_casts_no_verdict(
    steps: list[dict[str, Any]], tmp_path: Path
) -> None:
    fixture = (
        REPO_ROOT / "tests" / "fixtures" / "ci_jobs" / "run-41855161578-macos-not-acquired.json"
    )
    jobs = json.loads(fixture.read_text(encoding="utf-8"))["jobs"]
    listing = tmp_path / "jobs.ndjson"
    listing.write_text("\n".join(json.dumps(job) for job in jobs) + "\n", encoding="utf-8")
    rc, vote = _run_normalize_step(steps, tmp_path, f"cat '{listing}'\n")
    assert rc == 0
    assert vote == "vote-type=cancelled"


def test_a_success_conclusion_does_not_fetch_jobs(
    steps: list[dict[str, Any]], tmp_path: Path
) -> None:
    rc, vote = _run_normalize_step(steps, tmp_path, "echo called >&2; exit 1\n", "success")
    assert rc == 0
    assert vote == "vote-type=success"


def test_normalizer_exempts_the_vote_job_by_its_workflow_name(vote_job: dict[str, Any]) -> None:
    """The in-progress vote job has no conclusion yet; the script skips it by name."""
    script = REPO_ROOT / "scripts" / "normalize_ci_conclusion.py"
    spec = importlib.util.spec_from_file_location("normalize_ci_conclusion_vote_name", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert vote_job["name"] == module.VOTE_JOB_NAME


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
