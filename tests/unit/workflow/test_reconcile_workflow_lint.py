"""Workflow contract tests for the tickets-store ``merge=ours`` driver."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PRODUCTION = _REPO_ROOT / ".github" / "workflows" / "reconcile-bridge.yml"
_CANARY = _REPO_ROOT / ".github" / "workflows" / "reconcile-bridge-canary.yml"


def _steps(path: Path, job: str) -> list[dict]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    return workflow["jobs"][job]["steps"]


def _commands(step: dict) -> list[str]:
    return [
        line.strip()
        for line in step.get("run", "").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _step_index(steps: list[dict], command: str) -> int:
    matches = [index for index, step in enumerate(steps) if command in _commands(step)]
    assert len(matches) == 1, f"expected one workflow step running {command!r}, found {matches}"
    return matches[0]


def _core_push_index(steps: list[dict]) -> int:
    matches = [
        index
        for index, step in enumerate(steps)
        if any("python -m rebar._store.push" in line for line in _commands(step))
    ]
    assert len(matches) == 1, "workflow must delegate once to the core push entrypoint"
    return matches[0]


def _named_step(path: Path, job: str, name: str) -> dict:
    matches = [step for step in _steps(path, job) if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _run_reconciler_step(
    tmp_path: Path, *, stderr: str = "", stdout: str = "", rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "rebar"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s' \"${STUB_STDOUT:-}\"\n"
        "printf '%s' \"${STUB_STDERR:-}\" >&2\n"
        'exit "${STUB_RC:-0}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    github_output = tmp_path / "github-output"
    github_output.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "MODE": "live",
            "GITHUB_OUTPUT": str(github_output),
            "STUB_STDOUT": stdout,
            "STUB_STDERR": stderr,
            "STUB_RC": str(rc),
        }
    )
    completed = subprocess.run(
        ["bash", "-c", _named_step(_PRODUCTION, "reconcile", "Run reconciler")["run"]],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, github_output.read_text(encoding="utf-8")


def test_reconcile_workflows_provision_the_ours_driver_once() -> None:
    """Each workflow provisions the driver through its intended production path."""
    canary = _steps(_CANARY, "canary")
    canary_mount = _step_index(
        canary, "git worktree add -B tickets .tickets-tracker origin/tickets"
    )
    canary_init = _step_index(canary, "rebar init")
    assert canary_mount < canary_init < _core_push_index(canary)
    assert not any(
        "git config merge.ours.driver" in line for step in canary for line in _commands(step)
    ), "the canary already configures the driver through rebar init"

    production = _steps(_PRODUCTION, "reconcile")
    production_mount = _step_index(
        production, "git worktree add -B tickets .tickets-tracker origin/tickets"
    )
    production_config = _step_index(production, "git config merge.ours.driver true")
    assert production_mount < production_config < _core_push_index(production)


def test_paused_marker_sets_output_and_notice_and_skips_commit_step(tmp_path: Path) -> None:
    marker = (
        'BRIDGE_PAUSED: {"paused":true,"reason":"database cutover",'
        '"who":"operator@example.com","paused_at":"2026-08-08T17:00:00Z"}\n'
    )
    completed, output = _run_reconciler_step(tmp_path, stderr=marker)

    assert completed.returncode == 0
    assert output == "paused=true\n"
    assert "::notice::Reconcile bridge is paused" in completed.stdout
    assert "Reconcile converged." not in completed.stdout
    commit = _named_step(
        _PRODUCTION, "reconcile", "Commit reconciler events back and push to origin/tickets"
    )
    assert commit.get("if") == "steps.reconcile.outputs.paused != 'true'"


@pytest.mark.parametrize(
    "marker",
    [
        (
            'prefix BRIDGE_PAUSED: {"paused":true,"reason":"r","who":"w",'
            '"paused_at":"2026-08-08T17:00:00Z"}\n'
        ),
        "BRIDGE_PAUSED: {not-json}\n",
        'BRIDGE_PAUSED: {"paused":true,"reason":"r","who":"w"}\n',
        (
            'BRIDGE_PAUSED: {"paused":false,"reason":"r","who":"w",'
            '"paused_at":"2026-08-08T17:00:00Z"}\n'
        ),
        (
            'BRIDGE_PAUSED: {"paused":true,"reason":"r","who":"w",'
            '"paused_at":"2026-08-08T17:00:00Z"}\n'
            'BRIDGE_PAUSED: {"paused":true,"reason":"r","who":"w",'
            '"paused_at":"2026-08-08T17:00:00Z"}\n'
        ),
    ],
)
def test_unanchored_malformed_incomplete_or_false_markers_are_ordinary_success(
    tmp_path: Path, marker: str
) -> None:
    completed, output = _run_reconciler_step(tmp_path, stderr=marker)

    assert completed.returncode == 0
    assert output == ""
    assert "Reconcile converged." in completed.stdout
    assert "Reconcile bridge is paused" not in completed.stdout


def test_nonzero_marker_never_masks_failure_or_sets_paused_output(tmp_path: Path) -> None:
    marker = (
        'BRIDGE_PAUSED: {"paused":true,"reason":"database cutover",'
        '"who":"operator@example.com","paused_at":"2026-08-08T17:00:00Z"}\n'
    )
    completed, output = _run_reconciler_step(tmp_path, stderr=marker, rc=1)

    assert completed.returncode == 1
    assert output == ""
    assert "::error::Reconcile failed (exit 1)" in completed.stdout


@pytest.mark.parametrize(
    ("rc", "expected_rc", "message"),
    [
        (0, 0, "Reconcile converged."),
        (1, 1, "Reconcile failed (exit 1)"),
        (2, 2, "Reconcile failed (exit 2)"),
    ],
)
def test_workflow_uses_only_canonical_exit_contract(
    tmp_path: Path, rc: int, expected_rc: int, message: str
) -> None:
    run_step = _named_step(_PRODUCTION, "reconcile", "Run reconciler")["run"]
    assert 'case "$rc" in' not in run_step

    completed, output = _run_reconciler_step(tmp_path, rc=rc)

    assert completed.returncode == expected_rc
    assert output == ""
    assert message in completed.stdout
