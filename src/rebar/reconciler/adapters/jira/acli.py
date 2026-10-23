#!/usr/bin/env python3
"""ACLI subprocess wrapper for Jira issue operations.

Provides create_issue, update_issue, and get_issue functions that invoke
the Atlassian CLI (ACLI) via subprocess calls. Includes retry with
exponential backoff on transient failures and fast-abort on auth errors.

No external dependencies — stdlib only (subprocess, json, time, os, base64, urllib).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable, Iterator
from itertools import islice
from typing import Any

from rebar.reconciler.adapters.jira import acli_cli_ops, acli_subprocess

# Module-level ACLI issue ops live in acli_cli_ops; the AcliClient methods
# delegate to them via ``acli_cli_ops.<name>``. Only the private helpers below
# are pulled in by name for internal use.
from rebar.reconciler.adapters.jira.acli_cli_ops import (
    _attach_parent_guarded,
    _create_from_json_payload,
    _create_issue_from_json,
    _create_issue_no_json,
    _extract_parent_key,
    _parse_paginated_comments,
    _verify_created_issue,
)
from rebar.reconciler.adapters.jira.acli_graph import AcliGraphMixin, _iter_cursor_pages
from rebar.reconciler.adapters.jira.acli_rest import AcliRestMixin

# Subprocess transport floor (process exec + retry + typed mutation errors) lives
# in acli_subprocess; re-exported here so acli.<name> keeps resolving. The seam
# is reached MODULE-QUALIFIED (acli_subprocess._run_acli) at the call sites below
# so one patch point covers AcliClient._run + the acli_cli_ops free functions.
from rebar.reconciler.adapters.jira.acli_subprocess import (
    _ASSIGNEE_NOT_FOUND_ERROR,
    _ASSIGNEE_PERMISSION_ERROR,
    _AUTH_FAILURE_CODE,
    _DEFAULT_ACLI_CMD,
    _MAX_ATTEMPTS,
    AcliAuthError,
    AcliMutationError,
    AcliTimeoutError,
    AssigneeNotFoundError,
    RetryExhaustedError,
    _build_env,
    _check_mutation_failure,
    _run_acli,
    resolve_jira_settings,
)
from rebar.reconciler.adapters.jira.adf import text_to_adf as _text_to_adf  # canonical location

# Field sanitization + local↔Jira value maps live in the Jira vendor adapter
# (``adapters/jira/jira_fields.py``, relocated by ticket 44be); re-exported here
# so ``acli.<name>`` keeps resolving for callers and the characterization suites
# (point-of-use read access).
from rebar.reconciler.adapters.jira.jira_fields import (
    _sanitize_description,
)
from rebar.reconciler.adapters.jira_family import JIRA_LABEL_MAX_CHARS as _JIRA_LABEL_MAX_CHARS
from rebar.reconciler.adapters.jira_family import JIRA_SUMMARY_MAX_CHARS as _JIRA_SUMMARY_MAX_CHARS
from rebar.reconciler.adapters.jira_family import (
    LOCAL_PRIORITY_TO_JIRA as _LOCAL_PRIORITY_TO_JIRA,
)
from rebar.reconciler.adapters.jira_family import LOCAL_STATUS_TO_JIRA as _LOCAL_STATUS_TO_JIRA
from rebar.reconciler.adapters.jira_family import (
    InvalidLabelError,
)
from rebar.reconciler.adapters.jira_family import sanitize_label as _sanitize_label
from rebar.reconciler.adapters.jira_family import sanitize_summary as _sanitize_summary

# Re-export facade. These names are imported from the sibling acli_* modules and
# the Jira vendor adapter (adapters/jira/jira_fields) solely so ``acli.<name>``
# keeps resolving for callers and the characterization suites; ``__all__`` records
# them as intentional re-exports.
__all__ = [
    "_ASSIGNEE_NOT_FOUND_ERROR",
    "_ASSIGNEE_PERMISSION_ERROR",
    "_AUTH_FAILURE_CODE",
    "_DEFAULT_ACLI_CMD",
    "_JIRA_LABEL_MAX_CHARS",
    "_JIRA_SUMMARY_MAX_CHARS",
    "_MAX_ATTEMPTS",
    "AcliAuthError",
    "AcliMutationError",
    "AcliTimeoutError",
    "AssigneeNotFoundError",
    "InvalidLabelError",
    "RetryExhaustedError",
    "_attach_parent_guarded",
    "_build_env",
    "_check_mutation_failure",
    "_create_from_json_payload",
    "_create_issue_from_json",
    "_create_issue_no_json",
    "_run_acli",
    "_sanitize_label",
    "_verify_created_issue",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADF helpers
# ---------------------------------------------------------------------------


# _text_to_adf is imported from rebar.reconciler.adapters.jira.adf (canonical location)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transition_issue(
    jira_key: str,
    status: str,
) -> dict[str, Any]:
    """Transition a Jira issue to *status* via REST (bug 85a1, Gap 8).

    Status changes go through ``POST /rest/api/3/issue/{key}/transitions``,
    NOT ACLI's ``workitem transition``. ACLI's transition subcommand exits
    0 even when the transition is rejected (Gap 5 — the lying-success bug);
    REST surfaces failures as HTTP 4xx/5xx, which propagate as
    ``urllib.error.HTTPError`` for the caller.

    *status* may be either a local-side name (``in_progress``, ``open``)
    or a Jira-side name (``In Progress``, ``To Do``). The former is mapped
    via ``_LOCAL_STATUS_TO_JIRA``; the latter is passed through. The
    ``transition_issue_by_name`` method on ``AcliClient`` then matches
    case-insensitively against each transition's ``name`` and ``to.name``,
    so workflows that use ``Move to <state>`` transition names are handled.

    Returns ``{"key": jira_key, "status": <resolved_name>}`` on success.
    """
    resolved = _LOCAL_STATUS_TO_JIRA.get(status, status.replace("_", " ").title())
    _s = resolve_jira_settings()
    client = AcliClient(jira_url=_s.url, user=_s.user, api_token=_s.api_token)
    client.transition_issue_by_name(jira_key, resolved)
    return {"key": jira_key, "status": resolved}


def update_issue(
    jira_key: str,
    *,
    acli_cmd: list[str] | None = None,
    call_timeout: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Update a Jira issue via ACLI.

    If ``status`` is in kwargs, it is routed to ``transition_issue``
    (Jira status changes require transitions, not field edits).
    Remaining fields are sent via ``workitem edit``.

    **Priority**: ACLI does not support editing priority via CLI flags.
    Priority updates are routed to ``update_priority()`` which uses the
    REST API directly (PUT /rest/api/3/issue/{key}).
    """
    status = kwargs.pop("status", None)
    priority = kwargs.pop("priority", None)
    if priority is not None:
        # Resolve priority to a Jira name string, then update via REST.
        if isinstance(priority, int):
            priority_name = _LOCAL_PRIORITY_TO_JIRA.get(priority, "Medium")
        elif isinstance(priority, dict):
            priority_name = priority.get("name") or "Medium"
        else:
            priority_name = str(priority)
        # No acli_cmd: this call spawns no ACLI subprocess, so an ACLI argv prefix
        # is inapplicable. It resolves its own Jira target — see its docstring.
        acli_cli_ops.update_priority(jira_key, priority_name)

    if status is not None:
        transition_issue(jira_key, status)

    if not kwargs:
        # No editable fields remain (status/priority were already handled above)
        if status is not None:
            return {"key": jira_key, "status": status}
        return {"key": jira_key}

    cmd = [
        "jira",
        "workitem",
        "edit",
        "--key",
        jira_key,
        "--json",
    ]
    for field, value in kwargs.items():
        if field == "description":
            # Convert description to ADF (same as create_issue) — Jira REST API
            # v3 requires ADF format for description fields. Truncate first to
            # Jira's 32,767-char limit so an over-length description does not abort
            # the pass (bug 626d follow-up).
            cmd.extend([f"--{field}", json.dumps(_text_to_adf(_sanitize_description(str(value))))])
        else:
            cmd.extend([f"--{field}", str(value)])

    result = acli_subprocess._run_acli(
        cmd, acli_cmd=acli_cmd, retry_on_timeout=False, call_timeout=call_timeout
    )  # WRITE
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# AcliClient class — used by the rebar.reconciler bands (fetcher, applier,
# stale_band, open_count_skew_band) and the capability / forward-compat probes.
# ---------------------------------------------------------------------------


class _SearchResult:
    def __init__(self, pages: Iterator[dict[str, Any]]) -> None:
        self.pages = pages
        self.issues: list[dict[str, Any]] = []


def _iter_search_issues(post: Callable[[str, Any], Any], jql: str) -> Iterator[dict[str, Any]]:
    """Read only the cursor pages consumed by the caller, rejecting partial wire data."""

    def checked_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = post(path, body)
        if not isinstance(response, dict) or not isinstance(response.get("issues"), list):
            raise RuntimeError("Jira search returned a malformed issues page")
        if any(not isinstance(issue, dict) or not issue.get("key") for issue in response["issues"]):
            raise RuntimeError("Jira search returned a malformed issue")
        return response

    body = {
        "jql": jql,
        "maxResults": 100,
        "fields": [
            "issuetype",
            "key",
            "assignee",
            "priority",
            "status",
            "summary",
            "description",
            "labels",
        ],
    }
    for page in _iter_cursor_pages(checked_post, "/rest/api/3/search/jql", body):
        yield from page["issues"]


class AcliClient(AcliRestMixin, AcliGraphMixin):
    """Client wrapping ACLI Go binary for Jira operations.

    Provides the method interface consumed by the rebar.reconciler:
    create_issue, update_issue, delete_issue, get_issue, search_issues,
    get_myself, get_server_info, get_comments, set_relationship, plus
    per-issue property read/write helpers.

    Credentials are injected into the subprocess environment on each call
    so ACLI can authenticate without requiring prior ``acli auth`` setup.
    """

    def __init__(
        self,
        jira_url: str,
        user: str,
        api_token: str,
        *,
        jira_project: str = "",
        acli_cmd: list[str] | None = None,
        call_timeout: float = 0,
    ) -> None:
        self.jira_url = jira_url
        self.user = user
        self.api_token = api_token
        self.jira_project = jira_project
        self._acli_cmd = acli_cmd
        #: Per-call timeout in seconds; non-positive uses the ambient default.
        self._call_timeout: float | None = call_timeout if call_timeout > 0 else None
        # Cache JQL searches for this client's pass. Call
        # invalidate_search_cache before a same-pass freshness check.
        self._search_cache: dict[str, _SearchResult] = {}

    def invalidate_search_cache(self, jql: str | None = None) -> None:
        """Evict the search memo entry for *jql*, or the whole memo if None.

        The supported freshness door for the per-JQL memo in search_issues:
        callers that re-issue the same JQL expecting a fresh answer (index
        -visibility polls, retry loops) evict first instead of reaching into
        ``_search_cache``. Evicting a never-searched JQL is a no-op.
        """
        if jql is None:
            self._search_cache.clear()
        else:
            self._search_cache.pop(jql, None)

    def _run(
        self,
        cmd: list[str],
        *,
        retry_on_timeout: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run an ACLI command.

        ACLI Go reads auth from its config file (set by ``acli auth login``).
        Credentials stored on self are available for callers that need them
        (e.g., direct REST calls), but are not injected into the subprocess
        environment — ACLI does not read env vars for auth.

        ``retry_on_timeout`` (bug d843) forwards to ``_run_acli``: READS pass
        ``True`` (idempotent, safe to retry on timeout); WRITES leave the
        ``False`` default so a timed-out, possibly-committed mutation is never
        blind-retried against non-idempotent Jira.
        """
        return acli_subprocess._run_acli(
            cmd,
            acli_cmd=self._acli_cmd,
            retry_on_timeout=retry_on_timeout,
            call_timeout=self._call_timeout,
        )

    # --- Outbound API methods (local → Jira) ---

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        """Create a Jira issue from a ticket data dict.

        Uses self.jira_project as the project key, unless the create payload carries
        the reserved ``_bridge_target_project`` key (story d19d) — then the ticket's
        resolved project wins, so one client can create into any of the store's
        projects. Extracts ticket_type, title, description, priority, and assignee
        from ticket_data (matching the CREATE event data schema).
        """
        project = ticket_data.get("_bridge_target_project") or self.jira_project
        issue_type = ticket_data.get("ticket_type", "Task").capitalize()
        raw_summary = (ticket_data.get("title") or "").strip()
        if not raw_summary:
            raise ValueError(
                f"Cannot create Jira issue: title/summary is empty "
                f"(ticket_data keys: {list(ticket_data.keys())})"
            )
        # Defend against untrusted user input — truncate oversize titles
        # rather than crashing the reconciler pass on Jira's 255-char limit.
        summary = _sanitize_summary(raw_summary)
        optional_fields: dict[str, Any] = {}
        if ticket_data.get("description"):
            # Truncate to Jira's 32,767-char limit so an over-length description
            # does not abort the create (bug 626d follow-up).
            optional_fields["description"] = _sanitize_description(str(ticket_data["description"]))
        if ticket_data.get("priority") is not None:
            optional_fields["priority"] = ticket_data["priority"]
        if ticket_data.get("assignee"):
            # Resolve in project scope before create. Omit unassignable names;
            # let transient failures propagate for retry.
            try:
                optional_fields["assignee"] = self.validate_assignee_exists(
                    str(ticket_data["assignee"]), project_key=project
                )
            except AssigneeNotFoundError:
                pass  # unmappable → omit (leave unassigned), like the UPDATE path
        # Accept either a bare parent key or REST ``{"key": K}`` shape;
        # attach during create or through the from-JSON fallback.
        parent_key = _extract_parent_key(ticket_data.get("parent"))
        if parent_key:
            optional_fields["parent"] = parent_key
        return acli_cli_ops.create_issue(
            project,
            issue_type,
            summary,
            acli_cmd=self._acli_cmd,
            client=self,
            call_timeout=self._call_timeout,
            **optional_fields,
        )

    def update_issue(
        self, remote_id: str, *, assignee_is_account_id: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        """Update through ACLI, resolving or clearing the assignee first.

        ``assignee_is_account_id`` skips assignable-user lookup for a resolved
        ID. Empty values use REST unassign; its errors are logged so remaining
        fields still update. Other names are validated before dispatch.
        """
        if "assignee" in kwargs:
            if kwargs["assignee"] in (None, ""):
                kwargs.pop("assignee")
                try:
                    self.unassign_issue(remote_id)
                except Exception as exc:  # noqa: BLE001 — fail-open: unassign non-fatal, batch continues
                    print(  # noqa: T201
                        f"update_issue: unassign_issue({remote_id}) failed: {exc!r}",
                        file=sys.stderr,
                    )
            elif assignee_is_account_id:
                # 264f: already a resolved accountId — submit directly, no search.
                pass
            else:
                kwargs["assignee"] = self.validate_assignee_exists(
                    kwargs["assignee"], issue_key=remote_id
                )
        return update_issue(
            remote_id, acli_cmd=self._acli_cmd, call_timeout=self._call_timeout, **kwargs
        )

    def get_issue(self, jira_key: str) -> dict[str, Any]:
        """Get a Jira issue via ACLI."""
        return acli_cli_ops.get_issue(
            jira_key, acli_cmd=self._acli_cmd, call_timeout=self._call_timeout
        )

    def add_comment(self, jira_key: str, body: str) -> dict[str, Any]:
        """Add a comment to a Jira issue via ACLI."""
        return acli_cli_ops.add_comment(
            jira_key, body, acli_cmd=self._acli_cmd, call_timeout=self._call_timeout
        )

    def search_issues(
        self,
        jql: str,
        start_at: int = 0,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Return an offset slice, fetching REST cursor pages only as needed.

        Eager ACLI ``--paginate`` read the entire Done history before the fetcher's
        1,000-issue cap could stop it, exhausting the subprocess timeout (d730-8a25).
        Cache both consumed issues and the suspended cursor for this pass. Empty
        answers remain cached; callers needing fresh data must invalidate first.
        """
        if start_at < 0 or max_results < 0:
            raise ValueError("search offsets and limits must be non-negative")
        if max_results == 0:
            return []
        if jql not in self._search_cache:
            self._search_cache[jql] = _SearchResult(
                _iter_search_issues(self._direct_rest_post_json, jql)
            )
        cached = self._search_cache[jql]
        end = start_at + max_results
        try:
            cached.issues.extend(islice(cached.pages, max(0, end - len(cached.issues))))
        except Exception:
            # A raised generator is exhausted. Keeping it would turn a failed
            # later page into a successful, truncated result on the next read.
            self.invalidate_search_cache(jql)
            raise
        return cached.issues[start_at:end]

    def get_server_info(self) -> dict[str, Any]:
        """Get Jira server info for timezone verification.

        Jira Cloud always stores timestamps in UTC. The legacy Java ACLI
        needed a JVM timezone flag to avoid locale-dependent serialization;
        the Go ACLI has no such issue. Connectivity is already verified by
        the workflow's ``acli auth login`` step — a redundant API call here
        would add latency and a failure mode with no diagnostic value.
        """
        return {"timeZone": "UTC", "serverTitle": "Jira Cloud"}

    def get_comments(self, jira_key: str) -> list[dict[str, Any]]:
        """Get all comment pages and decode ACLI's concatenated JSON objects."""
        cmd = [
            "jira",
            "workitem",
            "comment",
            "list",
            "--key",
            jira_key,
            "--paginate",
            "--json",
        ]
        result = self._run(cmd, retry_on_timeout=True)  # READ — idempotent
        return _parse_paginated_comments(result.stdout)

    def delete_issue(
        self,
        jira_key: str,
    ) -> dict[str, Any]:
        """Delete a Jira issue via ACLI.

        Uses ``jira workitem delete --key KEY`` to permanently remove the issue.

        - 404 response (issue already gone) is treated as idempotent success.
        - 403 response (permission denied) raises ``PermissionError`` so callers
          can write a BRIDGE_ALERT and skip deletion without crashing.

        Raises:
            PermissionError: When ACLI exits with a 403 permission error.
            subprocess.CalledProcessError: On other ACLI failures (single attempt — no retry).
        """
        # Skip ACLI's interactive confirmation in non-TTY runs.
        cmd = [
            "jira",
            "workitem",
            "delete",
            "--key",
            jira_key,
            "--yes",
            # Detect structured failures even when ACLI exits 0.
            "--json",
        ]
        try:
            # Use bounded process-group cleanup. Do not retry an ambiguous
            # timed-out write; _run_acli checks structured failures.
            acli_subprocess._run_acli(
                cmd,
                acli_cmd=self._acli_cmd,
                retry_on_timeout=False,
                call_timeout=self._call_timeout,
            )
        except subprocess.CalledProcessError as exc:
            err_text = (exc.stderr or "") + (exc.stdout or "")
            if "404" in err_text or "not found" in err_text.lower():
                # Already deleted — idempotent success
                return {"status": "not_found", "key": jira_key}
            if "403" in err_text or "forbidden" in err_text.lower():
                msg = f"Permission denied deleting {jira_key}: {err_text.strip()}"
                raise PermissionError(msg) from exc
            raise
        return {"status": "deleted", "key": jira_key}
