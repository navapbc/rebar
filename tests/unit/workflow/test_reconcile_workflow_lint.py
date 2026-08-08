"""Workflow contract tests for the tickets-store ``merge=ours`` driver."""

from __future__ import annotations

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
