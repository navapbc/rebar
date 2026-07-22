#!/usr/bin/env python3
"""AcliClient REST transport mixin.

The direct-REST surface of the ACLI client: a retrying urlopen wrapper plus the
``_direct_rest_{get,put,put_raw,post_raw,post_json,delete}`` helpers and the
issue/entity property get/set methods built on them. Jira endpoints that ACLI
does not expose (issue properties, assignee unassign, transitions, parent,
priority/issuetype edits, comment delete) route through these.

Mixed into ``AcliClient`` (``acli.py``); every method depends only on the
credential attributes ``self.jira_url`` / ``self.user`` / ``self.api_token``
set in ``AcliClient.__init__``, so the bodies are unchanged from the monolith.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class AcliRestMixin:
    """REST transport helpers + issue-property accessors for AcliClient."""

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
    ) -> Any:
        """Execute urlopen(req, timeout=timeout) with transient-fault retry.

        Retries up to 2 times (3 total attempts) on transient connectivity
        errors: builtin ``TimeoutError`` (read-timeout from ssl/socket layer),
        ``urllib.error.URLError`` whose reason is a ``TimeoutError`` or
        ``ConnectionError``, and bare ``ConnectionError``.  Backoff delays are
        2 s after the first failure, 5 s after the second.

        Does NOT retry on ``urllib.error.HTTPError`` (4xx / 5xx) — HTTP-level
        error semantics are unchanged.  Raises the original exception after all
        attempts are exhausted.

        Retries are logged to stderr at WARNING level so they appear in the
        probe run log without polluting normal output.
        """
        _BACKOFFS = (2, 5)  # seconds between attempt 1→2 and 2→3
        last_exc: BaseException | None = None
        for attempt in range(3):
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
            if attempt < 2:
                delay = _BACKOFFS[attempt]
                print(
                    f"[REST-retry] attempt {attempt + 1} failed "
                    f"({last_exc!r}); retrying in {delay}s …",
                    file=sys.stderr,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def _direct_rest_put(self, path: str, data: Any) -> None:
        """PUT JSON data to a Jira issue-properties REST path using stored credentials.

        Wraps the body as ``{"value": data}`` per the Jira issue-properties
        API contract (used by set_issue_property). Do NOT use this for any
        other PUT endpoint (e.g. /rest/api/3/issue/{key} updates) — use
        _direct_rest_put_raw() instead so the body is sent unwrapped.

        Spike confirmed ACLI has no issue properties subcommand.
        Raises urllib.error.HTTPError on non-2xx response.
        """
        self._direct_rest_put_raw(path, {"value": data})

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
        JSON"). The earlier wrapping path (`_direct_rest_put` adding a
        `{"value": ...}` envelope) was incorrect — it caused the property to
        be stored as the literal `{"value": uuid}` dict instead of the uuid
        string. Bug 0b27-b785-dea8-49a0 surfaced this via the cfd6 live probe
        (STEP_PROPERTY_READ returned `{'value': uuid}` instead of `uuid`).

        Now uses `_direct_rest_put_raw` so the value is PUT exactly as-is.
        """
        path = f"/rest/api/3/issue/{jira_key}/properties/{property_key}"
        self._direct_rest_put_raw(path, value)

    def set_reporter(self, jira_key: str, account_id: str) -> None:
        """Set a Jira issue's reporter to ``account_id`` via REST (264f).

        Uses ``_direct_rest_put_raw`` (NOT ``_direct_rest_put``, which wraps the body as
        ``{"value": ...}`` for the issue-properties API) so the issue-edit body is sent
        verbatim: ``PUT /rest/api/3/issue/{key}`` with
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

    def _direct_rest_get(self, path: str) -> Any:
        """GET JSON data from a Jira REST path using stored credentials.

        Follows the same urllib pattern as _direct_rest_put().
        Raises urllib.error.HTTPError on non-2xx response.

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
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
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
