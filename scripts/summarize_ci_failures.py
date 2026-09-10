#!/usr/bin/env python3
"""Render a comment-only triage summary for unsuccessful Gerrit CI jobs.

The ``Verified`` vote links to the run but does not distinguish test failures, cancelled
jobs, timeouts, or transport faults. This script combines job conclusions with check-run
annotations because GitHub can encode a timeout as ``conclusion: cancelled``. The verify
workflow posts the result as a review comment without changing the vote.

Inputs:
  JOBS_JSON         GitHub list-jobs-for-a-run payload (``{"jobs": [...]}``) or a bare list
  ANNOTATIONS_JSON  optional ``{"<job id>": [{"message": ...}, ...]}`` map
Output:
  stdout            summary text, or nothing when every job succeeded
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any

# Match GitHub's timeout wording without depending on its duration suffix.
TIMEOUT_MARKER = "exceeded the maximum execution time"
TRANSPORT_MARKERS = (
    "error: rpc failed; http 5",
    "the requested url returned error: 5",
    "fatal: unable to access",
    "gnutls recv error",
    "connection reset by peer",
    "failed to connect",
    "could not resolve host",
    "operation timed out",
    "remote end hung up unexpectedly",
    "early eof",
)

# Conclusions that require explanatory review text.
REPORTABLE = ("failure", "cancelled", "timed_out")

HEADER = "CI did not pass. Jobs that did not succeed:"
FOOTER_TIMEOUT = (
    "A job that TIMED OUT reports the same `cancelled` conclusion as a superseded run; "
    "it is not a test verdict. Re-run it or raise that job's `timeout-minutes`."
)
FOOTER_TRANSPORT = (
    "An INFRASTRUCTURE/TRANSPORT FAULT means CI could not obtain the source; "
    "this is not a statement about the change."
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
    """Keep printable ASCII while removing remote shell quoting metacharacters.

    Patchsets control matrix job names, which enter a single-quoted ``gerrit review``
    message. Removing quotes and backslashes prevents argument injection.
    """
    stripped = text.replace("'", "").replace("\\", "")
    return "".join(ch for ch in stripped if ch == "\n" or (" " <= ch <= "~"))


def _is_timeout(annotations: list[dict[str, Any]]) -> bool:
    """True when any annotation carries GitHub's job-timeout wording."""
    return any(
        TIMEOUT_MARKER in str(annotation.get("message", "")).lower() for annotation in annotations
    )


def _is_transport_fault(job: dict[str, Any], annotations: list[dict[str, Any]]) -> bool:
    """True when the job/annotations show an observable git/HTTP transport failure."""
    haystack_parts = [str(job.get("name") or ""), str(job.get("html_url") or "")]
    haystack_parts.extend(str(annotation.get("message", "")) for annotation in annotations)
    haystack = "\n".join(haystack_parts).lower()
    return any(marker in haystack for marker in TRANSPORT_MARKERS)


def describe_job(job: dict[str, Any], annotations: list[dict[str, Any]]) -> str:
    """Render one job while distinguishing timeout, cancellation, and failure."""
    conclusion = str(job.get("conclusion") or "")
    name = sanitize(str(job.get("name") or "(unnamed job)"))

    if _is_transport_fault(job, annotations):
        outcome = "INFRASTRUCTURE/TRANSPORT FAULT (not a test verdict)"
    elif conclusion == "timed_out" or (conclusion == "cancelled" and _is_timeout(annotations)):
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
    if "INFRASTRUCTURE/TRANSPORT FAULT" in summary:
        summary += f"\n\n{FOOTER_TRANSPORT}"
    # Sanitize the assembled message so every remote-shell input follows the same rule.
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
    # Avoid shadowing the module-level ``annotations`` future feature.
    job_annotations = json.loads(raw_annotations) if raw_annotations else {}
    text = summarize(jobs, job_annotations)
    if text:
        print(text)
    sys.exit(0)
