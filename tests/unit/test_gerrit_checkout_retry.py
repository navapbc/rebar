"""Gerrit patchset checkout retries transient transport faults in-job."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
GERRIT_CHECKOUT_ACTION = "lfreleng-actions/checkout-gerrit-change-action@"
EXPECTED_WORKFLOWS = {
    "_artifact-probe.yml",
    "_build-and-test.yml",
    "_eval-discipline.yml",
    "_mutation.yml",
    "_optionality.yml",
    "_scanner-integration.yml",
    "gerrit-verify.yaml",
}


def _load_workflow(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _is_gerrit_checkout(step: dict[str, Any]) -> bool:
    return str(step.get("uses", "")).startswith(GERRIT_CHECKOUT_ACTION)


def _matching_checkouts(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in steps if _is_gerrit_checkout(step)]


def test_every_gerrit_checkout_uses_three_in_job_attempts() -> None:
    """A transient Gerrit/transport 5xx must be retried before the gate can vote."""
    found = 0
    for workflow_name in EXPECTED_WORKFLOWS:
        workflow = _load_workflow(WORKFLOW_DIR / workflow_name)
        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", []) or []
            checkouts = _matching_checkouts(steps)
            if not checkouts:
                continue
            found += 1
            attempts = {step.get("id"): step for step in checkouts}
            assert set(attempts) == {
                "checkout-gerrit-attempt-1",
                "checkout-gerrit-attempt-2",
                "checkout-gerrit-attempt-3",
            }, f"{workflow_name}::{job_name} must use exactly three checkout attempts"
            assert attempts["checkout-gerrit-attempt-1"].get("continue-on-error") is True
            assert attempts["checkout-gerrit-attempt-2"].get("continue-on-error") is True
            assert attempts["checkout-gerrit-attempt-3"].get("continue-on-error") is not True
            first_if = str(attempts["checkout-gerrit-attempt-1"].get("if", "")).lower()
            assert "gerrit-refspec != ''" in first_if or "gerrit_refspec != ''" in first_if
            assert "steps.checkout-gerrit-attempt-1.outcome == 'failure'" in str(
                attempts["checkout-gerrit-attempt-2"].get("if", "")
            )
            assert "steps.checkout-gerrit-attempt-2.outcome == 'failure'" in str(
                attempts["checkout-gerrit-attempt-3"].get("if", "")
            )
    assert found >= 1


def test_retry_attempts_keep_exact_gerrit_inputs() -> None:
    for workflow_name in EXPECTED_WORKFLOWS:
        workflow = _load_workflow(WORKFLOW_DIR / workflow_name)
        for job in workflow["jobs"].values():
            expected_inputs = (
                {
                    "gerrit-refspec": "${{ inputs.GERRIT_REFSPEC }}",
                    "gerrit-project": "${{ inputs.GERRIT_PROJECT }}",
                    "gerrit-url": "https://${{ vars.GERRIT_SERVER }}",
                }
                if workflow_name == "gerrit-verify.yaml"
                else {
                    "gerrit-refspec": "${{ inputs.gerrit-refspec }}",
                    "gerrit-project": "${{ inputs.gerrit-project }}",
                    "gerrit-url": "${{ inputs.gerrit-url }}",
                }
            )
            for step in _matching_checkouts(job.get("steps", []) or []):
                assert step["with"] == expected_inputs
