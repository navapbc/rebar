"""The CI Git-floor gate survives the definition-vs-tree skew, without going toothless.

Gerrit's ``Verified`` lane takes the workflow DEFINITION from the target branch but checks
out the CHANGE'S tree. A gate step that reads a repo file therefore runs against changes
whose base predates that file. ``.github/workflows/_build-and-test.yml`` states the house
rule for this in its "Definition-vs-tree skew guard (ticket ee2a)" note: a new gate step
that reads a repo artifact must skip — with a log line — when the artifact is absent from
the checked-out tree, and re-enforce on rebase and on every post-merge change. The
``Git version floor gate`` (ticket 980d-83ac-a6bb-4edb) did not copy that guard and
hard-failed every in-flight change (bug 7e90-233b-3ee9-4f14).

These tests EXECUTE the real step. They lift the gate's ``run:`` script straight out of the
workflow YAML and run it under ``bash`` against purpose-built trees, so they assert the
gate's behavior rather than its wording:

* a tree WITHOUT the floor file must pass, and must say why it skipped;
* a tree WITH the floor file must still enforce — an adequate Git passes, and an
  under-floor Git fails with the required version, the installed version and the upgrade
  path (980d's enforcement, preserved not weakened).

Both tree shapes are covered deliberately: the workflow-from-main / tree-from-patchset split
makes them genuinely different cases, and a fix verified on only one of them is unverified.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _subprocess_env import subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "_build-and-test.yml"
FLOOR_FILE_REL = ".github/git-version-floor.txt"
STEP_NAME = "Git version floor gate"


def _gate_script() -> str:
    """The `run:` body of the Git-floor gate step, lifted from the live workflow."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []) or []:
            if step.get("name") == STEP_NAME:
                return str(step["run"])
    raise AssertionError(
        f"{WORKFLOW.name} no longer defines a step named {STEP_NAME!r} — these tests pin "
        "that step's behavior; re-point them at its replacement rather than deleting them."
    )


def _run_gate(cwd: Path, *, git_version: str | None = None) -> subprocess.CompletedProcess[str]:
    """Execute the real gate script in `cwd`, optionally against a stubbed `git --version`."""
    env = None
    if git_version is not None:
        env = subprocess_env()
        env["PATH"] = f"{_stub_git(cwd / 'bin', git_version)}:{env['PATH']}"
    return subprocess.run(
        ["bash", "-c", _gate_script()],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def tree_without_floor_file(tmp_path: Path) -> Path:
    """A checked-out patchset tree whose base predates the floor file."""
    tree = tmp_path / "predates-the-gate"
    (tree / ".github").mkdir(parents=True)
    assert not (tree / FLOOR_FILE_REL).exists(), "fixture precondition: floor file absent"
    return tree


@pytest.fixture
def tree_with_floor_file(tmp_path: Path) -> Path:
    """A checked-out tree that carries the floor file (post-gate, or rebased)."""
    tree = tmp_path / "carries-the-floor"
    (tree / ".github").mkdir(parents=True)
    shutil.copy(REPO_ROOT / FLOOR_FILE_REL, tree / FLOOR_FILE_REL)
    assert (tree / FLOOR_FILE_REL).is_file(), "fixture precondition: floor file present"
    return tree


def _stub_git(directory: Path, version: str) -> Path:
    """A `git` shim on PATH that reports `version`, so the floor comparison is testable."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "git"
    shim.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then\n'
        f'  echo "git version {version}"\n  exit 0\nfi\nexit 0\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return directory


# ── the skew case: a tree that predates the gate ─────────────────────────────


def test_absent_floor_file_does_not_fail_the_build(tree_without_floor_file: Path) -> None:
    """A change branched before the gate landed must not be failed by the gate."""
    result = _run_gate(tree_without_floor_file)
    assert result.returncode == 0, (
        "the Git-floor gate failed a tree that does not carry the floor file — this is the "
        "definition-vs-tree skew the ee2a guard exists to prevent.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_absent_floor_file_explains_why_it_skipped(tree_without_floor_file: Path) -> None:
    """The skip must name the cause, not just the filename (bug 7e90 acceptance criterion)."""
    output = _run_gate(tree_without_floor_file).stdout.lower()
    assert "skipping" in output, f"the skip was silent; got: {output!r}"
    assert "predates" in output, (
        "the skip line does not name WHY it skipped (the checked-out tree predates the "
        f"gate); got: {output!r}"
    )


def test_absent_floor_file_emits_no_workflow_error(tree_without_floor_file: Path) -> None:
    """A skip must not annotate the run as an error — that is what misled change 1416."""
    result = _run_gate(tree_without_floor_file)
    assert "::error::" not in (result.stdout + result.stderr), (
        "the gate still emits a GitHub `::error::` annotation for an absent floor file; "
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ── the enforcing case: a tree that carries the floor file ───────────────────


def test_present_floor_file_passes_on_an_adequate_git(tree_with_floor_file: Path) -> None:
    """With the file present and a Git above the floor, the gate passes and reports both."""
    result = _run_gate(tree_with_floor_file, git_version="2.99.0")
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "declared floor" in result.stdout, (
        f"the gate no longer reports the floor it enforced; got: {result.stdout!r}"
    )


def test_present_floor_file_still_fails_an_under_floor_git(tree_with_floor_file: Path) -> None:
    """980d's enforcement is preserved: an under-floor Git still fails the build."""
    result = _run_gate(tree_with_floor_file, git_version="2.30.2")
    assert result.returncode != 0, (
        "an under-floor Git passed the gate — 980d's enforcement has been weakened.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_under_floor_diagnostic_names_version_and_upgrade_path(tree_with_floor_file: Path) -> None:
    """The under-floor failure must name required version, installed version and the fix."""
    result = _run_gate(tree_with_floor_file, git_version="2.30.2")
    output = result.stdout + result.stderr
    assert "2.30.2" in output, f"the diagnostic does not name the installed Git; got: {output!r}"
    assert "2.38" in output, f"the diagnostic does not name the required Git; got: {output!r}"
    assert "upgrade" in output.lower(), (
        f"the diagnostic does not name the upgrade path; got: {output!r}"
    )
