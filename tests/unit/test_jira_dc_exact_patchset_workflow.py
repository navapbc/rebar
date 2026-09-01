"""Config-as-artifact gate for the exact-patchset Jira DC workflow (AC1/AC2 of ticket
`3f27-cb3c-8023-4f57` / `single-vast-roan`).

The ticket's AC1 requires that CI "checks out and records the exact Gerrit patchset SHA
used for the ephemeral DC run" and that "an all-skipped or wrong-ref run fails." This test
parses the workflow file directly (never trusting a code comment) to assert those
properties actually hold, mirroring the config-as-artifact discipline used by
``test_jira_dc_capability_map_workflow.py`` for its sibling job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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


def test_uses_pinned_gerrit_checkout_action() -> None:
    text = _WORKFLOW.read_text()
    assert "lfreleng-actions/checkout-gerrit-change-action@" in text
    assert "@f87b90a3370e62dad310797fedc7fd3700c75832" in text, (
        "checkout-gerrit-change-action must be pinned by commit SHA (mirrors gerrit-verify.yaml)"
    )


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
