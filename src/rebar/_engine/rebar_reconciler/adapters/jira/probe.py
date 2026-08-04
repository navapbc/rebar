"""Jira absence probe — classifies issues that disappeared from the JQL working set.

When a local ticket's bound jira_key vanishes from a fetcher pass, the probe fetches
the issue directly via stdlib urllib (GET-only) and classifies the result into one of
the 4 :class:`~rebar_reconciler.inbound_probe.ProbeBranch` branches:

  1. PRESENT_RESOLVED    — issue still exists; status was changed to
                           Resolved/Done/Cancelled (out of working set)
  2. PRESENT_FILTERED    — issue still exists but no longer matches the JQL filter
                           for other reasons
  3. ARCHIVED_OR_MOVED   — 404/410/403 — the issue has been deleted, archived, or
                           moved off the project
  4. UNREACHABLE         — transient network / auth error; do not classify, leave for retry

GET-only invariant: every Request uses get_method() == 'GET'. POST/PUT/DELETE
would be a contract violation.

Story J2 (epic e369) relocated the pure classifier to ``adapters/jira_family/probe.py``
(Jira-family-general logic), taking the resolved-status set as a parameter instead of
reading a module-level constant. ``RESOLVED_STATUS_NAMES`` below is Cloud/DIG's
*configured value* (a self-hosted Data Center workflow can name its resolved states
anything), so it stays defined here, and ``classify_probe_response`` below is a
Cloud-side BOUND function — not a bare re-export — that delegates to the shared
classifier with this module's frozenset bound in.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from rebar_reconciler.adapters.jira_family import (
    classify_probe_response as _classify_probe_response,
)
from rebar_reconciler.inbound_probe import ProbeBranch, ProbeConfigError, ProbeResult

RESOLVED_STATUS_NAMES = frozenset({"Resolved", "Done", "Cancelled"})
# DECISION (story 2127-348c-c41d-472e, item 3 — repo artifact, not a tracker-only note):
# This set is HARDCODED here, whereas the DC side already sources its equivalent from
# config (ReconcilerConfig.resolved_statuses, src/rebar/_config_schema.py). A Cloud tenant
# whose workflow uses non-standard resolved-status names (e.g. "Closed", "Complete",
# "Won't Do") would therefore have those issues MISCLASSIFIED as PRESENT_FILTERED instead
# of PRESENT_RESOLVED. A runtime/config hook IS warranted for Cloud↔DC parity: Cloud should
# read a jira.resolved_statuses config key (defaulting to this frozenset) the same way DC
# reads reconciler.resolved_statuses. Tracked by story e34a-1d0c-0daa-4f2c
# (discovered_from 2127). Until that lands, this default matches Cloud/DIG's own configured
# resolved-status names, so a stock DIG tenant is unaffected.


def _make_request(jira_url: str, issue_key: str, user: str, token: str) -> urllib.request.Request:
    """Build a GET-only Request. The get_method() returns 'GET' explicitly."""
    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{issue_key}?fields=status,resolution"
    req = urllib.request.Request(url, method="GET")
    creds = base64.b64encode(f"{user}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    req.add_header("Accept", "application/json")
    return req


def _resolve_env() -> tuple[str, str, str]:
    # url/user resolve through the typed config (JIRA_URL/JIRA_USER env override the
    # [tool.rebar.jira] file); the secret token is env-only. All three stay required.
    from rebar_reconciler.adapters.jira import acli_subprocess

    settings = acli_subprocess.resolve_jira_settings()
    jira_url, user, token = settings.url, settings.user, settings.api_token
    missing = []
    if not jira_url:
        missing.append("JIRA_URL")
    if not user:
        missing.append("JIRA_USER")
    if not token:
        missing.append("JIRA_API_TOKEN")
    if missing:
        raise ProbeConfigError(f"inbound_probe: missing required Jira config: {', '.join(missing)}")
    return jira_url, user, token


def classify_probe_response(issue_key: str, status_code: int, payload: dict) -> ProbeResult:
    """Cloud-bound classifier — delegates to the shared ``jira_family`` classifier,
    binding this module's ``RESOLVED_STATUS_NAMES`` as the resolved-status set. Used
    by both the real probe and tests."""
    return _classify_probe_response(
        issue_key, status_code, payload, resolved_statuses=RESOLVED_STATUS_NAMES
    )


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
