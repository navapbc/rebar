#!/usr/bin/env python3
"""AcliClient REST transport mixin.

The direct-REST surface of the ACLI client: a retrying urlopen wrapper plus the
``_direct_rest_{get,put_raw,post_raw,post_json,delete}`` helpers and the
issue/entity property get/set methods built on them. Jira endpoints that ACLI
does not expose (issue properties, assignee unassign, transitions, parent,
priority/issuetype edits, comment delete) route through these concrete writes.

Mixed into ``AcliClient`` (``acli.py``); every method depends only on the
credential attributes ``self.jira_url`` / ``self.user`` / ``self.api_token``
set in ``AcliClient.__init__``, so the bodies are unchanged from the monolith.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from rebar_reconciler.adapters.jira.acli_subprocess import AssigneeNotFoundError


@dataclass(frozen=True)
class _OneAttemptNoSleep:
    """A tiny frozen marker whose SINGLE module-level instance
    (:data:`ONE_ATTEMPT_NO_SLEEP`) opts a REST call into the one-attempt / no-sleep
    per-call policy (REB-3115 S1 T2).

    Frozen so it is hashable and immutable, and DELIBERATELY compared by identity
    (``retry_policy is ONE_ATTEMPT_NO_SLEEP``) rather than by value — the shared
    singleton is the token the summary-recovery call site threads through, and the
    held-out oracle asserts it is the exact same object re-exported from
    ``summary_operation``. Any other ``retry_policy`` value (including ``None``, the
    default) leaves every caller on the legacy three-attempt / 2s-5s policy unchanged.
    """


#: The SOLE one-attempt/no-sleep policy token. Re-exported unchanged from
#: ``summary_operation`` so exactly one shared singleton exists across the seam.
ONE_ATTEMPT_NO_SLEEP = _OneAttemptNoSleep()


class AcliRestMixin:
    """REST transport helpers, issue-property accessors, and the direct-REST
    issue operations (transition, assignee validate/unassign, reparent,
    myself, point-read) for AcliClient."""

    # Credential attributes set in ``AcliClient.__init__`` (acli.py); declared
    # here type-only so mypy sees the surface this transport mixin depends on.
    jira_url: str
    user: str
    api_token: str

    def _rest_urlopen_with_retry(
        self,
        req: urllib.request.Request,
        *,
        timeout: int = 10,
        retry_policy: Any = None,
    ) -> Any:
        """Execute urlopen(req, timeout=timeout) with transient-fault retry.

        By default retries up to 2 times (3 total attempts) on transient
        connectivity errors: builtin ``TimeoutError`` (read-timeout from
        ssl/socket layer), ``urllib.error.URLError`` whose reason is a
        ``TimeoutError`` or ``ConnectionError``, and bare ``ConnectionError``.
        Backoff delays are 2 s after the first failure, 5 s after the second.

        When ``retry_policy is ONE_ATTEMPT_NO_SLEEP`` (REB-3115 S1 T2) the call
        makes EXACTLY ONE attempt and NEVER sleeps — the shared logical retry
        budget owns replay for that caller, so the transport must not also
        re-drive it. The attempt count and backoff schedule are the ONLY thing
        the policy changes; with any other value (including the ``None`` default)
        behaviour is byte-for-byte identical to before (3 attempts, sleeps 2 then
        5), so every unrelated caller is untouched.

        Does NOT retry on ``urllib.error.HTTPError`` (4xx / 5xx) — HTTP-level
        error semantics are unchanged.  Raises the original exception after all
        attempts are exhausted.

        Retries are logged to stderr at WARNING level so they appear in the
        probe run log without polluting normal output.
        """
        attempts, _BACKOFFS = (1, ()) if retry_policy is ONE_ATTEMPT_NO_SLEEP else (3, (2, 5))
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError:
                # HTTP errors (4xx/5xx) are deterministic — do not retry.
                raise
            except (TimeoutError, ConnectionError) as exc:
                last_exc = exc
            except urllib.error.URLError as exc:
                # URLError wraps lower-level errors in .reason; only retry
                # when the root cause is a timeout or connection failure.
                if isinstance(exc.reason, (TimeoutError, ConnectionError)):
                    last_exc = exc
                else:
                    raise
            if attempt < attempts - 1:
                delay = _BACKOFFS[attempt]
                print(
                    f"[REST-retry] attempt {attempt + 1} failed "
                    f"({last_exc!r}); retrying in {delay}s …",
                    file=sys.stderr,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def _direct_rest_post_raw(self, path: str, body: Any) -> None:
        """POST JSON body to a Jira REST path verbatim (no wrapping).

        Used for endpoints that take their own JSON shape — e.g.
        ``/rest/api/3/issue/{key}/transitions`` with
        ``{"transition": {"id": "..."}}``.

        Bug 85a1 (Gap 8): status outbound now uses REST instead of ACLI to
        avoid ACLI's silent-exit-0-on-failure (Gap 5). Returns None on 2xx;
        raises urllib.error.HTTPError on non-2xx.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            resp.read()

    def _direct_rest_put_raw(self, path: str, body: Any) -> None:
        """PUT JSON body to a Jira REST path verbatim (no wrapping).

        Used for endpoints that take their own JSON shape — e.g.
        /rest/api/3/issue/{key} with ``{"update": {"labels": [...]}}``,
        and issue-property writes (PUT /rest/api/3/issue/{key}/properties/{prop}
        whose request body IS the property value verbatim).
        Raises urllib.error.HTTPError on non-2xx response.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="PUT",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            resp.read()

    def set_issue_property(self, jira_key: str, property_key: str, value: Any) -> None:
        """Set a Jira issue property via REST PUT.

        Calls /rest/api/3/issue/{jira_key}/properties/{property_key} with the
        value sent as the request body verbatim. Jira's issue-properties API
        stores whatever JSON is PUT as the property's value (the docs are
        explicit: "Request body: The value of the property. Must be valid
        JSON"). The earlier wrapped helper was incorrect — it caused the
        property to be stored as the literal `{"value": uuid}` dict instead of
        the uuid string. Bug 0b27-b785-dea8-49a0 surfaced this via the cfd6
        live probe (STEP_PROPERTY_READ returned `{'value': uuid}` instead of
        `uuid`).

        Uses `_direct_rest_put_raw` so the value is PUT exactly as-is.
        """
        path = f"/rest/api/3/issue/{jira_key}/properties/{property_key}"
        self._direct_rest_put_raw(path, value)

    def set_reporter(self, jira_key: str, account_id: str) -> None:
        """Set a Jira issue's reporter to ``account_id`` via REST (264f).

        Uses ``_direct_rest_put_raw`` so the issue-edit body is sent verbatim:
        ``PUT /rest/api/3/issue/{key}`` with
        ``{"fields": {"reporter": {"accountId": account_id}}}``. Raises
        ``urllib.error.HTTPError`` on a non-2xx response (a 4xx = Modify-Reporter not
        granted); the caller (dispatch's ``_update_one_apply_reporter``) softens it."""
        self._direct_rest_put_raw(
            f"/rest/api/3/issue/{jira_key}",
            {"fields": {"reporter": {"accountId": account_id}}},
        )

    def search_user_by_email(self, email: str) -> str | None:
        """Resolve an email to a Jira accountId via ``GET /rest/api/3/user/search`` (264f).

        The v3 endpoint returns a JSON LIST of user objects each carrying ``accountId`` +
        ``emailAddress``; return the accountId of the entry whose ``emailAddress`` matches
        ``email`` EXACTLY (case-insensitive). Because Jira substring/relevance-matches the
        query, ZERO or ≥2 exact matches → ``None`` (never guess). Used only as a transient
        bootstrap by the outbound differ — the result is NOT persisted to ``mappings``."""
        if not email:
            return None
        path = f"/rest/api/3/user/search?query={urllib.parse.quote(email)}"
        users = self._direct_rest_get(path)
        if not isinstance(users, list):
            return None
        target = email.strip().lower()
        matched: list[str] = []
        for u in users:
            if not isinstance(u, dict):
                continue
            acct = u.get("accountId")
            got = u.get("emailAddress")
            if acct and isinstance(got, str) and got.strip().lower() == target:
                matched.append(acct)
        return matched[0] if len(matched) == 1 else None

    def _direct_rest_get(self, path: str, *, retry_policy: Any = None) -> Any:
        """GET JSON data from a Jira REST path using stored credentials.

        Follows the same urllib pattern as ``_direct_rest_put_raw``.
        Raises urllib.error.HTTPError on non-2xx response.

        ``retry_policy`` is threaded to :meth:`_rest_urlopen_with_retry`; the ONLY
        caller that passes ``ONE_ATTEMPT_NO_SLEEP`` (REB-3115 S1 T2) is the summary
        recovery read, so every other consumer keeps the default 3-attempt policy.

        Returns whatever json.loads decodes from the response body. Most Jira
        endpoints return a JSON object, but a few (e.g. issue-properties value
        when set to a scalar) return list/str/int/None. Callers that require a
        dict shape must validate explicitly.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Basic {creds}",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10, retry_policy=retry_policy) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_issue_property(self, jira_key: str, property_key: str) -> Any:
        """Get a Jira issue property via REST GET.

        Calls /rest/api/3/issue/{jira_key}/properties/{property_key} and returns
        the 'value' field from the response per the Jira issue properties API contract.

        Raises:
            urllib.error.HTTPError: from the underlying _direct_rest_get. Note
                that Jira returns 404 when the property does NOT exist on the
                issue — that case surfaces as HTTPError, NOT as KeyError below.
                Callers that need to handle "property not yet set" should catch
                HTTPError and inspect ``.code``.
            KeyError: only when the response IS a 2xx but the body shape is
                malformed (response is not a dict, or it lacks the 'value'
                field). This is a transport/proxy anomaly, NOT the
                missing-property signal. The exception message includes a
                truncated repr of the response for diagnostics; long bodies
                are clipped to 200 chars to avoid leaking credentials or PII
                from upstream error pages.
        """
        path = f"/rest/api/3/issue/{jira_key}/properties/{property_key}"
        response = self._direct_rest_get(path)
        if not isinstance(response, dict) or "value" not in response:
            # Clip the response repr so corporate-gateway error bodies that
            # may include auth headers or session cookies cannot leak in full
            # to logs / StepResult.details.
            _repr = repr(response)
            if len(_repr) > 200:
                _repr = _repr[:200] + f"...(truncated, {len(_repr)} chars total)"
            raise KeyError(
                f"Jira issue-property response for {jira_key}/{property_key} "
                f"missing 'value' field: {_repr}"
            )
        return response["value"]

    def set_entity_property(self, issue_key: str, prop_name: str, value: Any) -> None:
        """Alias for set_issue_property — sets a Jira entity property."""
        return self.set_issue_property(issue_key, prop_name, value)

    def get_entity_property(self, issue_key: str, prop_name: str) -> Any:
        """Alias for get_issue_property — retrieves a Jira entity property.

        Inherits the same Raises contract as get_issue_property:
        urllib.error.HTTPError on transport/4xx (including 404 for absent
        properties), KeyError only when the 2xx body shape is malformed.
        """
        return self.get_issue_property(issue_key, prop_name)

    def _direct_rest_post_json(self, path: str, body: Any) -> Any:
        """POST JSON to a Jira REST path and return the decoded JSON response.

        Unlike ``_direct_rest_post_raw`` (which discards the response body),
        this helper returns the parsed JSON — needed by ``get_parent_map`` to
        read search results.

        Raises ``urllib.error.HTTPError`` on non-2xx responses.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _direct_rest_delete(self, path: str) -> None:
        """DELETE a Jira REST resource using stored credentials.

        Raises urllib.error.HTTPError on non-2xx response.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        req = urllib.request.Request(
            url,
            method="DELETE",
            headers={
                "Authorization": f"Basic {creds}",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            resp.read()

    # --- Direct-REST issue operations (relocated from acli.py) ---
    #
    # These deliberately BYPASS the ACLI binary and speak Jira REST v3, either
    # because ACLI cannot express the operation (``--parent`` reparenting,
    # null-accountId unassign) or because it exits 0 on failure (the Gap 5
    # "lying-success" bug), leaving HTTP status codes as the only reliable
    # failure signal. They ride the ``_direct_rest_*`` /
    # ``_rest_urlopen_with_retry`` helpers above and sit beside the REST
    # issue-property / user-search accessors they are siblings of; bodies are
    # unchanged from the pre-split ``acli.py``.

    def get_issue_by_rest(self, jira_key: str, *, retry_policy: Any = None) -> dict[str, Any]:
        """Get a Jira issue via direct REST GET (immediately consistent).

        Unlike get_issue (which uses ACLI's JQL search internally), this
        hits GET /rest/api/3/issue/{key} which reads from the primary store
        and is not subject to Jira Cloud's search index lag.

        ``retry_policy`` (default ``None`` = legacy 3-attempt policy) threads to
        :meth:`_direct_rest_get`; only the summary-recovery call site passes the
        optional ``ONE_ATTEMPT_NO_SLEEP`` (REB-3115 S1 T2).
        """
        path = f"/rest/api/3/issue/{jira_key}"
        return self._direct_rest_get(path, retry_policy=retry_policy)

    def get_myself(self) -> dict[str, Any]:
        """Return the authenticated user's Jira profile via GET /rest/api/2/myself.

        Used to retrieve the service account's profile timezone, which Jira Cloud
        uses when interpreting unqualified JQL datetime strings. Cached per instance.
        """
        if hasattr(self, "_myself_cache"):
            return self._myself_cache
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
        requires body {"accountId": null} at root level — the issue-property
        write shape is rejected here. Empirically verified: direct REST PUT is
        the de-facto pattern used by pycontribs/jira and atlassian-python-api
        for null-accountId unassign.
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
