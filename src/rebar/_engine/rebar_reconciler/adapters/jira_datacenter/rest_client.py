"""JiraDataCenterClient — REST v2 / bearer-auth transport for Jira Server / Data Center.

The Data Center analogue of the Cloud ``AcliClient``: a stdlib-``urllib`` transport
speaking the classic ``/rest/api/2`` API with ``Authorization: Bearer <PAT>`` and
plain-text (wiki-markup) bodies rather than Cloud's ACLI subprocess + v3 ADF. It
satisfies the reconciler ``TicketTransport`` role plus the ``SupportsLinks`` and
``SupportsComments`` capabilities, and exposes ``probe_issue`` for the backend's
absence probe.

The retry contract mirrors ``adapters/jira/acli_rest._rest_urlopen_with_retry``:
transient connectivity faults (``TimeoutError`` / ``ConnectionError``, and
``URLError`` wrapping either) are retried with 2s / 5s backoff; ``HTTPError`` (4xx /
5xx) is deterministic and never retried. The ``urlopen`` callable is injectable so
tests exercise the endpoint/verb/header contract without a live server.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

_RETRY_BACKOFFS = (2, 5)
_DEFAULT_TIMEOUT = 10
_SEARCH_PAGE_SIZE = 100
_UNSET = object()


class JiraDataCenterClient:
    """REST v2 transport bound to one Data Center site, project, and PAT."""

    def __init__(
        self,
        *,
        base_url: str,
        pat: str,
        project: str,
        timeout: int = _DEFAULT_TIMEOUT,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._pat = pat
        self._project = project
        self._timeout = timeout
        self._opener = opener or urllib.request.urlopen

    # --- low-level HTTP ------------------------------------------------------

    def _headers(self, *, with_body: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._pat}",
            "Accept": "application/json",
        }
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        url = f"{self._base}{path}"
        if params:
            flat = {
                k: (",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v))
                for k, v in params.items()
            }
            url = f"{url}?{urllib.parse.urlencode(flat)}"
        return url

    def _urlopen_with_retry(self, req: urllib.request.Request) -> Any:
        """urlopen with transient-fault retry; HTTPError is never retried."""
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                return self._opener(req, timeout=self._timeout)
            except urllib.error.HTTPError:
                raise
            except (TimeoutError, ConnectionError) as exc:
                last_exc = exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (TimeoutError, ConnectionError)):
                    last_exc = exc
                else:
                    raise
            if attempt < 2:
                delay = _RETRY_BACKOFFS[attempt]
                print(
                    f"[jira-dc-retry] attempt {attempt + 1} failed "
                    f"({last_exc!r}); retrying in {delay}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> Any:
        """Issue a request and return the decoded JSON (``{}`` for an empty body).

        Raises ``urllib.error.HTTPError`` on a non-2xx response.
        """
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._build_url(path, params),
            data=data,
            method=method,
            headers=self._headers(with_body=data is not None),
        )
        with self._urlopen_with_retry(req) as resp:
            raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _get_allow_status(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """GET that returns ``(status_code, payload)`` instead of raising on 4xx/5xx.

        Used by the absence probe, which classifies by HTTP status.
        """
        req = urllib.request.Request(
            self._build_url(path, params),
            method="GET",
            headers=self._headers(with_body=False),
        )
        try:
            with self._urlopen_with_retry(req) as resp:
                raw = resp.read()
                status = getattr(resp, "status", None) or resp.getcode()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return status, payload
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    def _search_all(self, jql: str, fields: list[str]) -> list[dict[str, Any]]:
        """Page through the classic v2 ``/search`` (startAt/total) collecting issues."""
        issues: list[dict[str, Any]] = []
        start_at = 0
        while True:
            page = self._request(
                "GET",
                "/rest/api/2/search",
                params={
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": _SEARCH_PAGE_SIZE,
                    "fields": fields,
                },
            )
            batch = page.get("issues") or []
            if not isinstance(batch, list):
                break
            issues.extend(i for i in batch if isinstance(i, dict))
            total = page.get("total")
            start_at += len(batch)
            if not batch or not isinstance(total, int) or start_at >= total:
                break
        return issues

    # --- TicketTransport -----------------------------------------------------

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        """Create an issue via ``POST /rest/api/2/issue`` (name-based assignee)."""
        raw_summary = (ticket_data.get("title") or "").strip()
        if not raw_summary:
            raise ValueError(
                f"Cannot create Jira issue: title/summary is empty "
                f"(ticket_data keys: {list(ticket_data.keys())})"
            )
        fields: dict[str, Any] = {
            "project": {"key": self._project},
            "summary": raw_summary,
            "issuetype": {"name": (ticket_data.get("ticket_type") or "Task").capitalize()},
        }
        if ticket_data.get("description"):
            fields["description"] = str(ticket_data["description"])
        if ticket_data.get("priority") is not None:
            fields["priority"] = {"name": ticket_data["priority"]}
        if ticket_data.get("assignee"):
            fields["assignee"] = {"name": str(ticket_data["assignee"])}
        parent = ticket_data.get("parent")
        parent_key = parent.get("key") if isinstance(parent, dict) else parent
        if parent_key:
            fields["parent"] = {"key": parent_key}
        return self._request("POST", "/rest/api/2/issue", body={"fields": fields})

    def get_issue(self, remote_id: str) -> dict[str, Any]:
        """Fetch one issue via ``GET /rest/api/2/issue/{key}`` (``{"key","fields"}``)."""
        return self._request(
            "GET",
            f"/rest/api/2/issue/{remote_id}",
            params={"fields": "issuetype,key,assignee,priority,status,summary,description,labels"},
        )

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]:
        """Apply a mutation-field subset via ``PUT /rest/api/2/issue/{key}``.

        ``status`` routes to a workflow transition (not a settable field) and
        ``assignee`` to the name-based assignee endpoint; the remainder become
        ``fields`` on a single PUT. The ``_assignee_is_account_id`` sentinel from
        the Cloud dispatch path is ignored: Data Center identifies users by name.
        """
        kwargs.pop("_assignee_is_account_id", None)
        status = kwargs.pop("status", None)
        assignee = kwargs.pop("assignee", _UNSET)

        fields: dict[str, Any] = {}
        for name, value in kwargs.items():
            if name == "priority":
                fields["priority"] = {"name": value}
            elif name == "parent":
                fields["parent"] = {"key": value.get("key") if isinstance(value, dict) else value}
            elif name == "reporter":
                fields["reporter"] = {"name": value}
            else:
                fields[name] = value
        if fields:
            self._request("PUT", f"/rest/api/2/issue/{remote_id}", body={"fields": fields})
        if assignee is not _UNSET:
            self._set_assignee(remote_id, assignee)
        if status:
            self.transition_issue_by_name(remote_id, status)
        return {"key": remote_id}

    def _set_assignee(self, remote_id: str, assignee: str | None) -> None:
        """Set or clear the assignee via ``PUT /rest/api/2/issue/{key}/assignee``."""
        name = assignee if assignee else None
        self._request(
            "PUT", f"/rest/api/2/issue/{remote_id}/assignee", body={"name": name}
        )

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None:
        """Transition an issue to ``target_status`` via the v2 transitions endpoint."""
        listing = self._request("GET", f"/rest/api/2/issue/{remote_id}/transitions")
        target = target_status.strip().lower()
        transitions = listing.get("transitions") or []
        match = None
        for tr in transitions:
            if not isinstance(tr, dict):
                continue
            if (tr.get("name") or "").strip().lower() == target:
                match = tr
                break
            to_name = (tr.get("to") or {}).get("name") if isinstance(tr.get("to"), dict) else None
            if (to_name or "").strip().lower() == target:
                match = tr
                break
        if match is None:
            available = ", ".join(
                str((t or {}).get("name")) for t in transitions if isinstance(t, dict)
            )
            raise RuntimeError(
                f"no transition to {target_status!r} available for {remote_id} "
                f"(available: {available})"
            )
        self._request(
            "POST",
            f"/rest/api/2/issue/{remote_id}/transitions",
            body={"transition": {"id": match.get("id")}},
        )

    def add_label(self, remote_id: str, label: str) -> None:
        """Add one label via a ``PUT`` update op (``{"update":{"labels":[{"add":...}]}}``)."""
        self._request(
            "PUT",
            f"/rest/api/2/issue/{remote_id}",
            body={"update": {"labels": [{"add": label}]}},
        )

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]:
        """Search via ``GET /rest/api/2/search``, returning one page of issues."""
        page = self._request(
            "GET",
            "/rest/api/2/search",
            params={
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": "issuetype,key,assignee,priority,status,summary,description,labels",
            },
        )
        issues = page.get("issues") or []
        return [i for i in issues if isinstance(i, dict)]

    # --- SupportsLinks -------------------------------------------------------

    def set_relationship(
        self, from_id: str, to_id: str, link_type: str = "Blocks"
    ) -> dict[str, Any]:
        """Create ``from_id <link_type> to_id`` via ``POST /rest/api/2/issueLink``.

        The v2 payload names the outward direction explicitly, so the ACLI
        ``--out``/``--in`` inversion (Cloud bug 3b86) does not apply: ``from_id`` is
        the outward (blocking) endpoint and ``to_id`` the inward endpoint.
        """
        self._request(
            "POST",
            "/rest/api/2/issueLink",
            body={
                "type": {"name": link_type},
                "outwardIssue": {"key": from_id},
                "inwardIssue": {"key": to_id},
            },
        )
        return {"status": "created", "from": from_id, "to": to_id}

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]:
        """Return ``{issue_key: issuelinks_list}`` for the project via one paged search."""
        result: dict[str, Any] = {}
        for issue in self._search_all(f"project = {project_key}", ["issuelinks"]):
            key = issue.get("key")
            links = (issue.get("fields") or {}).get("issuelinks")
            if key and isinstance(links, list):
                result[key] = links
        return result

    # --- SupportsComments ----------------------------------------------------

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        """Add a plain-text comment via ``POST /rest/api/2/issue/{key}/comment``."""
        return self._request(
            "POST", f"/rest/api/2/issue/{remote_id}/comment", body={"body": body}
        )

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        """Return ``{issue_key: comment_field_dict}`` for the project via one paged search."""
        result: dict[str, Any] = {}
        for issue in self._search_all(f"project = {project_key}", ["comment"]):
            key = issue.get("key")
            comment = (issue.get("fields") or {}).get("comment")
            if key and isinstance(comment, dict):
                result[key] = comment
        return result

    # --- absence probe -------------------------------------------------------

    def probe_issue(self, remote_id: str) -> tuple[int, dict[str, Any]]:
        """GET ``status,resolution`` for the probe classifier; returns ``(status, payload)``."""
        return self._get_allow_status(
            f"/rest/api/2/issue/{remote_id}", params={"fields": "status,resolution"}
        )
