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
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from rebar_reconciler import acli_cli_ops, acli_subprocess

# Module-level ACLI issue ops live in acli_cli_ops; the AcliClient methods
# delegate to them via ``acli_cli_ops.<name>``. Only the private helpers below
# are pulled in by name for internal use.
from rebar_reconciler.acli_cli_ops import (
    _attach_parent_guarded,
    _create_from_json_payload,
    _create_issue_from_json,
    _create_issue_no_json,
    _extract_parent_key,
    _parse_paginated_comments,
    _verify_created_issue,
)
from rebar_reconciler.acli_graph import AcliGraphMixin
from rebar_reconciler.acli_rest import AcliRestMixin

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
    AcliMutationError,
    AcliTimeoutError,
    AssigneeNotFoundError,
    RetryExhaustedError,
    _build_env,
    _check_mutation_failure,
    _run_acli,
    resolve_jira_settings,
)

# Field sanitization + local↔Jira value maps live in the Jira vendor adapter
# (``adapters/jira/jira_fields.py``, relocated by ticket 44be); re-exported here
# so ``acli.<name>`` keeps resolving for callers and the characterization suites
# (point-of-use read access).
from rebar_reconciler.adapters.jira.jira_fields import (
    _JIRA_LABEL_MAX_CHARS,
    _JIRA_SUMMARY_MAX_CHARS,
    _LOCAL_PRIORITY_TO_JIRA,
    _LOCAL_STATUS_TO_JIRA,
    InvalidLabelError,
    _sanitize_comment,
    _sanitize_description,
    _sanitize_label,
    _sanitize_summary,
)
from rebar_reconciler.adf import text_to_adf as _text_to_adf  # canonical location

# Re-export facade. These names are imported from the sibling acli_* modules and
# the Jira vendor adapter (adapters/jira/jira_fields) solely so ``acli.<name>``
# keeps resolving for callers and the characterization suites; ``__all__`` records
# them as intentional re-exports.
__all__ = [
    "AcliMutationError",
    "AcliTimeoutError",
    "AssigneeNotFoundError",
    "InvalidLabelError",
    "RetryExhaustedError",
    "_ASSIGNEE_NOT_FOUND_ERROR",
    "_ASSIGNEE_PERMISSION_ERROR",
    "_AUTH_FAILURE_CODE",
    "_DEFAULT_ACLI_CMD",
    "_JIRA_LABEL_MAX_CHARS",
    "_JIRA_SUMMARY_MAX_CHARS",
    "_MAX_ATTEMPTS",
    "_attach_parent_guarded",
    "_build_env",
    "_check_mutation_failure",
    "_create_from_json_payload",
    "_create_issue_from_json",
    "_create_issue_no_json",
    "_run_acli",
    "_sanitize_comment",
    "_sanitize_label",
    "_verify_created_issue",
]

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

    result = acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd, retry_on_timeout=False)  # WRITE
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# AcliClient class — used by the rebar_reconciler bands (fetcher, applier,
# stale_band, open_count_skew_band) and the capability / forward-compat probes.
# ---------------------------------------------------------------------------


class AcliClient(AcliRestMixin, AcliGraphMixin):
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
            cmd, acli_cmd=self._acli_cmd, retry_on_timeout=retry_on_timeout
        )

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
            # Truncate to Jira's 32,767-char limit so an over-length description
            # does not abort the create (bug 626d follow-up).
            optional_fields["description"] = _sanitize_description(str(ticket_data["description"]))
        if ticket_data.get("priority") is not None:
            optional_fields["priority"] = ticket_data["priority"]
        if ticket_data.get("assignee"):
            # Bug 544e: resolve the assignee through the SAME validator the UPDATE
            # path uses (validate_assignee_exists), so an ambiguous/unmappable handle
            # is left UNASSIGNED — matching the update outcome — instead of being
            # passed raw to ACLI/Jira, which fuzzy-matches or applies a project
            # default and silently MIS-assigns. CREATE has no issue key yet, so
            # resolution uses PROJECT scope. A definitively-unmappable assignee
            # (AssigneeNotFoundError) is omitted (unassigned); transient resolution
            # errors propagate so the create is retried rather than mis-assigned.
            try:
                optional_fields["assignee"] = self.validate_assignee_exists(
                    str(ticket_data["assignee"]), project_key=project
                )
            except AssigneeNotFoundError:
                pass  # unmappable → omit (leave unassigned), like the UPDATE path
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

    def update_issue(
        self, jira_key: str, *, assignee_is_account_id: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        """Update a Jira issue via ACLI.

        264f: ``assignee_is_account_id`` (a bool — the ONLY new coupling into acli;
        NO rebar-core import) marks that ``kwargs["assignee"]`` is an ALREADY-RESOLVED
        Jira accountId (from the flow layer's identity mapping). When True a non-empty
        assignee is submitted DIRECTLY, skipping ``validate_assignee_exists`` (no
        assignable search). It is captured as an explicit keyword (never forwarded to
        the module-level ``update_issue``), so it can't leak to the ACLI subprocess as a
        bogus ``--assignee_is_account_id`` flag.

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
                except Exception as exc:  # noqa: BLE001 — fail-open: unassign non-fatal, batch continues
                    print(  # noqa: T201
                        f"update_issue: unassign_issue({jira_key}) failed: {exc!r}",
                        file=sys.stderr,
                    )
            elif assignee_is_account_id:
                # 264f: already a resolved accountId — submit directly, no search.
                pass
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
            result = self._run(cmd, retry_on_timeout=True)  # READ — idempotent
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
                self._myself_cache: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logging.warning("get_myself: failed to fetch /rest/api/2/myself: %s", exc)
            # missing keys gracefully (defaulting to UTC), and caching prevents a
            # second network failure on the same run from the verify+fetch double-call.
            self._myself_cache = {}
        return self._myself_cache

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
        transitions_resp = self._direct_rest_get(f"/rest/api/3/issue/{jira_key}/transitions")
        transitions = (
            transitions_resp.get("transitions", []) if isinstance(transitions_resp, dict) else []
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

        Requires an EXACT identity match (emailAddress / accountId / displayName).
        Jira's assignable/search does substring/relevance matching, so a local
        assignee that is not a Jira user (e.g. an agent identity like
        ``"loop-agent"``) can fuzzily match an unrelated account (``"Jira Triage
        Agent"``). Returning that first result would MIS-ASSIGN the ticket, so a
        non-exact result is treated as no match (bug 9b94 follow-up) — the caller
        then leaves the issue unassigned rather than guessing.

        Raises ``AssigneeNotFoundError`` when no user EXACTLY matches. Raises
        ``ValueError`` when neither scope arg is supplied.
        """
        if not (issue_key or project_key):
            raise ValueError("validate_assignee_exists: issue_key or project_key required")
        query_part = f"query={urllib.parse.quote(assignee)}"
        scope_part = (
            f"issueKey={urllib.parse.quote(issue_key)}"
            if issue_key
            else f"project={urllib.parse.quote(project_key or '')}"
        )
        path = f"/rest/api/3/user/assignable/search?{query_part}&{scope_part}"
        users = self._direct_rest_get(path)
        if not isinstance(users, list) or not users:
            scope_label = f"issue={issue_key!r}" if issue_key else f"project={project_key!r}"
            raise AssigneeNotFoundError(
                f"validate_assignee_exists: no assignable user matches "
                f"{assignee!r} for {scope_label}"
            )
        # 1) EXACT match on emailAddress / accountId / displayName.
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

        # 2) NORMALIZED match (bug 9b94): a local assignee is often a case/separator
        # variant of a real identity — "joe-oakhart" for "Joe Oakhart". Compare the
        # normalized (lowercased, alphanumerics-only) assignee against each user's
        # normalized displayName / email local-part / accountId, and accept ONLY a
        # UNIQUE match. This resolves clear variants while still rejecting BOTH
        # coincidental substring matches ("loop-agent" !-> "jiratriageagent") and
        # ambiguous partials ("joe" -> 3 Joes, no unique full-identity match).
        def _norm(s: str | None) -> str:
            return re.sub(r"[^a-z0-9]", "", (s or "").lower())

        target = _norm(assignee)
        if target:
            matched: set[str] = set()
            for u in users:
                if not isinstance(u, dict):
                    continue
                acct = u.get("accountId")
                if not acct:
                    continue
                candidates = {
                    _norm(u.get("displayName")),
                    _norm((u.get("emailAddress") or "").split("@")[0]),
                    _norm(acct),
                }
                candidates.discard("")
                if target in candidates:
                    matched.add(acct)
            if len(matched) == 1:
                return next(iter(matched))
        raise AssigneeNotFoundError(
            f"validate_assignee_exists: no exact or unique-normalized match for {assignee!r} "
            f"({len(users)} non-exact assignable-search result(s) ignored)"
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
        """Get ALL comments on a Jira issue.

        ``--paginate`` is REQUIRED (bug 1f3d): without it ACLI returns only the first
        page (50, oldest), so the outbound dedup re-posts everything past page 1 and
        inflates high-traffic issues to Jira's 5000-comment cap. ``--paginate`` streams
        one JSON object per page — parse via ``_parse_paginated_comments``.
        """
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
        # `--yes` skips ACLI's interactive confirmation prompt. Without it,
        # `acli jira workitem delete` waits on stdin for confirmation and
        # exits non-zero in non-TTY contexts (bug 3256-f960-4ae6-4943
        # surfaced by the live cfd6 capability probe run).
        cmd = [
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
            # Bug d843: route the delete through the _run_acli chokepoint so it
            # inherits the timeout/process-group reaping (and errors='replace').
            # WRITE — retry_on_timeout=False (delete is non-idempotent on a
            # timeout; callers below treat 404 as idempotent success).
            # _run_acli already runs _check_mutation_failure on the completed run.
            acli_subprocess._run_acli(cmd, acli_cmd=self._acli_cmd, retry_on_timeout=False)
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
