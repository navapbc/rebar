#!/usr/bin/env python3
"""Forward-compatibility probe: exercises 4 identity-critical Jira operations on a throwaway issue."""

from __future__ import annotations

import importlib.util
import os
import time
import urllib.error
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# JQL search retry constants — mirror jira-capability-probe's pattern so both
# probes are resilient to Jira's eventually-consistent label indexing. Search
# results immediately after a label write can lag by 1-3 seconds.
_JQL_RETRY_COUNT = 3
_JQL_RETRY_SLEEP_S = 2


@dataclass
class StepResult:
    name: str
    ok: bool
    message: str
    details: dict = field(default_factory=dict)


def _load_acli_client():
    """Load AcliClient from acli-integration.py in the scripts directory."""
    here = Path(__file__).parent
    # Navigate to the scripts directory (one level up from rebar_reconciler/)
    acli_path = here.parent / "acli-integration.py"
    spec = importlib.util.spec_from_file_location("acli_integration", acli_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load acli-integration from {acli_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AcliClient


def run() -> StepResult:
    """Exercise 4 identity-critical Jira sub-operations on a throwaway issue."""
    jira_url = os.environ.get("JIRA_URL", "")
    jira_user = os.environ.get("JIRA_USER", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    # Project is configurable via env var for plugin portability; default
    # preserves the in-tree DIG project for the dso bridge use case.
    # `or "DIG"` (not the second arg to .get) so an explicit empty-string
    # JIRA_PROJECT="" — common when a templated secret renders blank — falls
    # back to the default rather than being passed through as an empty key.
    jira_project = os.environ.get("JIRA_PROJECT") or "DIG"

    if not (jira_url and jira_user and jira_token):
        return StepResult(
            name="forward_compat_probe",
            ok=False,
            message="missing Jira credentials (JIRA_URL, JIRA_USER, JIRA_API_TOKEN)",
        )

    probe_uuid = str(uuid.uuid4())
    label = f"rebar-id:{probe_uuid}"
    issue_key = None
    sub_ops: list[dict] = []

    try:
        AcliClient = _load_acli_client()
        client = AcliClient(
            jira_url=jira_url,
            user=jira_user,
            api_token=jira_token,
            jira_project=jira_project,
        )

        # Create throwaway issue
        result = client.create_issue({
            "title": f"DSO forward-compat probe {probe_uuid}",
            "ticket_type": "task",
        })
        issue_key = result.get("key") or result.get("id")
        if not issue_key:
            return StepResult(
                name="forward_compat_probe",
                ok=False,
                message=f"create_issue returned no key/id: {result!r}",
                details={"sub_operations": sub_ops},
            )

        # Sub-op 1: label_write (raw PUT — issue updates take {"update": ...}, not {"value": ...})
        try:
            client._direct_rest_put_raw(
                f"/rest/api/3/issue/{issue_key}",
                {"update": {"labels": [{"add": label}]}},
            )
            sub_ops.append({"op": "label_write", "ok": True})
        except Exception as exc:
            sub_ops.append({"op": "label_write", "ok": False, "error": str(exc)})
            return StepResult(
                name="forward_compat_probe",
                ok=False,
                message=f"FAIL label_write: {exc}",
                details={"sub_operations": sub_ops},
            )

        # Sub-op 2: property_write
        try:
            client.set_issue_property(issue_key, "dso_local_id", probe_uuid)
            sub_ops.append({"op": "property_write", "ok": True})
        except Exception as exc:
            sub_ops.append({"op": "property_write", "ok": False, "error": str(exc)})
            return StepResult(
                name="forward_compat_probe",
                ok=False,
                message=f"FAIL property_write: {exc}",
                details={"sub_operations": sub_ops},
            )

        # Sub-op 3: jql_search — with retry/backoff for Jira's
        # eventually-consistent label indexing. The try/except is INSIDE the
        # retry loop so transient HTTP 5xx (Atlassian's dominant
        # label-indexing-lag failure mode) also triggers backoff, not just
        # the narrower 200-OK-with-empty-results case. The except is narrowed
        # to transport-class exceptions so programming errors (AttributeError,
        # TypeError) fail fast rather than burning N×sleep on a deterministic
        # defect; an outer broad-Exception handler at the function level
        # still catches genuinely-unexpected failures.
        found = False
        attempts = 0
        last_error: str | None = None
        for _attempt in range(_JQL_RETRY_COUNT):
            attempts = _attempt + 1
            try:
                results = client.search_issues(f'labels="{label}"')
                if any(r.get("key") == issue_key for r in results):
                    found = True
                    # Only clear last_error when we genuinely located the
                    # issue. A successful-but-empty result on attempt N
                    # must NOT erase the diagnostic from a 5xx on attempt
                    # N-1 — operators need the earlier error to triage.
                    last_error = None
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                # Transport-class failures: capture and retry.
                last_error = str(exc)
            if _attempt < _JQL_RETRY_COUNT - 1:
                time.sleep(_JQL_RETRY_SLEEP_S)
        entry: dict = {"op": "jql_search", "ok": found, "attempts": attempts}
        if last_error is not None:
            entry["error"] = last_error
        sub_ops.append(entry)
        if not found:
            if last_error is not None:
                msg = f"FAIL jql_search: {last_error} (after {attempts} attempt(s))"
            else:
                msg = (
                    f"FAIL jql_search: issue key not found in label search "
                    f"results after {attempts} attempt(s)"
                )
            return StepResult(
                name="forward_compat_probe",
                ok=False,
                message=msg,
                details={"sub_operations": sub_ops},
            )

        # Sub-op 4: property_rest_read
        try:
            value = client.get_issue_property(issue_key, "dso_local_id")
            match = value == probe_uuid
            sub_ops.append({"op": "property_rest_read", "ok": match})
            if not match:
                return StepResult(
                    name="forward_compat_probe",
                    ok=False,
                    message=f"FAIL property_rest_read: expected {probe_uuid!r}, got {value!r}",
                    details={"sub_operations": sub_ops},
                )
        except Exception as exc:
            sub_ops.append({"op": "property_rest_read", "ok": False, "error": str(exc)})
            return StepResult(
                name="forward_compat_probe",
                ok=False,
                message=f"FAIL property_rest_read: {exc}",
                details={"sub_operations": sub_ops},
            )

        return StepResult(
            name="forward_compat_probe",
            ok=True,
            message="all 4 sub-operations passed",
            details={"sub_operations": sub_ops},
        )

    except Exception as exc:
        # Outer broad-Exception guard: per the sub-op 3 narrowing rationale,
        # programming errors (AttributeError, TypeError) and other
        # genuinely-unexpected failures fall through here and surface as a
        # structured StepResult rather than crashing the orchestrator.
        return StepResult(
            name="forward_compat_probe",
            ok=False,
            message=f"unexpected error: {exc!r}",
            details={"sub_operations": sub_ops},
        )

    finally:
        if issue_key:
            try:
                AcliClient = _load_acli_client()
                # Match the main-path constructor: pass jira_project so the
                # cleanup client targets the same project as the issue was
                # created in (relevant for any future delete_issue codepath
                # that consults self.jira_project).
                client = AcliClient(
                    jira_url=jira_url,
                    user=jira_user,
                    api_token=jira_token,
                    jira_project=jira_project,
                )
                client.delete_issue(issue_key)
            except Exception:
                pass
