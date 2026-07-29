"""Jira-family absence-probe classifier (story J2, epic e369).

Relocated from ``adapters/jira/probe.py``: classifies a fetched issue response into
one of the 4 :class:`~rebar_reconciler.inbound_probe.ProbeBranch` branches.

  1. PRESENT_RESOLVED    — issue still exists; its status is one of the caller's
                           resolved-status names (out of the working set)
  2. PRESENT_FILTERED    — issue still exists but no longer matches the JQL filter
                           for other reasons
  3. ARCHIVED_OR_MOVED   — 404/410/403 — the issue has been deleted, archived, or
                           moved off the project
  4. UNREACHABLE         — transient network / auth error; do not classify, leave for retry

The resolved-status set is a REQUIRED keyword-only parameter, deliberately with no
default: Cloud/DIG's workflow names (``Resolved``/``Done``/``Cancelled``) are a
CONFIGURED value, not shared logic — a self-hosted Data Center workflow can name its
resolved states anything, so baking Cloud's names in here would misclassify a
resolved DC issue as ``PRESENT_FILTERED``. Cloud binds its own frozenset in
``adapters/jira/probe.py``; a future Jira-family backend binds its own.
"""

from __future__ import annotations

from rebar_reconciler.inbound_probe import ProbeBranch, ProbeResult


def classify_probe_response(
    issue_key: str, status_code: int, payload: dict, *, resolved_statuses: frozenset[str]
) -> ProbeResult:
    """Pure classifier — used by both the real probe and tests.

    ``resolved_statuses`` is the caller's configured set of Jira workflow status
    names that mean "resolved/out of the working set" (e.g. Cloud/DIG's
    ``{"Resolved", "Done", "Cancelled"}"). Required and keyword-only so a caller
    cannot silently inherit another backend's workflow names.
    """
    if status_code in (404, 410, 403):
        return ProbeResult(ProbeBranch.ARCHIVED_OR_MOVED, issue_key, {"status_code": status_code})
    if status_code >= 500 or status_code == 401:
        return ProbeResult(ProbeBranch.UNREACHABLE, issue_key, {"status_code": status_code})
    if status_code == 200:
        status_name = (payload.get("fields", {}).get("status") or {}).get("name", "")
        if status_name in resolved_statuses:
            return ProbeResult(ProbeBranch.PRESENT_RESOLVED, issue_key, {"status": status_name})
        return ProbeResult(ProbeBranch.PRESENT_FILTERED, issue_key, {"status": status_name})
    # Unknown status code — treat as unreachable
    return ProbeResult(
        ProbeBranch.UNREACHABLE, issue_key, {"status_code": status_code, "unknown": True}
    )
