"""Isolated GitHub-Actions coverage / CI-health adapter (ticket 1f77).

Parses two signals from GitHub-Actions artifacts without any network access:

- ``parse_coverage`` extracts the overall coverage percentage from a
  pytest-cov ``TOTAL`` summary line in captured job-log text.
- ``red_to_green_recovery`` measures how long CI stayed red — the interval
  between a failing run and the next passing run — from run metadata.

This module is **isolated**: it is imported only by ``scripts/harvest_gha.py``
(the persistence path) and its tests. The core ``rebar.metrics`` package and
its registry never import it, and importing it registers nothing into
``REGISTRY``.
"""

from __future__ import annotations

import re

# Anchored to the start of a line, ``TOTAL`` as a whole word, capturing the
# trailing ``NN.N%`` (or integer ``NN%``) coverage figure produced by pytest-cov.
_TOTAL_COVERAGE_RE = re.compile(
    r"^TOTAL\b.*?(\d+(?:\.\d+)?)%",
    re.MULTILINE,
)


def parse_coverage(log_text: str) -> float | None:
    """Return the coverage percentage from a pytest-cov ``TOTAL`` line.

    For a line like ``TOTAL   1000   158   84.2%`` this returns ``84.2``.
    Returns ``None`` when the log contains no ``TOTAL`` coverage line.
    """
    match = _TOTAL_COVERAGE_RE.search(log_text)
    if match is None:
        return None
    return float(match.group(1))


def red_to_green_recovery(runs: list[dict]) -> int | None:
    """Return the recovery interval from a failing run to the next passing run.

    ``runs`` are run dicts — each ``{"conclusion": "success"|"failure",
    "created_at": <int>, "head_sha": ...}`` — ordered oldest-first. Scanning
    forward, the first ``failure`` run followed (at any later index) by a
    ``success`` run yields the ``created_at`` delta between them. Returns
    ``None`` when no failure-then-success recovery exists.
    """
    pending_failure: int | None = None
    for run in runs:
        conclusion = run.get("conclusion")
        if conclusion == "failure":
            if pending_failure is None:
                pending_failure = run["created_at"]
        elif conclusion == "success" and pending_failure is not None:
            return int(run["created_at"] - pending_failure)
    return None
