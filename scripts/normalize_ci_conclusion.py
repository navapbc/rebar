#!/usr/bin/env python3
"""normalize_ci_conclusion.py — map aggregated CI conclusion to Gerrit vote-type domain.

Ports the retired normalize_ci_conclusion shell script (ticket bad2; port ticket 9d07).

Inputs (env vars):
  CONCLUSION        raw WORKFLOW_CONCLUSION (may be empty)
  FAILURE_OBSERVED  "true" if any needed job's result was failure/cancelled
Output (stdout): exactly one of success|failure|cancelled
"""
from __future__ import annotations

import os
import sys


def normalize(conclusion: str, failure_observed: str) -> str:
    """Map (conclusion, failure_observed) to a Gerrit vote-type.

    The returned value is always in {success, failure, cancelled}.
    """
    if conclusion == "success":
        return "success"
    if conclusion == "failure":
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
    print(normalize(conclusion, failure_observed))
    sys.exit(0)
