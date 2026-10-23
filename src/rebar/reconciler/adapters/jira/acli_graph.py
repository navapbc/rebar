#!/usr/bin/env python3
"""AcliClient graph/relationship mixin.

The graph-shaped Jira operations of the ACLI client: additive label add/remove,
issue links (create/list/delete + link-type list), bulk parent/comment maps via
paged REST search, comment update/delete, and priority/issuetype edits. Some
dispatch through the ACLI subprocess (``self._run``), others through the REST
helpers (``self._direct_rest_*``) — both resolved via the AcliClient MRO.

Mixed into ``AcliClient`` (``acli.py``); method bodies are unchanged from the
monolith.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
from typing import TYPE_CHECKING, Any

from rebar.reconciler._backend import BackendPaginationStallError
from rebar.reconciler.adapters.jira_family import sanitize_label as _sanitize_label

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterator


class RunawayPaginationError(BackendPaginationStallError):
    """Signal a repeated non-null page token instead of silently truncating a read.

    The vendor-neutral base lets snapshot fetches re-raise cursor stalls past
    their per-enrichment fail-open handlers.
    """


def _iter_cursor_pages(
    post: Callable[[str, dict[str, Any]], Any],
    path: str,
    base_body: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Yield cursor pages until ``isLast`` or a missing token.

    Start with ``base_body`` unchanged, then add ``nextPageToken``. A non-dict
    response ends the walk; transport errors reach the caller. Repeated non-null
    tokens raise ``RunawayPaginationError`` to prevent silent truncation.
    """
    next_page_token: str | None = None
    while True:
        body = dict(base_body)
        if next_page_token is not None:
            body["nextPageToken"] = next_page_token
        resp = post(path, body)
        if not isinstance(resp, dict):
            return
        token = None if resp.get("isLast") else resp.get("nextPageToken")
        if token and token == next_page_token:
            raise RunawayPaginationError(
                f"cursor walk on {path} stalled: the server returned "
                f"nextPageToken {token!r} twice consecutively; aborting the "
                f"walk rather than spinning [rebar:cabc-7a98-d173-4d7c]"
            )
        yield resp
        if not token:
            return
        next_page_token = token


def _issue_local_id_property(issue: dict[str, Any]) -> str:
    """Extract ``local_id`` from a Jira search issue's included properties."""
    props = issue.get("properties")
    if isinstance(props, dict):
        raw = props.get("local_id")
    elif isinstance(props, list):
        raw = None
        for prop in props:
            if isinstance(prop, dict) and prop.get("key") == "local_id":
                raw = prop.get("value")
                break
    else:
        raw = None
    if isinstance(raw, dict) and "value" in raw:
        raw = raw["value"]
    return str(raw) if raw else ""


class AcliGraphMixin:
    """Labels, links, parent/comment maps, and field-edit ops for AcliClient."""

    if TYPE_CHECKING:
        # Transport helpers provided by the composed ``AcliClient`` (``_run``
        # from acli.py; ``_direct_rest_*`` from AcliRestMixin), resolved via the
        # MRO at runtime. Declared type-only so mypy sees this mixin's surface.
        def _run(
            self,
            cmd: list[str],
            *,
            retry_on_timeout: bool = ...,
        ) -> subprocess.CompletedProcess[str]: ...

        def _direct_rest_get(self, path: str) -> Any: ...

        def _direct_rest_put_raw(self, path: str, body: Any) -> None: ...

        def _direct_rest_post_json(self, path: str, body: Any) -> Any: ...

        def _direct_rest_delete(self, path: str) -> None: ...

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
        result = self._run(cmd, retry_on_timeout=True)  # READ — idempotent
        parsed = json.loads(result.stdout or "[]")
        if isinstance(parsed, list):
            return parsed
        # Some ACLI versions wrap the list in a dict under "issueLinkTypes"
        if isinstance(parsed, dict) and "issueLinkTypes" in parsed:
            return parsed["issueLinkTypes"]
        return []

    def add_label(self, jira_key: str, label: str) -> None:
        # Sanitize before reaching ACLI so we fail fast on invalid labels rather
        # than emitting a malformed mutation against live Jira.
        label = _sanitize_label(label)
        return self._add_label_impl(jira_key, label)

    def _add_label_impl(self, jira_key: str, label: str) -> None:
        """Add a label without replacing existing labels.

        Use ACLI ``--from-json`` with ``labelsToAdd`` and ``--yes``. The
        ``--labels`` flag replaces the whole set; repeated additive calls succeed.
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

    def get_parent_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, str | None]:
        """Return Jira keys mapped to parents through paged REST search.

        Search ``fields=["parent"]`` because ACLI rejects that field selector.
        ``jql`` overrides the project query. REST failures log and return any
        pages collected so far (HTTP 410 at ERROR); a stalled cursor raises.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        effective_jql = jql or f"project = {project}"
        result: dict[str, str | None] = {}
        base_body: dict[str, Any] = {
            "jql": effective_jql,
            "maxResults": 100,
            "fields": ["parent"],
        }
        try:
            for resp in _iter_cursor_pages(
                self._direct_rest_post_json, "/rest/api/3/search/jql", base_body
            ):
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
        except RunawayPaginationError:
            # A stalled cursor means a truncated whole-project map the differ
            # would treat as authoritative. Loud beats fail-open here.
            raise
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
        except Exception as exc:  # noqa: BLE001 — fail-open: degrade gracefully, parent data absent
            _log.warning(
                "get_parent_map: REST search failed: %r; "
                "degrading gracefully — parent data will be absent this pass",
                exc,
            )

        return result

    def get_comment_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, Any]:
        """Return raw Jira comment fields from one paged REST search.

        Omit issues without a comment field so callers can fetch them directly.
        ``jql`` overrides the project query. REST failures log and return any
        pages collected so far (HTTP 410 at ERROR); a stalled cursor raises.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        effective_jql = jql or f"project = {project}"
        result: dict[str, Any] = {}
        base_body: dict[str, Any] = {
            "jql": effective_jql,
            "maxResults": 100,
            "fields": ["comment"],
        }
        try:
            for resp in _iter_cursor_pages(
                self._direct_rest_post_json, "/rest/api/3/search/jql", base_body
            ):
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
        except RunawayPaginationError:
            # A stalled cursor means a truncated whole-project map. Loud beats
            # fail-open here — see _iter_cursor_pages.
            raise
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
        except Exception as exc:  # noqa: BLE001 — fail-open: degrade to per-ticket fallback
            _log.warning(
                "get_comment_map: REST search failed: %r; "
                "degrading gracefully — per-ticket fallback applies",
                exc,
            )

        return result

    def get_local_id_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, str]:
        """Return ``{jira_key: local_id}`` via one paged REST search.

        Jira issue properties are identity data for duplicate suppression. They
        must be fetched in bulk, not as one REST GET per issue; a property-read
        failure raises so the reconciler does not proceed with an ambiguous
        snapshot that can create duplicates.
        """
        effective_jql = jql or f"project = {project}"
        result: dict[str, str] = {}
        base_body: dict[str, Any] = {
            "jql": effective_jql,
            "maxResults": 100,
            "fields": [],
            "properties": ["local_id"],
        }
        for resp in _iter_cursor_pages(
            self._direct_rest_post_json, "/rest/api/3/search/jql", base_body
        ):
            issues = resp.get("issues") or []
            if not isinstance(issues, list):
                break
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                local_id = _issue_local_id_property(issue)
                if key and local_id:
                    result[str(key)] = local_id
        return result

    def get_issuelinks_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, Any]:
        """Return REST-shaped issue-link lists from one paged search.

        The map supplies link data to both differs. REST failures log and return
        any pages collected so far (HTTP 410 at ERROR); a stalled cursor raises.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        effective_jql = jql or f"project = {project}"
        result: dict[str, Any] = {}
        base_body: dict[str, Any] = {
            "jql": effective_jql,
            "maxResults": 100,
            "fields": ["issuelinks"],
        }
        try:
            for resp in _iter_cursor_pages(
                self._direct_rest_post_json, "/rest/api/3/search/jql", base_body
            ):
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
                    links = fields.get("issuelinks")
                    # Record any issue the search returned an issuelinks list for
                    # (including the empty list — an authoritative "no links" that
                    # lets the outbound differ's dedup treat the issue as known).
                    if isinstance(links, list):
                        result[key] = links
        except RunawayPaginationError:
            # A stalled cursor means a truncated whole-project map. Loud beats
            # fail-open here — see _iter_cursor_pages.
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 410:
                _log.error(
                    "get_issuelinks_map: endpoint POST /rest/api/3/search/jql "
                    "returned HTTP 410 GONE — the Jira search endpoint has been "
                    "RETIRED; issuelink enrichment is unavailable this pass. "
                    "API retirement, not a transient fault: %r",
                    exc,
                )
            else:
                _log.warning(
                    "get_issuelinks_map: REST search failed (HTTP %s): %r; "
                    "degrading gracefully (no issuelink enrichment this pass)",
                    exc.code,
                    exc,
                )
        except Exception as exc:  # noqa: BLE001 — fail-open: degrade to no enrichment
            _log.warning(
                "get_issuelinks_map: REST search failed: %r; "
                "degrading gracefully (no issuelink enrichment this pass)",
                exc,
            )

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

    def update_comment(self, jira_key: str, comment_id: str, body: str) -> dict[str, Any]:
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
        """Create a link so that ``from_key <link_type> to_key`` (e.g. "from blocks to").

        Raises subprocess.CalledProcessError on ACLI failure.
        """
        # ACLI uses ``--in`` for the blocker and ``--out`` for the blocked
        # issue. ``--json`` is rejected here; ``--yes`` prevents an EOF prompt.
        cmd = [
            "jira",
            "workitem",
            "link",
            "create",
            "--out",
            to_key,
            "--in",
            from_key,
            "--type",
            link_type,
            "--yes",
        ]
        self._run(cmd)  # raises on failure — no silent swallowing
        return {"status": "created", "from": from_key, "to": to_key}

    def get_issue_links(self, jira_key: str) -> list[dict[str, Any]]:
        """Get existing issue links for a Jira issue, in REST-nested shape:
        ``[{"id", "type": {"name", "inward", "outward"},
            "inwardIssue": {"key", ...}|absent, "outwardIssue": {"key", ...}|absent}]``.

        Reads via the REST API rather than the ACLI ``link list`` command: the
        latter does not report the linked issue key (it returns
        ``outwardIssueKey: null``), so it cannot identify what a link points to.
        ``GET /rest/api/3/issue/{key}?fields=issuelinks`` returns the canonical
        shape with the linked-issue keys the differs and callers need.

        Raises on REST failure (via ``_direct_rest_get``).
        """
        data = self._direct_rest_get(f"/rest/api/3/issue/{jira_key}?fields=issuelinks")
        fields = data.get("fields") if isinstance(data, dict) else None
        links = fields.get("issuelinks") if isinstance(fields, dict) else None
        return links if isinstance(links, list) else []

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
            # ACLI rejects ``--json`` for link delete; ``--yes`` avoids an EOF
            # prompt. ``_run`` raises on failure, so the return is synthesized.
            "--yes",
        ]
        self._run(cmd)
        return {"status": "deleted", "link_id": link_id}
