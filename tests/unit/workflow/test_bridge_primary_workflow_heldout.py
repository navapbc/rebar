"""Held-out production-workflow oracle for the additive bridge vocabulary migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _ROOT / ".github" / "workflows" / "reconcile-bridge.yml"


def _run_step() -> str:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    matches = [
        step["run"]
        for step in workflow["jobs"]["reconcile"]["steps"]
        if step.get("name") == "Run reconciler"
    ]
    assert len(matches) == 1
    return matches[0]


def test_workflow_delegates_to_the_shared_runner() -> None:
    """The provider wrapper retains one stable entrypoint for every mode."""
    assert _run_step() == "rebar bridge run"


def test_current_operator_docs_lead_with_bridge_and_remove_legacy_cli_mapping() -> None:
    """Primary instructions expose bridge operations after the legacy CLI is removed."""
    workflow_step = _run_step()
    assert workflow_step == "rebar bridge run"

    combined = "\n".join(
        (_ROOT / path).read_text(encoding="utf-8")
        for path in (
            "docs/user-guide.md",
            "docs/cli-reference.md",
            "docs/jira-sync-setup.md",
            "docs/release-notes.md",
        )
    )

    assert "rebar bridge preview" in combined
    assert "rebar bridge sync" in combined
    normalized = " ".join(combined.split())
    assert "The legacy top-level `rebar reconcile` adapter is removed" in combined
    assert "direct `--mode reconcile-check`" in combined
    assert "profile spelling `reconcile-check`" in normalized
