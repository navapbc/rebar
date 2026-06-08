"""Inbound probe — classifies issues that disappeared from the JQL working set.

When a local ticket's bound jira_key vanishes from a fetcher pass, the probe
fetches the issue directly via stdlib urllib (GET-only) and classifies the
result into one of 4 branches:

  1. PRESENT_RESOLVED    — issue still exists; status was changed to Resolved/Done/Cancelled (out of working set)
  2. PRESENT_FILTERED    — issue still exists but no longer matches the JQL filter for other reasons
  3. ARCHIVED_OR_MOVED   — 404/410/403 — the issue has been deleted, archived, or moved off the project
  4. UNREACHABLE         — transient network / auth error; do not classify, leave for retry

GET-only invariant: every Request uses get_method() == 'GET'. POST/PUT/DELETE
would be a contract violation.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProbeConfigError(RuntimeError):
    """Raised when required env vars are missing."""


class ProbeBranch(StrEnum):
    PRESENT_RESOLVED = "present_resolved"
    PRESENT_FILTERED = "present_filtered"
    ARCHIVED_OR_MOVED = "archived_or_moved"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    branch: ProbeBranch
    issue_key: str
    detail: dict[str, Any]


RESOLVED_STATUS_NAMES = frozenset({"Resolved", "Done", "Cancelled"})


def _make_request(jira_url: str, issue_key: str, user: str, token: str) -> urllib.request.Request:
    """Build a GET-only Request. The get_method() returns 'GET' explicitly."""
    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{issue_key}?fields=status,resolution"
    req = urllib.request.Request(url, method="GET")
    import base64
    creds = base64.b64encode(f"{user}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    req.add_header("Accept", "application/json")
    return req


def _resolve_env() -> tuple[str, str, str]:
    missing = []
    jira_url = os.environ.get("JIRA_URL")
    user = os.environ.get("JIRA_USER")
    token = os.environ.get("JIRA_API_TOKEN")
    if not jira_url:
        missing.append("JIRA_URL")
    if not user:
        missing.append("JIRA_USER")
    if not token:
        missing.append("JIRA_API_TOKEN")
    if missing:
        raise ProbeConfigError(f"inbound_probe: missing required env var(s): {', '.join(missing)}")
    return jira_url, user, token


def classify_probe_response(issue_key: str, status_code: int, payload: dict) -> ProbeResult:
    """Pure classifier — used by both real probe and tests."""
    if status_code in (404, 410, 403):
        return ProbeResult(ProbeBranch.ARCHIVED_OR_MOVED, issue_key, {"status_code": status_code})
    if status_code >= 500 or status_code == 401:
        return ProbeResult(ProbeBranch.UNREACHABLE, issue_key, {"status_code": status_code})
    if status_code == 200:
        status_name = (payload.get("fields", {}).get("status") or {}).get("name", "")
        if status_name in RESOLVED_STATUS_NAMES:
            return ProbeResult(ProbeBranch.PRESENT_RESOLVED, issue_key, {"status": status_name})
        return ProbeResult(ProbeBranch.PRESENT_FILTERED, issue_key, {"status": status_name})
    # Unknown status code — treat as unreachable
    return ProbeResult(ProbeBranch.UNREACHABLE, issue_key, {"status_code": status_code, "unknown": True})


def probe(issue_key: str) -> ProbeResult:
    """Live probe — issues a GET to Jira and classifies."""
    jira_url, user, token = _resolve_env()
    req = _make_request(jira_url, issue_key, user, token)
    assert req.get_method() == "GET", "GET-only invariant violated"
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return classify_probe_response(issue_key, resp.status, payload)
    except urllib.error.HTTPError as e:
        return classify_probe_response(issue_key, e.code, {})
    except (urllib.error.URLError, TimeoutError) as e:
        return ProbeResult(ProbeBranch.UNREACHABLE, issue_key, {"error": str(e)})
