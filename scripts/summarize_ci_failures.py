#!/usr/bin/env python3
"""summarize_ci_failures.py — render a triage summary of a run's non-successful jobs.

WHY THIS EXISTS
---------------
The Gerrit ``Verified`` vote is cast by ``lfreleng-actions/gerrit-review-action``, which
builds its own message and offers no input for custom text:

    MESSAGE="${STATUS}: ${SERVER_URL}/${REPOSITORY}/actions/runs/${RUN_ID}"

So a ``Verified -1`` reads exactly ``CANCELLED: <run URL>`` no matter what went wrong. A job
that hit its ``timeout-minutes`` and a job that failed on stale-base version skew produce a
byte-identical vote, even though the first needs no author action and the second needs a
rebase. Whoever picks the change up has to open the run and read job conclusions by hand.

This script turns the run's own job list into a short summary that
``.github/workflows/gerrit-verify.yaml`` posts as a comment-only review beside the vote. It
changes nothing about WHEN the gate votes +1/-1 — only what an operator can see without
opening the run.

Distinguishing a timeout from a plain cancellation needs the check-run annotations: GitHub
records a timed-out job as ``conclusion: cancelled`` and explains itself only in the
annotation text ("The job has exceeded the maximum execution time of 30m0s"). Jobs and
annotations are therefore both inputs.

Inputs (env vars):
  JOBS_JSON         GitHub list-jobs-for-a-run payload (``{"jobs": [...]}``) or a bare list
  ANNOTATIONS_JSON  optional ``{"<job id>": [{"message": ...}, ...]}`` map
Output (stdout): the summary, or nothing at all when every job succeeded.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any

# GitHub's wording when a job exceeds `timeout-minutes`. Matched case-insensitively on a
# substring so a change to the duration suffix ("of 30m0s") cannot break detection.
TIMEOUT_MARKER = "exceeded the maximum execution time"

# Conclusions that say nothing useful on their own and so are worth explaining.
REPORTABLE = ("failure", "cancelled", "timed_out")

HEADER = "CI did not pass. Jobs that did not succeed:"
FOOTER_TIMEOUT = (
    "A job that TIMED OUT reports the same `cancelled` conclusion as a superseded run; "
    "it is not a test verdict. Re-run it or raise that job's `timeout-minutes`."
)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp, tolerating the trailing ``Z``."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_duration(started_at: str | None, completed_at: str | None) -> str:
    """Render a job's wall-clock duration as ``NmNNs``, or "" if it cannot be computed."""
    start = _parse_ts(started_at)
    end = _parse_ts(completed_at)
    if start is None or end is None:
        return ""
    total = int((end - start).total_seconds())
    if total < 0:
        return ""
    return f"{total // 60}m{total % 60:02d}s"


def sanitize(text: str) -> str:
    """Strip characters that could break out of the remote shell's single-quoted argument.

    The summary is sent as ``gerrit review <change>,<ps> --message '<summary>'`` over ssh, and
    job names are NOT fully trusted: matrix job names derive from ``.github/mutation-shards.toml``,
    which a patchset controls. A single quote in a job name would otherwise terminate the
    quoting and let the rest of the name be parsed as arguments to ``gerrit review``. Drop the
    quoting metacharacters outright and keep only printable ASCII plus newline.
    """
    stripped = text.replace("'", "").replace("\\", "")
    return "".join(ch for ch in stripped if ch == "\n" or (" " <= ch <= "~"))


def _is_timeout(annotations: list[dict[str, Any]]) -> bool:
    """True when any annotation carries GitHub's job-timeout wording."""
    return any(
        TIMEOUT_MARKER in str(annotation.get("message", "")).lower() for annotation in annotations
    )


def describe_job(job: dict[str, Any], annotations: list[dict[str, Any]]) -> str:
    """Render one non-successful job as a single triage line.

    The three outcomes are worded differently on purpose: `timed out` and `cancelled` share
    the `cancelled` conclusion in GitHub's API but mean opposite things to an author.
    """
    conclusion = str(job.get("conclusion") or "")
    name = sanitize(str(job.get("name") or "(unnamed job)"))

    if conclusion == "timed_out" or (conclusion == "cancelled" and _is_timeout(annotations)):
        outcome = "TIMED OUT"
    elif conclusion == "cancelled":
        outcome = "CANCELLED (not a test verdict)"
    else:
        outcome = "FAILED"

    line = f"- {name}: {outcome}"
    duration = format_duration(job.get("started_at"), job.get("completed_at"))
    if duration:
        line += f" after {duration}"
    url = job.get("html_url")
    if url:
        line += f"\n  {url}"
    return line


def summarize(
    jobs: list[dict[str, Any]],
    annotations: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Summarize the non-successful jobs of a run.

    Returns the empty string when nothing needs explaining, so a green run posts no comment.
    """
    annotations = annotations or {}
    lines = [
        describe_job(job, annotations.get(str(job.get("id")), []))
        for job in jobs
        if str(job.get("conclusion") or "") in REPORTABLE
    ]
    if not lines:
        return ""

    summary = "\n".join([HEADER, *lines])
    if "TIMED OUT" in summary:
        summary += f"\n\n{FOOTER_TIMEOUT}"
    # Final guard: sanitize the whole message, not just the job names, so no field that
    # reaches the remote `gerrit review` command can carry shell-quoting metacharacters.
    return sanitize(summary)


def _load_jobs(raw: str) -> list[dict[str, Any]]:
    """Accept either the full list-jobs payload or a bare list of jobs."""
    if not raw.strip():
        return []
    payload = json.loads(raw)
    if isinstance(payload, dict):
        return list(payload.get("jobs", []))
    return list(payload)


if __name__ == "__main__":
    jobs = _load_jobs(os.environ.get("JOBS_JSON", ""))
    raw_annotations = os.environ.get("ANNOTATIONS_JSON", "").strip()
    # NB: not named `annotations` — that is the module-level `__future__` import.
    job_annotations = json.loads(raw_annotations) if raw_annotations else {}
    text = summarize(jobs, job_annotations)
    if text:
        print(text)
    sys.exit(0)
