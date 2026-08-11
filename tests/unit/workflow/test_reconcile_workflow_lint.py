"""Portable reconcile-bridge provider and runner contract tests."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_GITHUB = _ROOT / ".github" / "workflows" / "reconcile-bridge.yml"
_CANARY = _ROOT / ".github" / "workflows" / "reconcile-bridge-canary.yml"
_JENKINS = _ROOT / "Jenkinsfile"
_GITLAB = _ROOT / ".gitlab-ci.yml"
_GITLAB_SCHEMA = _ROOT / ".github" / "schemas" / "gitlab-ci.schema.json"
_GITLAB_PROVENANCE = _ROOT / ".github" / "schemas" / "gitlab-ci.schema.provenance.json"


def _workflow(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _steps(path: Path, job: str) -> list[dict]:
    return _workflow(path)["jobs"][job]["steps"]


def _commands(step: dict) -> list[str]:
    return [
        line.strip()
        for line in str(step.get("run", "")).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _step_index(steps: list[dict], command: str) -> int:
    matches = [index for index, step in enumerate(steps) if command in _commands(step)]
    assert len(matches) == 1, f"expected one workflow step running {command!r}, found {matches}"
    return matches[0]


def test_reconcile_workflows_provision_the_ours_driver_before_delivery() -> None:
    """The canary and primary adapters retain their proven merge-driver ordering."""
    canary = _steps(_CANARY, "canary")
    canary_mount = _step_index(
        canary, "git worktree add -B tickets .tickets-tracker origin/tickets"
    )
    canary_init = _step_index(canary, "rebar init")
    canary_push = next(
        index
        for index, step in enumerate(canary)
        if any("python -m rebar._store.push" in line for line in _commands(step))
    )
    assert canary_mount < canary_init < canary_push
    assert not any(
        "git config merge.ours.driver" in line for step in canary for line in _commands(step)
    )

    production = _steps(_GITHUB, "reconcile")
    mount = _step_index(production, "git worktree add -B tickets .tickets-tracker origin/tickets")
    configure = _step_index(production, "git config merge.ours.driver true")
    runner = _step_index(production, "rebar bridge run")
    assert mount < configure < runner


def test_github_wrapper_delegates_once_without_reimplementing_runner_policy() -> None:
    steps = _steps(_GITHUB, "reconcile")
    matches = [step for step in steps if step.get("name") == "Run reconciler"]
    assert len(matches) == 1
    assert matches[0]["id"] == "reconcile"
    assert matches[0]["run"] == "rebar bridge run"
    workflow_text = _GITHUB.read_text(encoding="utf-8")
    assert 'case "$MODE"' not in workflow_text
    assert "python -m rebar._store.push commit-and-push" not in workflow_text


def test_gitlab_workflow_validates_offline_against_pinned_schema() -> None:
    schema = json.loads(_GITLAB_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    errors = sorted(
        Draft7Validator(schema).iter_errors(_workflow(_GITLAB)),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    assert not errors, "\n".join(error.message for error in errors)

    provenance = json.loads(_GITLAB_PROVENANCE.read_text(encoding="utf-8"))
    digest = hashlib.sha256(_GITLAB_SCHEMA.read_bytes()).hexdigest()
    assert provenance["revision"] == "776c20ad5675eecea0c2010433a94d98d745e921"
    assert provenance["dialect"] == "http://json-schema.org/draft-07/schema#"
    assert provenance["sha256"] == digest


def _github_shell() -> str:
    step = next(
        step for step in _steps(_GITHUB, "reconcile") if step.get("name") == "Run reconciler"
    )
    return str(step["run"])


def _jenkins_shell() -> str:
    blocks = re.findall(r"sh\s+'''\n(.*?)\n\s*'''", _JENKINS.read_text(), re.DOTALL)
    assert blocks
    return "\n".join(textwrap.dedent(block) for block in blocks)


def _gitlab_shell() -> str:
    job = _workflow(_GITLAB)["reconcile_bridge"]
    blocks = [*job.get("before_script", []), *job.get("script", [])]
    assert blocks
    return "\n".join(str(block) for block in blocks)


@pytest.mark.parametrize(
    ("provider", "source"),
    [("github", _github_shell), ("jenkins", _jenkins_shell), ("gitlab", _gitlab_shell)],
)
def test_provider_shell_bodies_pass_pinned_shellcheck(
    tmp_path: Path, provider: str, source: object
) -> None:
    version = subprocess.run(
        ["shellcheck", "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert re.search(r"^version: 0\.11\.0$", version, re.MULTILINE)

    script = tmp_path / f"{provider}.sh"
    body = source()  # type: ignore[operator]
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n", encoding="utf-8")
    completed = subprocess.run(
        ["shellcheck", "--shell=bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
