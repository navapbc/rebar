"""CI gate steps survive the definition-vs-tree skew, without going toothless.

Gerrit's ``Verified`` lane takes the workflow DEFINITION from the target branch but checks
out the CHANGE'S tree. A gate step that runs a repo script therefore runs against changes
whose base predates that script, and dies with ``can't open file ... [Errno 2]`` — a hard CI
failure indistinguishable from a real gate violation.
``.github/workflows/_build-and-test.yml`` states the house rule in its "Definition-vs-tree
skew guard (ticket ee2a)" note: a gate step that runs a repo script must skip — with a log
line — when the script is absent from the checked-out tree, and re-enforce on rebase and on
every post-merge change. Bug 7e90-233b-3ee9-4f14 fixed the ``Git version floor gate`` that
way; bug 1909-c1a7-9f20-440f fixes the remaining unguarded steps.

Two kinds of test live here, and they do different jobs:

* a STRUCTURAL guard, which parses the patchset-lane workflows and FAILS on any step that
  runs a repo script without an if-present guard — so the ee2a convention stops depending on
  an author remembering the note, and the twelfth instance cannot be introduced silently;
* BEHAVIORAL tests, which EXECUTE each guarded step's real ``run:`` body, lifted from the
  live workflow YAML, against purpose-built trees. They assert behavior rather than wording:
  a tree WITHOUT the script must pass and say why it skipped, and a tree WITH the script must
  still invoke it and still propagate its failure (enforcement preserved, not weakened).

Both tree shapes are covered deliberately: the workflow-from-main / tree-from-patchset split
makes them genuinely different cases, and a guard verified on only one of them is unverified.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DOCS_ACTION_REF = "./.github/actions/docs-gates"
DOCS_ACTION = REPO_ROOT / ".github" / "actions" / "docs-gates" / "action.yml"

# The two workflows that run against a CHECKED-OUT GERRIT PATCHSET while their definition
# comes from the target branch — the exact condition that produces the skew. Other workflows
# (release, canary, scheduled probes) run their own ref's tree and are not exposed.
#
# `test.yml`'s `golden-path` job is the deliberate near-miss: it runs the SAME two scripts as
# gerrit-verify.yaml's golden-path, unguarded, and that is correct. It checks out branch head
# (plain `actions/checkout`, no Gerrit refspec), so its definition and its tree come from one
# ref and no skew is possible. The asymmetry is stated here so a reader diffing the two
# near-identical golden-path jobs does not have to re-derive which one is safe.
PATCHSET_LANE_WORKFLOWS = ("_build-and-test.yml", "gerrit-verify.yaml")

# The action that checks out the CHANGE'S tree. A job that uses it is in the patchset lane.
# Selecting on it — rather than on a workflow-wide list — is what keeps the structural guard
# from flagging branch-head jobs, and what excludes the vote job's `normalize_ci_conclusion`
# step, which sparse-checks-out its script from the TRUSTED default branch, not the patchset.
GERRIT_CHECKOUT_ACTION = "checkout-gerrit-change-action"

# Anchored at a path boundary so `tests/scripts/foo.py` is not misread as the gate
# script `scripts/foo.py`. Without the lookbehind a step running a TEST file under
# tests/scripts/ is swept in as a gate script, and then no if-present guard can
# satisfy the check — the guard would be looking for a path that does not exist.
_SCRIPT_RE = re.compile(r"(?<![A-Za-z0-9_./-])scripts/[A-Za-z0-9_./-]+\.py")


class GateStep(NamedTuple):
    """A patchset-lane step that runs one or more repo scripts."""

    workflow: str
    job: str
    name: str
    run: str
    shell: str | None
    scripts: tuple[str, ...]

    def __str__(self) -> str:  # pragma: no cover - identifies the case in pytest output
        return f"{self.workflow}::{self.job}::{self.name}"


def _job_is_in_patchset_lane(job: dict[str, Any]) -> bool:
    """True when the job checks out the Gerrit change's tree rather than its own ref."""
    for step in job.get("steps", []) or []:
        if GERRIT_CHECKOUT_ACTION in str(step.get("uses", "")):
            return True
    return False


def _collect_gate_steps() -> list[GateStep]:
    """Every patchset-lane step that runs a repo script, across both exposed workflows."""
    steps: list[GateStep] = []
    for workflow_name in PATCHSET_LANE_WORKFLOWS:
        workflow = yaml.safe_load((WORKFLOW_DIR / workflow_name).read_text(encoding="utf-8"))
        for job_name, job in workflow["jobs"].items():
            if not _job_is_in_patchset_lane(job):
                continue
            expanded_steps: list[dict[str, Any]] = []
            for step in job.get("steps", []) or []:
                expanded_steps.append(step)
                if step.get("uses") == DOCS_ACTION_REF:
                    action = yaml.safe_load(DOCS_ACTION.read_text(encoding="utf-8"))
                    for nested in action["runs"]["steps"]:
                        expanded_steps.append(
                            {
                                **nested,
                                "name": f"{step.get('name', 'docs-gates')} / {nested.get('name')}",
                            }
                        )
            for step in expanded_steps:
                run = step.get("run")
                if not run:
                    continue
                # The route classifier is sparse-checked out from trusted main into this
                # prefix after the patchset checkout; it is not a patchset-tree script and
                # therefore has no definition-vs-tree skew.
                if ".trusted-verify-router/scripts/" in str(run):
                    continue
                scripts = tuple(dict.fromkeys(_SCRIPT_RE.findall(str(run))))
                if not scripts:
                    continue
                steps.append(
                    GateStep(
                        workflow=workflow_name,
                        job=str(job_name),
                        name=str(step.get("name", "<unnamed>")),
                        run=str(run),
                        shell=step.get("shell"),
                        scripts=scripts,
                    )
                )
    return steps


GATE_STEPS = _collect_gate_steps()


def _guards(run: str, script: str) -> bool:
    """True when `run` tests `script` for presence before relying on it."""
    return f"-f {script}" in run


# ── the structural guard: the convention stops depending on memory ───────────


def test_the_sweep_finds_the_patchset_lane_steps() -> None:
    """Guard the guard: a detector that silently matches nothing would pass vacuously."""
    assert len(GATE_STEPS) >= 11, (
        "the patchset-lane sweep found "
        f"{len(GATE_STEPS)} script-running steps, which is fewer than the 11 known at the "
        "time of bug 1909-c1a7-9f20-440f — the detector (or the workflow structure) has "
        "changed and the structural guard below may be passing vacuously."
    )
    assert {s.workflow for s in GATE_STEPS} == set(PATCHSET_LANE_WORKFLOWS), (
        "the sweep no longer covers both patchset-lane workflows; "
        f"got {sorted({s.workflow for s in GATE_STEPS})}"
    )


@pytest.mark.parametrize("step", GATE_STEPS, ids=str)
def test_patchset_lane_step_guards_every_repo_script_it_runs(step: GateStep) -> None:
    """No step in the Verified lane may run a repo script it has not tested for presence.

    This is the structural half of bug 1909-c1a7-9f20-440f: the ee2a note asks authors to
    copy an if-present guard, and eleven steps did not. A convention enforced only by a
    comment is a convention that decays, so it is enforced here instead.
    """
    unguarded = [script for script in step.scripts if not _guards(step.run, script)]
    assert not unguarded, (
        f"step {step} runs {', '.join(unguarded)} without an if-present guard.\n\n"
        "Gerrit's Verified lane takes the workflow DEFINITION from the target branch but "
        "checks out the CHANGE'S tree, so this step will hard-fail (exit 2, `can't open "
        "file`) on every open change whose base predates that script — a false Verified -1 "
        "indistinguishable from a real gate violation (bugs 7e90-233b-3ee9-4f14, "
        "1909-c1a7-9f20-440f).\n\n"
        "Copy the ee2a if-present guard used by the `raw-git-write gate` step: test the "
        "script with `[ ! -f <script> ]` first and skip with a log line naming the cause, "
        "leaving the enforcement below unchanged."
    )


# ── the behavioral half: both tree shapes, per guarded step ──────────────────


def _bash_argv(step: GateStep) -> list[str]:
    """The shell GitHub Actions would run this step under.

    `shell: bash` is `bash --noprofile --norc -eo pipefail {0}`; a bare `run:` on Linux is
    `bash -e {0}`. Reproducing the `-e` matters: it is what makes a failing gate script
    fail the step.
    """
    if step.shell == "bash":
        return ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step.run]
    return ["bash", "-e", "-c", step.run]


def _run_step(step: GateStep, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Execute the step's real `run:` body in `cwd`."""
    return subprocess.run(_bash_argv(step), cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def tree_without_scripts(tmp_path: Path) -> Path:
    """A checked-out patchset tree whose base predates the gate scripts."""
    tree = tmp_path / "predates-the-gate"
    tree.mkdir()
    assert not (tree / "scripts").exists(), "fixture precondition: no scripts/ directory"
    return tree


@pytest.mark.parametrize("step", GATE_STEPS, ids=str)
def test_absent_script_does_not_fail_the_build(step: GateStep, tree_without_scripts: Path) -> None:
    """A change branched before the script landed must not be failed by the step."""
    result = _run_step(step, tree_without_scripts)
    assert result.returncode == 0, (
        f"step {step} failed a tree that does not carry {', '.join(step.scripts)} — this is "
        "the definition-vs-tree skew the ee2a guard exists to prevent.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.parametrize("step", GATE_STEPS, ids=str)
def test_absent_script_skip_is_explained_and_not_an_error(
    step: GateStep, tree_without_scripts: Path
) -> None:
    """The skip must announce itself and must not annotate the run as an error."""
    result = _run_step(step, tree_without_scripts)
    output = result.stdout + result.stderr
    assert "skipping" in output.lower(), (
        f"step {step} skipped silently; a silent skip reads as a passing gate. got: {output!r}"
    )
    assert "::error::" not in output, (
        f"step {step} still emits a GitHub `::error::` annotation for an absent script, so "
        f"the run is annotated as failing. got: {output!r}"
    )


@pytest.mark.parametrize("step", GATE_STEPS, ids=str)
def test_present_script_is_invoked_and_its_failure_still_fails_the_build(
    step: GateStep, tmp_path: Path
) -> None:
    """With the script present the gate is untouched: it runs, and a violation still fails.

    This is the non-weakening half of the acceptance criteria. Each script is stubbed to
    record that it ran and then exit non-zero, standing in for a genuine gate violation.
    """
    tree = tmp_path / f"carries-the-scripts-{abs(hash(str(step)))}"
    (tree / "scripts").mkdir(parents=True)
    for script in step.scripts:
        marker = tree / f"{Path(script).stem}.ran"
        target = tree / script
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text('ran')\nsys.exit(1)\n",
            encoding="utf-8",
        )

    result = _run_step(step, tree)

    first = Path(step.scripts[0])
    assert (tree / f"{first.stem}.ran").is_file(), (
        f"step {step} did not invoke {step.scripts[0]} even though it was present in the "
        "tree — the if-present guard is skipping a gate it should be enforcing.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.returncode != 0, (
        f"step {step} passed despite {step.scripts[0]} exiting non-zero — the gate has been "
        f"weakened.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
