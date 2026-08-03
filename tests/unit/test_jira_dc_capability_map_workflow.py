"""Config-as-artifact gate for the Jira DC capability-map workflow (ticket 259b-b7da-a346-4785).

The mapping job is explicitly an AUTHORING tool, never a test-path component (see the
workflow's own header comment and ``scripts/jira_dc_capability_map.py``'s docstring): its
acceptance criteria require that it "cannot run on push, PR, or schedule, and this is
asserted, not merely configured." This test is that assertion — it parses the workflow's
``on:`` trigger block directly (never trusting a code comment) and fails if anything other
than a bare ``workflow_dispatch`` ever appears there.

It also pins the harness-boot shape to the SAME digest-pinned Dockerfile the
``jira-dc-harness`` job in ``external-integration.yml`` builds, so a future edit cannot
quietly repoint this job at a different, unpinned image and still call itself "pinned by
digest" per the ticket's caveat.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "jira-dc-capability-map.yml"
_HARNESS_DOCKERFILE = _ROOT / "tests" / "external" / "live_jira_dc" / "Dockerfile"


def _load() -> dict:
    assert _WORKFLOW.exists(), f"expected workflow missing: {_WORKFLOW}"
    return yaml.safe_load(_WORKFLOW.read_text())


def test_dispatch_only_no_push_pr_or_schedule() -> None:
    doc = _load()
    # PyYAML parses the bare `on:` key as the boolean True when unquoted, per YAML 1.1's
    # implicit boolean scalars — a real, previously-observed footgun for exactly this kind
    # of trigger-block assertion, so this reads BOTH keys defensively.
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), f"'on:' block is not a mapping: {triggers!r}"
    assert set(triggers) == {"workflow_dispatch"}, (
        f"jira-dc-capability-map.yml must be workflow_dispatch-ONLY (found trigger keys "
        f"{sorted(triggers)}) — this job is an authoring tool, never a test-path component, "
        f"and must never fire on push, pull_request, or a schedule (ticket 259b-b7da-a346-4785)"
    )


def test_no_write_permissions_granted() -> None:
    doc = _load()
    perms = doc.get("permissions") or {}
    assert perms.get("contents") == "read", (
        f"jira-dc-capability-map.yml should request only read access to repo contents "
        f"(found permissions={perms!r}) — it authors an artifact for a human to commit, "
        f"it does not commit anything itself"
    )


def test_boots_the_same_digest_pinned_harness_dockerfile() -> None:
    """The job must build tests/external/live_jira_dc/'s vendored Dockerfile — the ONE
    place the base image is pinned by digest — rather than a second, independently-pinned
    (and driftable) image reference."""
    assert _HARNESS_DOCKERFILE.exists(), f"harness Dockerfile missing: {_HARNESS_DOCKERFILE}"
    dockerfile_text = _HARNESS_DOCKERFILE.read_text()
    assert "@sha256:" in dockerfile_text.splitlines()[0] or any(
        line.startswith("FROM") and "@sha256:" in line for line in dockerfile_text.splitlines()
    ), f"{_HARNESS_DOCKERFILE} no longer pins its base image by digest"

    doc = _load()
    jobs = doc.get("jobs") or {}
    assert jobs, "workflow defines no jobs"
    run_steps: list[str] = []
    for job in jobs.values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "run" in step:
                run_steps.append(step["run"])
    assert any("docker compose" in run and "--build" in run for run in run_steps), (
        "expected a step that builds the harness via `docker compose up -d --build` "
        "(against tests/external/live_jira_dc/Dockerfile) so the digest pin there applies here too"
    )


def test_script_is_invoked() -> None:
    doc = _load()
    jobs = doc.get("jobs") or {}
    run_steps: list[str] = []
    for job in jobs.values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "run" in step:
                run_steps.append(step["run"])
    assert any("scripts/jira_dc_capability_map.py" in run for run in run_steps), (
        "workflow must invoke scripts/jira_dc_capability_map.py — the mapping tool "
        "cannot silently rot out of this job"
    )
