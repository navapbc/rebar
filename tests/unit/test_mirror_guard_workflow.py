"""Workflow-level contract for Mirror Guard run-state surfacing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mirror-guard.yml"


def _replication_step_run() -> str:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["guard"]["steps"]
    matches = [step["run"] for step in steps if step.get("name", "").startswith("mirror-guard")]
    assert len(matches) == 1
    return matches[0]


def test_workflow_surfaces_unreachable_distinct_from_divergence() -> None:
    run = _replication_step_run()

    assert "fetch-depth: 0" in _WORKFLOW.read_text(encoding="utf-8")
    assert "--merged-reachability" in run
    assert 'case "$code" in' in run
    assert "Mirror Guard unreachable" in run
    assert "not a divergence" in run
    assert "exit 2" in run
    assert "Mirror Guard invariant failure" in run
    assert "exit 1" in run
