"""Config-as-artifact gate for the exact-patchset Jira DC workflow (AC1/AC2 of ticket
`3f27-cb3c-8023-4f57` / `single-vast-roan`).

The ticket's AC1 requires that CI "checks out and records the exact Gerrit patchset SHA
used for the ephemeral DC run" and that "an all-skipped or wrong-ref run fails." This test
parses the workflow file directly (never trusting a code comment) to assert those
properties actually hold, mirroring the config-as-artifact discipline used by
``test_jira_dc_capability_map_workflow.py`` for its sibling job.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "jira-dc-exact-patchset.yml"

_GERRIT_INPUTS = {
    "GERRIT_BRANCH",
    "GERRIT_CHANGE_ID",
    "GERRIT_CHANGE_NUMBER",
    "GERRIT_CHANGE_URL",
    "GERRIT_EVENT_TYPE",
    "GERRIT_PATCHSET_NUMBER",
    "GERRIT_PATCHSET_REVISION",
    "GERRIT_PROJECT",
    "GERRIT_REFSPEC",
}


def _load() -> dict:
    assert _WORKFLOW.exists(), f"expected workflow missing: {_WORKFLOW}"
    return yaml.safe_load(_WORKFLOW.read_text())


def _job(doc: dict) -> dict:
    jobs = doc["jobs"]
    assert len(jobs) == 1, f"expected exactly one job, found {sorted(jobs)}"
    return next(iter(jobs.values()))


def _checkout_step(doc: dict) -> dict:
    for step in _job(doc)["steps"]:
        if step.get("name") == "Checkout the exact Gerrit patchset SHA":
            return step
    raise AssertionError("workflow must own deterministic exact-SHA checkout")


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    _run(["git", "add", name], repo)
    _run(["git", "commit", "-m", f"add {name}"], repo)
    return _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()


def _patchset_remotes(tmp_path: Path) -> tuple[Path, Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _run(["git", "init"], source)
    _run(["git", "config", "user.email", "ci@example.com"], source)
    _run(["git", "config", "user.name", "CI"], source)
    patchset_1 = _make_commit(source, "one.txt", "patchset 1\n")
    patchset_2 = _make_commit(source, "two.txt", "patchset 2\n")

    origin = tmp_path / "origin.git"
    gerrit_root = tmp_path / "gerrit"
    gerrit = gerrit_root / "rebar.git"
    _run(["git", "init", "--bare", str(origin)], tmp_path)
    _run(["git", "init", "--bare", str(gerrit)], tmp_path)

    _run(["git", "push", str(origin), f"{patchset_1}:refs/changes/66/2466/1"], source)
    _run(["git", "push", str(origin), f"{patchset_2}:refs/changes/66/2466/2"], source)
    _run(["git", "push", str(gerrit), f"{patchset_1}:refs/changes/66/2466/1"], source)
    _run(["git", "push", str(gerrit), f"{patchset_2}:refs/changes/66/2466/2"], source)
    return origin, gerrit_root, patchset_1, patchset_2


def _run_checkout_script(
    tmp_path: Path,
    *,
    origin: Path,
    gerrit_root: Path,
    refspec: str,
    expected_sha: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = subprocess_env(
        {
            "GERRIT_PROJECT": "rebar.git",
            "GERRIT_REFSPEC": refspec,
            "GERRIT_PATCHSET_REVISION": expected_sha,
            "GERRIT_CHANGE_NUMBER": "2466",
            "GERRIT_CHANGE_URL": "https://gerrit.example/c/rebar/+/2466",
            "GERRIT_PATCHSET_NUMBER": "2",
            "GERRIT_URL": str(gerrit_root),
            "GITHUB_MIRROR_URL": str(origin),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "GITHUB_ENV": str(tmp_path / "github.env"),
        }
    )
    result = subprocess.run(
        ["bash", "-c", str(_checkout_step(_load())["run"])],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return result, workspace


def test_dispatch_only_with_full_gerrit_input_set() -> None:
    doc = _load()
    # See jira-dc-capability-map's own test for why both keys are checked: PyYAML parses
    # a bare `on:` as the boolean True under YAML 1.1's implicit-boolean-scalar rules.
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), f"'on:' block is not a mapping: {triggers!r}"
    assert set(triggers) == {"workflow_dispatch"}, (
        "exact-patchset DC lane must be dispatch-only (never push/PR/schedule), "
        f"got {sorted(triggers)}"
    )
    inputs = set(triggers["workflow_dispatch"]["inputs"])
    missing = _GERRIT_INPUTS - inputs
    assert not missing, f"missing required Gerrit dispatch input(s): {sorted(missing)}"


def test_uses_owned_exact_sha_checkout_not_refspec_first_action() -> None:
    doc = _load()
    checkout = _checkout_step(doc)
    assert checkout["shell"] == "bash"
    assert checkout["env"]["GERRIT_PATCHSET_REVISION"] == "${{ inputs.GERRIT_PATCHSET_REVISION }}"
    text = _WORKFLOW.read_text()
    assert "lfreleng-actions/checkout-gerrit-change-action@" not in text


def test_verifies_checked_out_sha_against_the_patchset_revision_input() -> None:
    """AC1: 'an all-skipped or wrong-ref run fails' — this is the wrong-ref half."""
    doc = _load()
    job = _job(doc)
    steps_text = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "git rev-parse HEAD" in steps_text, (
        "workflow must read the actually-checked-out HEAD SHA to compare it"
    )
    assert "GERRIT_PATCHSET_REVISION" in steps_text
    assert "exit 1" in steps_text, "a SHA mismatch must fail the job, not merely warn"


def test_checkout_script_leaves_head_at_expected_sha(tmp_path: Path) -> None:
    origin, gerrit_root, _patchset_1, patchset_2 = _patchset_remotes(tmp_path)
    result, workspace = _run_checkout_script(
        tmp_path,
        origin=origin,
        gerrit_root=gerrit_root,
        refspec="refs/changes/66/2466/2",
        expected_sha=patchset_2,
    )
    assert result.returncode == 0, result.stdout
    assert _run(["git", "rev-parse", "HEAD"], workspace).stdout.strip() == patchset_2
    assert f"verified_sha={patchset_2}" in (tmp_path / "github.env").read_text()


def test_checkout_script_falls_back_to_gerrit_when_mirror_lacks_refspec(
    tmp_path: Path,
) -> None:
    origin, gerrit_root, _patchset_1, patchset_2 = _patchset_remotes(tmp_path)
    _run(["git", "--git-dir", str(origin), "update-ref", "-d", "refs/changes/66/2466/2"], tmp_path)

    result, workspace = _run_checkout_script(
        tmp_path,
        origin=origin,
        gerrit_root=gerrit_root,
        refspec="refs/changes/66/2466/2",
        expected_sha=patchset_2,
    )

    assert result.returncode == 0, result.stdout
    assert _run(["git", "rev-parse", "HEAD"], workspace).stdout.strip() == patchset_2


def test_checkout_script_rejects_refspec_revision_mismatch_before_checkout(
    tmp_path: Path,
) -> None:
    origin, gerrit_root, patchset_1, patchset_2 = _patchset_remotes(tmp_path)
    result, workspace = _run_checkout_script(
        tmp_path,
        origin=origin,
        gerrit_root=gerrit_root,
        refspec="refs/changes/66/2466/1",
        expected_sha=patchset_2,
    )
    assert result.returncode != 0
    assert "dispatch inputs disagree" in result.stdout
    assert patchset_1 in result.stdout
    assert patchset_2 in result.stdout
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workspace,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        != 0
    )


def test_checkout_script_fails_clearly_when_refspec_cannot_be_fetched(
    tmp_path: Path,
) -> None:
    origin, gerrit_root, _patchset_1, patchset_2 = _patchset_remotes(tmp_path)
    result, workspace = _run_checkout_script(
        tmp_path,
        origin=origin,
        gerrit_root=gerrit_root,
        refspec="refs/changes/66/2466/99",
        expected_sha=patchset_2,
    )
    assert result.returncode != 0
    assert "failed to fetch GERRIT_REFSPEC" in result.stdout
    assert "refs/changes/66/2466/99" in result.stdout
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workspace,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        != 0
    )


def test_runs_the_live_harness_with_the_all_skip_canary_engaged() -> None:
    """AC1's 'all-skipped run fails' half: REBAR_RUN_EXTERNAL=1 arms
    tests/external/conftest.py's existing all-skip canary for this -m external run.
    """
    doc = _load()
    job = _job(doc)
    steps_text = yaml.dump(job["steps"])
    assert "tests/external/live_jira_dc" in steps_text
    assert "REBAR_RUN_EXTERNAL" in steps_text
    assert "-m external" in steps_text


def test_teardown_always_runs() -> None:
    doc = _load()
    job = _job(doc)
    teardown_steps = [
        step for step in job["steps"] if "docker compose down" in str(step.get("run", ""))
    ]
    assert teardown_steps, "expected a docker compose teardown step"
    for step in teardown_steps:
        assert step.get("if") == "always()", (
            "harness teardown must run even when the test step fails, to avoid leaking "
            "the ephemeral DC container"
        )


def test_native_amd64_runner() -> None:
    doc = _load()
    job = _job(doc)
    assert job["runs-on"] == "ubuntu-latest", (
        "Jira DC does not finish booting on an emulated arm64 host within an hour "
        "(measured; see tests/external/live_jira_dc/README.md) — this lane must use a "
        "native amd64 runner like every other Docker-backed Jira DC job in this repo"
    )
