#!/usr/bin/env python3
"""normalize_ci_conclusion.py — map aggregated CI conclusion to Gerrit vote-type domain.

Ports the retired normalize_ci_conclusion shell script (ticket bad2; port ticket 9d07).

Inputs (env vars):
  CONCLUSION        raw WORKFLOW_CONCLUSION (may be empty)
  FAILURE_OBSERVED  "true" if any needed job's result was failure/cancelled
  JOBS_FILE         optional path to this run's list-jobs payload (``{"jobs": [...]}`` or a
                    bare list). Used only to recognise a run whose sole unsuccessful jobs never
                    got a runner (ticket 20e1-f766-929b-4951).
Output (stdout): exactly one of success|failure|cancelled
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# The vote job is still running when the snapshot is taken, so it has no conclusion yet.
VOTE_JOB_NAME = "cast Verified"
# Conclusions that carry no failure.
_PASSING = frozenset({"success", "skipped", "neutral"})


def runner_never_acquired(job: dict[str, Any]) -> bool:
    """True for a job GitHub cancelled before any runner picked it up.

    "The job was not acquired by Runner of type hosted even after multiple attempts" leaves
    ``runner_id`` 0, an empty ``runner_name`` and no steps. A job cancelled after it started
    (superseded, timed out, or stopped mid-test) always has a runner and steps.
    """
    return (
        job.get("conclusion") == "cancelled"
        and not job.get("steps")
        and not job.get("runner_id")
        and not job.get("runner_name")
    )


def _load_jobs(jobs_file: str) -> list[dict[str, Any]] | None:
    """Read the jobs payload, or None when it is absent or malformed (fail closed)."""
    try:
        with open(jobs_file, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        return None
    return jobs


def only_unacquired_runners_failed(jobs_file: str) -> bool:
    """True when at least one job never got a runner and every other job passed."""
    jobs = _load_jobs(jobs_file) if jobs_file else None
    if not jobs:
        return False
    others = [job for job in jobs if job.get("name") != VOTE_JOB_NAME]
    unacquired = [job for job in others if runner_never_acquired(job)]
    rest = [job for job in others if not runner_never_acquired(job)]
    return bool(unacquired) and all(job.get("conclusion") in _PASSING for job in rest)


def normalize(conclusion: str, failure_observed: str, jobs_file: str = "") -> str:
    """Map (conclusion, failure_observed) to a Gerrit vote-type.

    The returned value is always in {success, failure, cancelled}. ``cancelled`` casts no
    label (ticket d1d4-8c8b-e2c5-4ac2): it is used for a superseded run and for a run whose
    only unsuccessful jobs were never given a runner, since neither carries a test verdict.
    """
    if conclusion == "success":
        return "success"
    if conclusion == "failure":
        if failure_observed != "true" and only_unacquired_runners_failed(jobs_file):
            return "cancelled"
        return "failure"
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion == "skipped":
        return "failure" if failure_observed == "true" else "success"
    # Empty or any unrecognized value: fail closed.
    return "failure"


if __name__ == "__main__":
    conclusion = os.environ.get("CONCLUSION", "")
    failure_observed = os.environ.get("FAILURE_OBSERVED", "")
    jobs_file = os.environ.get("JOBS_FILE", "")
    print(normalize(conclusion, failure_observed, jobs_file))
    sys.exit(0)
