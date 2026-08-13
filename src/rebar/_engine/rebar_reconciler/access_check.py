"""Structured Jira bridge access check shared by CLI, library, and MCP."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from rebar_reconciler.adapters.jira.acli import AcliClient

JQL_RETRY_COUNT = 6
JQL_RETRY_SLEEP = 5


def _step(
    steps: list[dict[str, object]],
    lines: list[str],
    name: str,
    passed: bool,
    *,
    reason: str | None = None,
    detail: object | None = None,
) -> None:
    item: dict[str, object] = {"step": name, "passed": passed}
    if reason is not None:
        item["reason"] = reason
    if detail is not None:
        item["detail"] = str(detail)
    steps.append(item)
    if passed:
        lines.append(f"PROBE_PASS step={name}")
        return
    suffix = f" reason={reason}" if reason is not None else ""
    if detail is not None:
        suffix += f" detail={detail}"
    lines.append(f"PROBE_FAIL step={name}{suffix}")


def _retry_sleep(seconds: float) -> None:
    """The default JQL-retry sleeper: a patchable indirection over ``time.sleep``.

    Deliberately a named function rather than ``sleep_fn=time.sleep`` in the
    signature. A default expression is evaluated ONCE at import, capturing whatever
    ``time.sleep`` was then — so whether a test's ``time.sleep`` patch reached this
    retry depended on whether this module happened to be imported first, and the JQL
    loop really slept 5x5s whenever it did (ticket 5ea3-76e5-480a-4464). This
    indirection resolves ``time.sleep`` at CALL time, so the seam is reliably
    patchable while production behaviour is byte-identical. Mirrors
    ``acli_subprocess._backoff_sleep``, which exists for the same reason.
    """
    time.sleep(seconds)


def run_access_check(
    *,
    env: dict[str, str] | None = None,
    client_cls=AcliClient,
    sleep_fn=_retry_sleep,
) -> tuple[dict[str, object], list[str], int]:
    """Run the six-step probe and return its result, legacy lines, and exit code."""
    if env is None:
        jira_url = os.environ.get("JIRA_URL", "")
        jira_user = os.environ.get("JIRA_USER", "")
        jira_api_token = os.environ.get("JIRA_API_TOKEN", "")
        jira_project = os.environ.get("JIRA_PROJECT") or "DIG"
    else:
        jira_url = env.get("JIRA_URL", "")
        jira_user = env.get("JIRA_USER", "")
        jira_api_token = env.get("JIRA_API_TOKEN", "")
        jira_project = env.get("JIRA_PROJECT") or "DIG"
    if not jira_url or not jira_user or not jira_api_token:
        return (
            {"verdict": "INVALID", "steps": [], "reason": "missing_credentials"},
            ["PROBE_FAIL reason=missing_credentials"],
            2,
        )

    probe_uuid = str(uuid.uuid4())
    label = f"rebar-id:{probe_uuid}"
    client = client_cls(
        jira_url=jira_url,
        user=jira_user,
        api_token=jira_api_token,
        jira_project=jira_project,
    )
    issue_key: str | None = None
    failed = False
    steps: list[dict[str, object]] = []
    lines: list[str] = []
    current_step = "STEP_CREATE"

    try:
        result = client.create_issue(
            {"title": f"rebar capability probe {probe_uuid}", "ticket_type": "task"}
        )
        issue_key = result.get("key") or result.get("id")
        if not issue_key:
            _step(
                steps, lines, current_step, False, reason="no_key_in_response", detail=repr(result)
            )
            failed = True
        else:
            _step(steps, lines, current_step, True)
            current_step = "STEP_LABEL"
            client._direct_rest_put_raw(
                f"/rest/api/3/issue/{issue_key}",
                {"update": {"labels": [{"add": label}]}},
            )
            _step(steps, lines, current_step, True)

            current_step = "STEP_PROPERTY_WRITE"
            client.set_issue_property(issue_key, "local_id", probe_uuid)
            _step(steps, lines, current_step, True)

            current_step = "STEP_JQL_SEARCH"
            jql = f'labels="{label}"'
            results: list[Any] = []
            for attempt in range(JQL_RETRY_COUNT):
                cache = getattr(client, "_search_cache", None)
                if isinstance(cache, dict):
                    cache.pop(jql, None)
                results = client.search_issues(jql)
                if results:
                    break
                if attempt < JQL_RETRY_COUNT - 1:
                    sleep_fn(JQL_RETRY_SLEEP)
            if results:
                _step(steps, lines, current_step, True)
            else:
                _step(steps, lines, current_step, False, reason="no_results_after_retry")
                failed = True

            current_step = "STEP_PROPERTY_READ"
            try:
                read_value = client.get_issue_property(issue_key, "local_id")
            except KeyError as exc:
                _step(steps, lines, current_step, False, reason="malformed_response", detail=exc)
                failed = True
            else:
                if read_value == probe_uuid:
                    _step(steps, lines, current_step, True)
                else:
                    detail = f"expected={probe_uuid} got={read_value}"
                    steps.append(
                        {
                            "step": current_step,
                            "passed": False,
                            "reason": "value_mismatch",
                            "detail": detail,
                        }
                    )
                    lines.append(f"PROBE_FAIL step={current_step} reason=value_mismatch {detail}")
                    failed = True
    except Exception as exc:  # noqa: BLE001 - the probe reports provider failures in-band
        steps.append(
            {"step": current_step, "passed": False, "reason": "exception", "detail": str(exc)}
        )
        lines.append(f"PROBE_FAIL reason=exception detail={exc}")
        failed = True
    finally:
        if issue_key is not None:
            try:
                client.delete_issue(issue_key)
                _step(steps, lines, "STEP_DELETE", True)
            except Exception as exc:  # noqa: BLE001 - cleanup is part of the probe verdict
                _step(steps, lines, "STEP_DELETE", False, reason="exception", detail=exc)
                failed = True

    verdict = "FAIL" if failed else "PASS"
    return {"verdict": verdict, "steps": steps}, lines, 1 if failed else 0
