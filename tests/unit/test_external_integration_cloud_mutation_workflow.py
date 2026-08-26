"""Config-as-artifact gate for the `jira-cloud-mutation-probe` lane (ticket 1d21-887e).

The epic's Live-External AC requires the RP-03 create-coordinator's create / binding /
commit-unknown / fuse to be exercised against **live Jira Cloud** in CI. The only existing
Cloud lane is read-only and gated off, so a new BOUNDED, self-cleaning mutation lane is
added to ``external-integration.yml``. This test pins that lane's shape directly from the
workflow YAML (never trusting a comment): it must be dispatchable, wire the run-scoped
teardown label from the run id, run the capability preflight and the live-Cloud module,
and ALWAYS sweep its throwaway issues by that label on failure or cancel — so a leaked
mutation can never survive a crashed run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "external-integration.yml"

_JOB = "jira-cloud-mutation-probe"
_MODULE_DIR = "tests/external/live_jira_cloud_mutation"


def _load() -> dict:
    assert _WORKFLOW.exists(), f"expected workflow missing: {_WORKFLOW}"
    return yaml.safe_load(_WORKFLOW.read_text())


def _job() -> dict:
    doc = _load()
    jobs = doc.get("jobs", {})
    assert _JOB in jobs, f"{_JOB!r} lane missing from {_WORKFLOW}"
    return jobs[_JOB]


def _steps() -> list[dict]:
    steps = _job().get("steps", [])
    assert isinstance(steps, list) and steps, f"{_JOB!r} has no steps"
    return steps


def test_workflow_is_dispatchable_and_scheduled() -> None:
    doc = _load()
    # PyYAML parses a bare, unquoted `on:` key as the boolean True (YAML 1.1 implicit
    # booleans); read both defensively.
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), f"'on:' block is not a mapping: {triggers!r}"
    assert "workflow_dispatch" in triggers
    assert "schedule" in triggers


def test_lane_exports_the_run_scoped_teardown_label() -> None:
    """REBAR_PROBE_RUN_LABEL must be derived from the GitHub run id (unique per run)."""
    text = yaml.safe_dump(_job())
    assert "REBAR_PROBE_RUN_LABEL" in text, "lane does not set REBAR_PROBE_RUN_LABEL"
    assert "cloudprobe-" in text, "run label is not the cloudprobe-<run_id> scheme"
    assert "github.run_id" in text, "run label is not derived from github.run_id"


def test_lane_runs_check_access_preflight_then_the_live_module() -> None:
    text = yaml.safe_dump(_steps())
    assert "check-access" in text, "lane does not run the bridge check-access preflight"
    assert _MODULE_DIR in text, f"lane does not run the {_MODULE_DIR} module"


def test_lane_wires_the_existing_cloud_credentials() -> None:
    """The lane must reference the repo's provisioned Jira Cloud creds, not placeholders."""
    text = yaml.safe_dump(_job())
    assert "vars.JIRA_URL" in text
    assert "vars.JIRA_USER" in text
    assert "vars.JIRA_PROJECT" in text
    assert "secrets.JIRA_API_TOKEN" in text
    # The external tier is inert unless opted in.
    assert "REBAR_RUN_EXTERNAL" in text


def test_lane_has_an_always_run_labelled_teardown_sweep() -> None:
    """A failure/cancel teardown must delete every issue carrying the run label via acli."""
    teardown = None
    for step in _steps():
        cond = str(step.get("if", ""))
        if "failure()" in cond and "cancelled()" in cond:
            body = str(step.get("run", ""))
            if "acli" in body and "delete" in body:
                teardown = step
                break
    assert teardown is not None, "no always-run acli teardown step found"

    body = str(teardown.get("run", ""))
    # The real acli invocation path (bug 3256): the full `jira workitem` subcommands.
    assert "acli jira workitem delete" in body, "teardown does not use the acli delete subcommand"
    assert "--yes" in body, "non-TTY delete must pass --yes so the sweep does not block on stdin"
    assert "REBAR_PROBE_RUN_LABEL" in body, "teardown is not keyed on the run label"
