#!/usr/bin/env python3
"""ACLI subprocess wrapper for Jira issue operations.

Provides create_issue, update_issue, and get_issue functions that invoke
the Atlassian CLI (ACLI) via subprocess calls. Includes retry with
exponential backoff on transient failures and fast-abort on auth errors.

No external dependencies — stdlib only (subprocess, json, time, os, base64, urllib).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from rebar_reconciler import acli_cli_ops, acli_subprocess
from rebar_reconciler.acli_rest import AcliRestMixin
from rebar_reconciler.adf import text_to_adf as _text_to_adf  # canonical location

# Subprocess transport floor (process exec + retry + typed mutation errors) lives
# in acli_subprocess; re-exported here so acli.<name> keeps resolving. The seam
# is reached MODULE-QUALIFIED (acli_subprocess._run_acli) at the call sites below
# so one patch point covers AcliClient._run + the acli_cli_ops free functions.
from rebar_reconciler.acli_subprocess import (
    _ASSIGNEE_NOT_FOUND_ERROR,
    _ASSIGNEE_PERMISSION_ERROR,
    _AUTH_FAILURE_CODE,
    _DEFAULT_ACLI_CMD,
    _MAX_ATTEMPTS,
    _RETRYABLE_HTTP_CODES,
    AcliMutationError,
    AssigneeNotFoundError,
    RetryExhaustedError,
    _build_env,
    _call_with_backoff,
    _check_mutation_failure,
    _run_acli,
)

# Module-level ACLI issue ops live in acli_cli_ops; the AcliClient methods
# delegate to them. Re-exported so acli.<name> keeps resolving for callers.
from rebar_reconciler.acli_cli_ops import (
    _attach_parent_guarded,
    _create_from_json_payload,
    _create_issue_from_json,
    _create_issue_no_json,
    _extract_parent_key,
    _parse_acli_comments,
    _verify_created_issue,
    add_comment,
    create_issue,
    get_comments,
    get_issue,
    update_priority,
)

# Field sanitization + local↔Jira value maps live in jira_fields; re-exported
# here so ``acli.<name>`` keeps resolving for callers and the characterization
# suites (point-of-use read access).
from rebar_reconciler.jira_fields import (
    InvalidLabelError,
    _JIRA_LABEL_MAX_CHARS,
    _JIRA_SUMMARY_MAX_CHARS,
    _LOCAL_PRIORITY_TO_JIRA,
    _LOCAL_STATUS_TO_JIRA,
    _sanitize_comment,
    _sanitize_label,
    _sanitize_summary,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADF helpers
# ---------------------------------------------------------------------------


# _text_to_adf is imported from rebar_reconciler.adf (canonical location)



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transition_issue(
    jira_key: str,
    status: str,
    *,
    acli_cmd: list[str] | None = None,
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
    The ``acli_cmd`` argument is accepted for backward compatibility but
    is no longer used.
    """
    resolved = _LOCAL_STATUS_TO_JIRA.get(status, status.replace("_", " ").title())
    client = AcliClient(
        jira_url=os.environ.get("JIRA_URL", ""),
        user=os.environ.get("JIRA_USER", ""),
        api_token=os.environ.get("JIRA_API_TOKEN", ""),
    )
    client.transition_issue_by_name(jira_key, resolved)
    return {"key": jira_key, "status": resolved}


def update_issue(
    jira_key: str,
    *,
    acli_cmd: list[str] | None = None,
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
        acli_cli_ops.update_priority(jira_key, priority_name, acli_cmd=acli_cmd)

    if status is not None:
        transition_issue(jira_key, status, acli_cmd=acli_cmd)

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
            # v3 requires ADF format for description fields.
            cmd.extend([f"--{field}", json.dumps(_text_to_adf(str(value)))])
        else:
            cmd.extend([f"--{field}", str(value)])

    result = acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd)
    return json.loads(result.stdout)



# ---------------------------------------------------------------------------
# AcliClient class — used by the rebar_reconciler bands (fetcher, applier,
# stale_band, open_count_skew_band) and the capability / forward-compat probes.
# ---------------------------------------------------------------------------


class AcliClient(AcliRestMixin):
    """Client wrapping ACLI Go binary for Jira operations.

    Provides the method interface consumed by the rebar_reconciler:
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
    ) -> None:
        self.jira_url = jira_url
        self.user = user
        self.api_token = api_token
        self.jira_project = jira_project
        self._acli_cmd = acli_cmd

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Run an ACLI command.

        ACLI Go reads auth from its config file (set by ``acli auth login``).
        Credentials stored on self are available for callers that need them
        (e.g., direct REST calls), but are not injected into the subprocess
        environment — ACLI does not read env vars for auth.
        """
        return acli_subprocess._run_acli(cmd, acli_cmd=self._acli_cmd)

    # --- Outbound API methods (local → Jira) ---

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        """Create a Jira issue from a ticket data dict.

        Uses self.jira_project as the project key. Extracts ticket_type,
        title, description, priority, and assignee from ticket_data
        (matching the CREATE event data schema).
        """
        project = self.jira_project
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
            optional_fields["description"] = ticket_data["description"]
        if ticket_data.get("priority") is not None:
            optional_fields["priority"] = ticket_data["priority"]
        if ticket_data.get("assignee"):
            optional_fields["assignee"] = ticket_data["assignee"]
        # Parent sync (ticket 8b25): outbound_differ emits the resolved Jira
        # parent key into the create payload. The differ writes a BARE string
        # (``_map_local_to_jira_fields`` sets ``result["parent"] = jira_key``),
        # but accept the Jira REST nested shape ``{"key": K}`` too so a future
        # differ change does not silently drop the parent. create_issue then
        # attaches the parent at create time (--parent) or via set_parent
        # fallback (--from-json path). Previously dropped silently — bug 8b25.
        parent_key = _extract_parent_key(ticket_data.get("parent"))
        if parent_key:
            optional_fields["parent"] = parent_key
        return acli_cli_ops.create_issue(
            project,
            issue_type,
            summary,
            acli_cmd=self._acli_cmd,
            client=self,
            **optional_fields,
        )

    def update_issue(self, jira_key: str, **kwargs: Any) -> dict[str, Any]:
        """Update a Jira issue via ACLI.

        Bug 85a1 (Fix D7): assignee=None/empty is routed through
        ``unassign_issue`` (REST PUT /assignee with ``{"accountId": null}``)
        rather than passed to ACLI as ``--assignee ""``, which ACLI silently
        no-ops (the probe Phase 2 verify-assignee-unassigned regression).

        Bug 06a5 (Gap 5 follow-up): non-empty assignee values are pre-validated
        against ``/rest/api/3/user/assignable/search`` and normalised to the
        matched ``accountId`` before the ACLI dispatch. Bogus assignees raise
        ``AssigneeNotFoundError`` here rather than silently no-op via ACLI's
        exit-0-on-failure contract.

        unassign_issue failures are caught and logged so a transient REST
        error does not abort the entire batch — the rest of the update_one
        body (label/comment dispatch, field edits) must still run.
        """
        if "assignee" in kwargs:
            if kwargs["assignee"] in (None, ""):
                kwargs.pop("assignee")
                try:
                    self.unassign_issue(jira_key)
                except Exception as exc:  # noqa: BLE001
                    print(  # noqa: T201
                        f"update_issue: unassign_issue({jira_key}) failed: {exc!r}",
                        file=sys.stderr,
                    )
            else:
                kwargs["assignee"] = self.validate_assignee_exists(
                    kwargs["assignee"], issue_key=jira_key
                )
        return update_issue(jira_key, acli_cmd=self._acli_cmd, **kwargs)

    def get_issue(self, jira_key: str) -> dict[str, Any]:
        """Get a Jira issue via ACLI."""
        return acli_cli_ops.get_issue(jira_key, acli_cmd=self._acli_cmd)

    def get_issue_by_rest(self, jira_key: str) -> dict[str, Any]:
        """Get a Jira issue via direct REST GET (immediately consistent).

        Unlike get_issue (which uses ACLI's JQL search internally), this
        hits GET /rest/api/3/issue/{key} which reads from the primary store
        and is not subject to Jira Cloud's search index lag.
        """
        path = f"/rest/api/3/issue/{jira_key}"
        return self._direct_rest_get(path)

    def add_comment(self, jira_key: str, body: str) -> dict[str, Any]:
        """Add a comment to a Jira issue via ACLI."""
        return acli_cli_ops.add_comment(jira_key, body, acli_cmd=self._acli_cmd)

    def get_issue_link_types(self) -> list[dict[str, Any]]:
        """Return all available Jira issue link types via ACLI.

        Uses ``jira workitem link type list --json`` to query Jira for the
        full set of configured link types. Returns a list of dicts, each
        containing at minimum ``id`` and ``name`` fields (plus ``inward``
        and ``outward`` when the ACLI response includes them).

        Raises subprocess.CalledProcessError on ACLI failure.
        """
        cmd = [
            "jira",
            "workitem",
            "link",
            "type",
            "list",
            "--json",
        ]
        result = self._run(cmd)
        parsed = json.loads(result.stdout or "[]")
        if isinstance(parsed, list):
            return parsed
        # Some ACLI versions wrap the list in a dict under "issueLinkTypes"
        if isinstance(parsed, dict) and "issueLinkTypes" in parsed:
            return parsed["issueLinkTypes"]
        return []

    def search_issues(
        self,
        jql: str,
        start_at: int = 0,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Search Jira issues via JQL, returning a page slice.

        ACLI Go has no offset flag, so --paginate fetches all results in one
        call. Results are cached per-JQL to avoid redundant fetches when the
        caller paginates. Returns a slice of ``[start_at:start_at+max_results]``
        to satisfy the reconciler's pagination loop contract.
        """
        # Cache the full result set for this JQL to avoid re-fetching
        if not hasattr(self, "_search_cache"):
            self._search_cache: dict[str, list[dict[str, Any]]] = {}

        if jql not in self._search_cache:
            cmd = [
                "jira",
                "workitem",
                "search",
                "--jql",
                jql,
                "-f",
                # Bug 5328: ``labels`` MUST be in this list. Without it the
                # batch snapshot has labels=[] for every issue, which makes
                # both differs hallucinate divergence symmetrically (outbound
                # emits ADD-every-tag, inbound emits REMOVE-every-tag, and
                # bidir suppression cancels them out). Any Jira-side label
                # ADD on a bound ticket then becomes invisible to inbound
                # because the snapshot pretends Jira has no labels at all.
                # Mirrors the single-issue ``get_issue`` field list above.
                "issuetype,key,assignee,priority,status,summary,description,labels",
                "--paginate",
                "--json",
            ]
            result = self._run(cmd)
            parsed = json.loads(result.stdout)
            if isinstance(parsed, list):
                all_issues = parsed
            elif isinstance(parsed, dict) and "issues" in parsed:
                all_issues = parsed["issues"]
            else:
                logging.warning(
                    "search_issues: unexpected ACLI JSON shape (type=%s); "
                    "treating as empty result. Response prefix: %.200r",
                    type(parsed).__name__,
                    parsed,
                )
                all_issues = []
            self._search_cache[jql] = all_issues

        all_issues = self._search_cache[jql]
        return all_issues[start_at : start_at + max_results]

    def get_server_info(self) -> dict[str, Any]:
        """Get Jira server info for timezone verification.

        Jira Cloud always stores timestamps in UTC. The legacy Java ACLI
        needed a JVM timezone flag to avoid locale-dependent serialization;
        the Go ACLI has no such issue. Connectivity is already verified by
        the workflow's ``acli auth login`` step — a redundant API call here
        would add latency and a failure mode with no diagnostic value.
        """
        return {"timeZone": "UTC", "serverTitle": "Jira Cloud"}

    def get_myself(self) -> dict[str, Any]:
        """Return the authenticated user's Jira profile via GET /rest/api/2/myself.

        Used to retrieve the service account's profile timezone, which Jira Cloud
        uses when interpreting unqualified JQL datetime strings. Cached per instance.
        """
        if hasattr(self, "_myself_cache"):
            return self._myself_cache  # type: ignore[return-value]
        url = f"{self.jira_url.rstrip('/')}/rest/api/2/myself"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Basic {creds}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self._myself_cache: dict[str, Any] = json.loads(
                    resp.read().decode("utf-8")
                )
        except (urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logging.warning("get_myself: failed to fetch /rest/api/2/myself: %s", exc)
            # missing keys gracefully (defaulting to UTC), and caching prevents a
            # second network failure on the same run from the verify+fetch double-call.
            self._myself_cache = {}
        return self._myself_cache








    def add_label(self, jira_key: str, label: str) -> None:
        # Sanitize before reaching ACLI so we fail fast on invalid labels rather
        # than emitting a malformed mutation against live Jira.
        label = _sanitize_label(label)
        return self._add_label_impl(jira_key, label)

    def _add_label_impl(self, jira_key: str, label: str) -> None:
        """Additively add a label to a Jira issue via ACLI workitem edit.

        Uses ``acli jira workitem edit --from-json <file> --yes`` with payload
        ``{"issues": ["<KEY>"], "labelsToAdd": ["<label>"]}``. The ``labelsToAdd``
        operation is ADDITIVE — existing labels are preserved (verified live
        against DIG-3802 2026-05-24 per bug c916-74a1-ed06-40e4).

        Per ACLI v1.3.18:
          - The singular ``--label`` flag DOES NOT EXIST and is rejected with
            'unknown flag: --label'.
          - The plural ``--labels`` flag is a SET-REPLACE — passing
            ``--labels foo`` clobbers all existing labels, leaving only ``foo``.
            That semantic is incompatible with the reconciler's conflict policy
            ('additive content merged inbound: labels added') because it would
            destroy Jira-only labels on every rebar-id stamp.
          - The ``--from-json`` payload schema (exposed via
            ``acli jira workitem edit --generate-json``) includes
            ``labelsToAdd`` and ``labelsToRemove`` as the documented additive
            operations. This is the correct surface.
          - ``--from-json`` writes require ``--yes`` to skip the interactive
            'You're about to edit N work item(s). (y/N)' prompt.

        The ``--from-json`` path is single-call (no read-then-write race) and
        idempotent at the ACLI layer — calling with a label that already
        exists on the issue succeeds silently.
        """
        payload = {"issues": [jira_key], "labelsToAdd": [label]}
        fd, json_path = tempfile.mkstemp(suffix=".json", prefix="acli-edit-")
        fd_owned = False
        try:
            with os.fdopen(fd, "w") as f:
                fd_owned = True
                json.dump(payload, f)
        except Exception:
            if not fd_owned:
                os.close(fd)
            raise
        try:
            # Bug 44de: --json so _run_acli can parse the structured-failure
            # shape and raise AcliMutationError on exit=0 + FAILURE result.
            cmd = [
                "jira",
                "workitem",
                "edit",
                "--from-json",
                json_path,
                "--yes",
                "--json",
            ]
            self._run(cmd)
        finally:
            os.unlink(json_path)

    def remove_label(self, jira_key: str, label: str) -> None:
        # Sanitize so we reject obviously-malformed label values before issuing
        # the mutation. ACLI may accept invalid labels silently in remove mode.
        label = _sanitize_label(label)
        return self._remove_label_impl(jira_key, label)

    def _remove_label_impl(self, jira_key: str, label: str) -> None:
        """Additively remove a label from a Jira issue via ACLI workitem edit.

        Counterpart to ``add_label``. Uses ``--from-json`` with the
        ``labelsToRemove`` operation, which is target-specific — only the
        named label is removed; all other labels are preserved. Verified
        live against DIG-3802 2026-05-24 per bug c916-74a1-ed06-40e4.

        Idempotent at the ACLI layer — calling with a label that does not
        exist on the issue succeeds silently.
        """
        payload = {"issues": [jira_key], "labelsToRemove": [label]}
        fd, json_path = tempfile.mkstemp(suffix=".json", prefix="acli-edit-")
        fd_owned = False
        try:
            with os.fdopen(fd, "w") as f:
                fd_owned = True
                json.dump(payload, f)
        except Exception:
            if not fd_owned:
                os.close(fd)
            raise
        try:
            # Bug 44de: --json for structured-failure detection (see add_label).
            cmd = [
                "jira",
                "workitem",
                "edit",
                "--from-json",
                json_path,
                "--yes",
                "--json",
            ]
            self._run(cmd)
        finally:
            os.unlink(json_path)



    def transition_issue_by_name(self, jira_key: str, target_status: str) -> None:
        """Transition a Jira issue to *target_status* via REST.

        Bug 85a1 (Gap 8): replaces the previous ACLI-based ``transition_issue``
        which silently exited 0 on bogus transitions (Gap 5). Uses direct
        REST so HTTP status codes reliably surface failure:

          1. GET /rest/api/3/issue/{key}/transitions to list available
          2. Match *target_status* (case-insensitive) against each
             transition's ``name`` first, then ``to.name``. Workflows that
             use "Move to <state>" transition names with a distinct
             target-state name are handled by the ``to.name`` fallback.
          3. POST /rest/api/3/issue/{key}/transitions with
             ``{"transition": {"id": "<id>"}}``.

        Raises a ``RuntimeError`` (with available transition names listed)
        when no transition reaches *target_status* — the workflow does not
        allow it from the current state. Raises ``urllib.error.HTTPError``
        on non-2xx response from the POST.

        Per-issue lookup, not cached: transitions are issue-state-specific
        (depend on current status + workflow + caller permissions). Caching
        by project+issuetype produces incorrect hits for an issue mid-
        workflow.
        """
        transitions_resp = self._direct_rest_get(
            f"/rest/api/3/issue/{jira_key}/transitions"
        )
        transitions = (
            transitions_resp.get("transitions", [])
            if isinstance(transitions_resp, dict)
            else []
        )
        target_lower = target_status.strip().lower()
        match_id = None
        for t in transitions:
            if not isinstance(t, dict):
                continue
            name = (t.get("name") or "").strip().lower()
            to_name = ((t.get("to") or {}).get("name") or "").strip().lower()
            if target_lower in (name, to_name):
                match_id = t.get("id")
                if match_id:
                    break
        if not match_id:
            available = [
                f"{t.get('name')!r}->{(t.get('to') or {}).get('name')!r}"
                for t in transitions
                if isinstance(t, dict)
            ]
            raise RuntimeError(
                f"transition_issue_by_name: no transition reaches "
                f"{target_status!r} on {jira_key}. Available: "
                f"{available if available else '[none]'}"
            )
        self._direct_rest_post_raw(
            f"/rest/api/3/issue/{jira_key}/transitions",
            {"transition": {"id": str(match_id)}},
        )

    def validate_assignee_exists(
        self,
        assignee: str,
        *,
        issue_key: str | None = None,
        project_key: str | None = None,
    ) -> str:
        """Validate *assignee* resolves to an assignable user; return accountId.

        Mirrors the client-side pre-validation pattern from
        ``transition_issue_by_name`` (Gap 8). GETs
        ``/rest/api/3/user/assignable/search?query=<assignee>&issueKey=<key>``
        (or ``&project=<project>`` when called from a CREATE path with no
        issue key yet), then returns the matched ``accountId``. Callers should
        forward this resolved accountId to ACLI rather than the raw input to
        eliminate display-name/email ambiguity at the API boundary.

        Raises ``AssigneeNotFoundError`` when no user matches. Raises
        ``ValueError`` when neither scope arg is supplied.
        """
        if not (issue_key or project_key):
            raise ValueError(
                "validate_assignee_exists: issue_key or project_key required"
            )
        query_part = f"query={urllib.parse.quote(assignee)}"
        scope_part = (
            f"issueKey={urllib.parse.quote(issue_key)}"
            if issue_key
            else f"project={urllib.parse.quote(project_key or '')}"
        )
        path = f"/rest/api/3/user/assignable/search?{query_part}&{scope_part}"
        users = self._direct_rest_get(path)
        if not isinstance(users, list) or not users:
            scope_label = (
                f"issue={issue_key!r}" if issue_key else f"project={project_key!r}"
            )
            raise AssigneeNotFoundError(
                f"validate_assignee_exists: no assignable user matches "
                f"{assignee!r} for {scope_label}"
            )
        # Prefer exact match on emailAddress / accountId / displayName;
        # fall back to the first result (Jira's relevance ordering).
        for u in users:
            if not isinstance(u, dict):
                continue
            if assignee in (
                u.get("emailAddress"),
                u.get("accountId"),
                u.get("displayName"),
            ):
                acct = u.get("accountId")
                if acct:
                    return acct
        first = users[0]
        if isinstance(first, dict) and first.get("accountId"):
            return first["accountId"]
        raise AssigneeNotFoundError(
            f"validate_assignee_exists: assignable search returned results "
            f"with no accountId for {assignee!r}"
        )

    def unassign_issue(self, jira_key: str) -> None:
        """Explicitly unassign a Jira issue via REST v3 PUT.

        Uses direct REST v3 (not ACLI binary) because the /assignee endpoint
        requires body {"accountId": null} at root level — ACLI's _direct_rest_put
        wraps body as {"value": data} which is rejected by the assignee endpoint.
        Empirically verified: direct REST PUT is the de-facto pattern used by
        pycontribs/jira and atlassian-python-api for null-accountId unassign.
        """
        path = f"/rest/api/3/issue/{jira_key}/assignee"
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        body = json.dumps({"accountId": None}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="PUT",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            resp.read()

    def get_comments(self, jira_key: str) -> list[dict[str, Any]]:
        """Get all comments on a Jira issue."""
        cmd = [
            "jira",
            "workitem",
            "comment",
            "list",
            "--key",
            jira_key,
            "--json",
        ]
        result = self._run(cmd)
        return _parse_acli_comments(json.loads(result.stdout))

    def set_parent(self, jira_key: str, parent_key: str | None) -> None:
        """Set or clear the parent of a Jira issue via REST PUT.

        ACLI edit does NOT support --parent reparenting (verified live — ticket
        8b25-ae7a-efc3-47f6).  Uses direct REST:
        PUT /rest/api/3/issue/{key} {"fields":{"parent":{"key":"..."}}}

        When ``parent_key`` is None or empty, clears the parent by passing
        ``{"fields": {"parent": None}}``.

        Probe-validated: returns 204 on success.
        """
        if parent_key:
            body: Any = {"fields": {"parent": {"key": parent_key}}}
        else:
            body = {"fields": {"parent": None}}
        self._direct_rest_put_raw(f"/rest/api/3/issue/{jira_key}", body)

    def get_parent_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, str | None]:
        """Return a {jira_key → parent_key | None} map via REST search.

        Issues one paged REST search (POST ``/rest/api/3/search/jql``) with
        ``fields=["parent"]`` so we get parent data without hitting ACLI's
        field-selector restriction (ACLI rejects ``-f parent``).

        Endpoint contract (ticket 8b25, live-proven): the legacy
        ``POST /rest/api/3/search`` endpoint is RETIRED (HTTP 410). The
        replacement ``/rest/api/3/search/jql`` paginates via an opaque
        ``nextPageToken`` cursor — there is NO ``total`` field and sending
        ``startAt`` is rejected with HTTP 400. The first request body carries
        ``{jql, fields, maxResults}``; each subsequent request adds
        ``{nextPageToken: <token>}``. The loop terminates when the response
        reports ``isLast: true`` or yields a null/absent ``nextPageToken``.

        Paginates until the cursor is exhausted.  Returns an empty dict and
        logs on any REST failure (fetcher degrades gracefully — ticket 8b25).
        An HTTP 410 (endpoint retirement) is logged at ERROR (loud — API
        retirements must be noticed); transient faults stay at WARNING.

        Args:
            project: Jira project key (e.g. "DIG").
            jql: Optional JQL override.  Defaults to ``project = <project>``.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        effective_jql = jql or f"project = {project}"
        result: dict[str, str | None] = {}
        page_size = 100
        next_page_token: str | None = None

        while True:
            body: dict[str, Any] = {
                "jql": effective_jql,
                "maxResults": page_size,
                "fields": ["parent"],
            }
            if next_page_token is not None:
                body["nextPageToken"] = next_page_token
            try:
                resp = self._direct_rest_post_json("/rest/api/3/search/jql", body)
            except urllib.error.HTTPError as exc:
                if exc.code == 410:
                    _log.error(
                        "get_parent_map: endpoint POST /rest/api/3/search/jql "
                        "returned HTTP 410 GONE — the Jira search endpoint has "
                        "been RETIRED; parent enrichment is unavailable this pass. "
                        "This is an API retirement, not a transient fault: %r",
                        exc,
                    )
                else:
                    _log.warning(
                        "get_parent_map: REST search failed (HTTP %s): %r; "
                        "degrading gracefully — parent data absent this pass",
                        exc.code,
                        exc,
                    )
                break
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "get_parent_map: REST search failed: %r; "
                    "degrading gracefully — parent data will be absent this pass",
                    exc,
                )
                break

            if not isinstance(resp, dict):
                break
            issues = resp.get("issues") or []
            if not isinstance(issues, list):
                break
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                if not key:
                    continue
                fields = issue.get("fields") or {}
                parent_raw = fields.get("parent")
                parent_key_val: str | None = None
                if isinstance(parent_raw, dict):
                    parent_key_val = parent_raw.get("key") or None
                result[key] = parent_key_val

            # nextPageToken cursor contract: stop when isLast or token absent.
            if resp.get("isLast"):
                break
            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

        return result

    def get_comment_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, Any]:
        """Return a {jira_key → comment-field dict} map via ONE paged REST search.

        Comment-state enrichment (Action viability): the live comment fetch
        previously issued one ``acli comment list`` call per commented ticket
        every pass (~1-2s each, fleet-wide — measured multi-hour passes). This
        method amortises that into a SINGLE paged ``POST /rest/api/3/search/jql``
        with ``fields=["comment"]`` so the differ can dedup comments without a
        per-ticket round-trip.

        Returns ``{jira_key: <comment field dict>}`` where the value is the raw
        Jira ``comment`` field (``{"comments": [...], "total": N, ...}``) — the
        exact shape ``outbound_differ._diff_comments`` reads from a snapshot
        entry's ``comment`` key. Keys whose ``comment`` field is absent are
        omitted so the caller can fall back to the per-ticket ``get_comments``
        path for them (the never-emit-blind invariant stays intact).

        Pagination + degradation contract mirror ``get_parent_map``: opaque
        ``nextPageToken`` cursor (no ``startAt`` / ``total``); HTTP 410 →
        ERROR (endpoint retirement is loud); other faults → WARNING; an empty
        dict is returned on failure so the fetcher degrades gracefully.

        Args:
            project: Jira project key (e.g. "DIG").
            jql: Optional JQL override.  Defaults to ``project = <project>``.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        effective_jql = jql or f"project = {project}"
        result: dict[str, Any] = {}
        page_size = 100
        next_page_token: str | None = None

        while True:
            body: dict[str, Any] = {
                "jql": effective_jql,
                "maxResults": page_size,
                "fields": ["comment"],
            }
            if next_page_token is not None:
                body["nextPageToken"] = next_page_token
            try:
                resp = self._direct_rest_post_json("/rest/api/3/search/jql", body)
            except urllib.error.HTTPError as exc:
                if exc.code == 410:
                    _log.error(
                        "get_comment_map: endpoint POST /rest/api/3/search/jql "
                        "returned HTTP 410 GONE — the Jira search endpoint has "
                        "been RETIRED; comment enrichment is unavailable this pass. "
                        "Per-ticket get_comments fallback applies. API retirement, "
                        "not a transient fault: %r",
                        exc,
                    )
                else:
                    _log.warning(
                        "get_comment_map: REST search failed (HTTP %s): %r; "
                        "degrading gracefully — per-ticket fallback applies",
                        exc.code,
                        exc,
                    )
                break
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "get_comment_map: REST search failed: %r; "
                    "degrading gracefully — per-ticket fallback applies",
                    exc,
                )
                break

            if not isinstance(resp, dict):
                break
            issues = resp.get("issues") or []
            if not isinstance(issues, list):
                break
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                if not key:
                    continue
                fields = issue.get("fields") or {}
                comment_field = fields.get("comment")
                # Only record keys the search actually returned a comment field
                # for; omit the rest so the caller falls back to get_comments
                # (preserves the never-emit-blind invariant).
                if isinstance(comment_field, dict):
                    result[key] = comment_field

            if resp.get("isLast"):
                break
            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

        return result


    def update_priority(self, jira_key: str, priority_name: str) -> None:
        """Update priority on a Jira issue via REST PUT.

        ACLI does not support priority edit. Uses direct REST API:
        PUT /rest/api/3/issue/{key} with {"fields":{"priority":{"name":"..."}}}
        Probe-validated: returns 204 on success.
        """
        self._direct_rest_put_raw(
            f"/rest/api/3/issue/{jira_key}",
            {"fields": {"priority": {"name": priority_name}}},
        )

    def update_issuetype(self, jira_key: str, type_name: str) -> None:
        """Update issue type on a Jira issue via REST PUT.

        ACLI does not support issuetype edit. Uses direct REST API:
        PUT /rest/api/3/issue/{key} with {"fields":{"issuetype":{"name":"..."}}}
        Probe-validated: returns 204 on success.
        """
        self._direct_rest_put_raw(
            f"/rest/api/3/issue/{jira_key}",
            {"fields": {"issuetype": {"name": type_name}}},
        )

    def update_comment(
        self, jira_key: str, comment_id: str, body: str
    ) -> dict[str, Any]:
        """Update an existing comment on a Jira issue via ACLI.

        Probe-validated: ``acli jira workitem comment update`` works correctly.
        """
        cmd = [
            "jira",
            "workitem",
            "comment",
            "update",
            "--key",
            jira_key,
            "--id",
            str(comment_id),
            "--body",
            body,
            "--json",
        ]
        result = self._run(cmd)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def delete_comment(self, jira_key: str, comment_id: str) -> None:
        """Delete a comment from a Jira issue via REST DELETE.

        ACLI has no comment delete subcommand. Uses direct REST API:
        DELETE /rest/api/3/issue/{key}/comment/{id}
        Probe-validated: returns 204 on success.
        """
        path = f"/rest/api/3/issue/{jira_key}/comment/{comment_id}"
        self._direct_rest_delete(path)


    def set_relationship(
        self,
        from_key: str,
        to_key: str,
        link_type: str = "Blocks",
    ) -> dict[str, Any]:
        """Create a link between two Jira issues.

        Raises subprocess.CalledProcessError on ACLI failure.
        """
        cmd = [
            "jira",
            "workitem",
            "link",
            "create",
            "--out",
            from_key,
            "--in",
            to_key,
            "--type",
            link_type,
            # Bug 44de: --json enables structured-failure detection.
            "--json",
        ]
        self._run(cmd)  # raises on failure — no silent swallowing
        return {"status": "created", "from": from_key, "to": to_key}

    def get_issue_links(self, jira_key: str) -> list[dict[str, Any]]:
        """Get existing issue links for a Jira issue.

        Returns a list of link dicts matching the Jira REST API format:
        ``[{"type": {"name": ...}, "inwardIssue": {...}|None, "outwardIssue": {...}|None}]``

        Used by the LINK handler for pre-create deduplication.
        Raises subprocess.CalledProcessError on ACLI failure.
        """
        cmd = [
            "jira",
            "workitem",
            "link",
            "list",
            "--key",
            jira_key,
            "--json",
        ]
        result = self._run(cmd)
        parsed = json.loads(result.stdout or "[]")
        if isinstance(parsed, list):
            return parsed
        # Some ACLI versions wrap results in a dict with an "issuelinks" key
        if isinstance(parsed, dict):
            return parsed.get("issuelinks", [])
        return []

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
        base = self._acli_cmd if self._acli_cmd is not None else _DEFAULT_ACLI_CMD
        # `--yes` skips ACLI's interactive confirmation prompt. Without it,
        # `acli jira workitem delete` waits on stdin for confirmation and
        # exits non-zero in non-TTY contexts (bug 3256-f960-4ae6-4943
        # surfaced by the live cfd6 capability probe run).
        full_cmd = base + [
            "jira",
            "workitem",
            "delete",
            "--key",
            jira_key,
            "--yes",
            # Bug 44de: --json so structured-failure detection runs on the
            # exit=0-on-failure path that ACLI exposes for delete too.
            "--json",
        ]
        try:
            completed = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                check=True,
                env=_build_env(),
            )
            # Bug 44de: delete bypasses _run_acli, so call the check here too.
            _check_mutation_failure(completed.stdout, full_cmd)
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

    def delete_issue_link(self, link_id: str) -> dict[str, Any]:
        """Delete a Jira issue link by its ID via ACLI.

        Uses ``jira workitem link delete --id LINK_ID`` to remove the link.
        Raises subprocess.CalledProcessError on ACLI failure (e.g. 404 if
        the link was already deleted, or 409 on concurrent modification).
        Callers should treat 404/409 as idempotent success.
        """
        cmd = [
            "jira",
            "workitem",
            "link",
            "delete",
            "--id",
            link_id,
            # Bug 44de: --json enables structured-failure detection.
            "--json",
        ]
        self._run(cmd)
        return {"status": "deleted", "link_id": link_id}
