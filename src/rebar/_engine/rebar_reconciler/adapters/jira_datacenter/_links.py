"""Issue-link + comment mixins for the Jira Data Center transport (ticket
465d, epic e369) — the ``SupportsLinks`` and ``SupportsComments`` capabilities.

Co-located in one module: read against the current method inventory, each
capability alone (links: 3 methods + the bulk map; comments: 1 write + 2 reads)
falls under the module-size policy's 100-LOC floor for a split fragment, and
the two are adjacent Protocols in ``_backend.py`` with the same shape (a
mutate + a per-item read + a paged project-wide map). ``_hierarchy.py``'s and
``_issues.py``'s capabilities are each large enough to stand alone; these two
are not, so they stand together rather than as two sub-100-line files.

RELOCATED VERBATIM out of ``transport.py``; this module changes no behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter._base import _call_logged, _TransportBase, _unwrap
from rebar_reconciler.adapters.jira_datacenter.retry import _with_connection_retry


class _LinksMixin(_TransportBase):
    """``SupportsLinks``: create/list/delete issue links + the bulk links map."""

    if TYPE_CHECKING:
        # Provided by the sibling ``_IssuesMixin``, resolved via the composed
        # transport's MRO. Declared type-only so mypy sees this mixin's surface.
        def get_issue(self, remote_id: str) -> dict[str, Any]: ...

    def set_relationship(
        self, from_id: str, to_id: str, link_type: str = "Blocks"
    ) -> dict[str, Any]:
        _with_connection_retry(lambda: self._client.create_issue_link(link_type, from_id, to_id))
        return self.get_issue(from_id)

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]:
        issues = self._paged_search(f"project = {project_key}", rate_limit_retry=True)
        return {
            issue["key"]: list(issue.get("fields", {}).get("issuelinks") or []) for issue in issues
        }

    def get_issue_links(self, remote_id: str) -> list[dict[str, Any]]:
        """The issue's ``issuelinks`` in REST-nested shape — the shape
        ``JiraDataCenterBackend.map_remote_links`` already canonicalizes and the
        shape ``dispatch_one._index_existing_links`` indexes. REST v2 and v3 carry
        an identical ``issuelinks`` payload, so no translation is needed."""
        issue = _call_logged("get_issue_links", remote_id, lambda: self._client.issue(remote_id))
        raw = _unwrap(issue)
        fields = raw.get("fields") if isinstance(raw, dict) else None
        links = fields.get("issuelinks") if isinstance(fields, dict) else None
        return links if isinstance(links, list) else []

    def delete_issue_link(self, link_id: str) -> dict[str, Any]:
        """Delete an issue link by id, absorbing 404/409 as idempotent success
        **inside the transport**.

        That absorption is NOT redundant with the caller. ``dispatch_one`` treats a
        concurrent-removal failure as success only when it sees
        ``subprocess.CalledProcessError`` — a Cloud/ACLI-specific type, as its own
        comment says ("delete_issue_link shells out via ACLI"). DC raises
        ``BackendHTTPError``, which does not match that clause, so a raced 404 would
        escape and unwind the pass. Owning idempotence here makes the DC path
        correct under a handler written for a different transport.
        """
        try:
            _call_logged(
                "delete_issue_link", link_id, lambda: self._client.delete_issue_link(link_id)
            )
        except BackendHTTPError as exc:
            if exc.code in (404, 409):
                return {"status": "already_absent", "id": link_id}
            raise
        return {"status": "deleted", "id": link_id}


class _CommentsMixin(_TransportBase):
    """``SupportsComments``: add a comment, and the two read shapes core uses —
    per-issue ``get_comments`` and the bulk ``get_comment_map``."""

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        comment = _with_connection_retry(lambda: self._client.add_comment(remote_id, body))
        return _unwrap(comment)

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        issues = self._paged_search(f"project = {project_key}", rate_limit_retry=True)
        out: dict[str, Any] = {}
        for issue in issues:
            key = issue["key"]
            # PATH B: this per-issue fetch does NOT route through `_paged_search` or
            # `_call_logged`, so it must opt in DIRECTLY. It is one request PER ISSUE —
            # the highest-volume read in a pass, and therefore the call most likely to
            # trip a token bucket. Threading the flag only through `_call_logged` would
            # have left exactly this one unprotected while every test passed.
            comments = _with_connection_retry(
                lambda k=key: self._client.comments(k), rate_limit_retry=True
            )
            out[key] = {"comments": [_unwrap(c) for c in comments]}
        return out

    def get_comments(self, remote_id: str) -> list[dict[str, Any]]:
        """All comments on an issue, as raw payload dicts.

        ``get_comment_map`` already made this exact call (``client.comments``);
        what was missing was the SURFACE, not the capability. The library pages
        internally, so there is no Cloud-style ``--paginate`` caveat here.
        """
        comments = _call_logged("get_comments", remote_id, lambda: self._client.comments(remote_id))
        return [_unwrap(c) for c in comments]
