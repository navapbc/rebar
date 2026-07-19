"""Anti-rot gate: the end-to-end probe must stay wired into CI (ticket 6cca).

``scripts/probe-rebar.sh`` is the reusable end-to-end CLI probe. To keep it from
silently rotting, the ``golden-path`` job in BOTH CI entry points must invoke it:

* ``.github/workflows/test.yml`` (the GitHub push/PR mirror), and
* ``.github/workflows/gerrit-verify.yaml`` (the pre-merge Verified gate).

This test parses each workflow, locates the ``golden-path`` job, and asserts one
of its steps runs ``scripts/probe-rebar.sh``. If either job drops the probe
step, this test fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# workflow filename -> the job key whose steps must invoke the probe.
_PROBE_JOBS: dict[str, str] = {
    "test.yml": "golden-path",
    "gerrit-verify.yaml": "golden-path",
}


def _job_run_scripts(workflow: str, job: str) -> list[str]:
    path = _WORKFLOWS_DIR / workflow
    assert path.exists(), f"expected workflow {workflow} is missing"
    doc = yaml.safe_load(path.read_text())
    jobs = doc.get("jobs") or {}
    assert job in jobs, f"{workflow}: job {job!r} not found (jobs: {sorted(jobs)})"
    steps = jobs[job].get("steps") or []
    return [step["run"] for step in steps if isinstance(step, dict) and "run" in step]


@pytest.mark.parametrize(("workflow", "job"), sorted(_PROBE_JOBS.items()))
def test_golden_path_job_invokes_probe(workflow: str, job: str) -> None:
    runs = _job_run_scripts(workflow, job)
    assert any("scripts/probe-rebar.sh" in run for run in runs), (
        f"{workflow}: the {job!r} job has no step running scripts/probe-rebar.sh — "
        f"the end-to-end probe is not wired into CI and can silently rot"
    )
