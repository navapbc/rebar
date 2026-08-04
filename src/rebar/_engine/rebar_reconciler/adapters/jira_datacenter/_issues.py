"""Core issue CRUD + status/label mutation mixin for the Jira Data Center
transport (ticket 465d, epic e369) — the ``TicketTransport`` capability.

RELOCATED VERBATIM out of ``transport.py``; this module changes no behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter._base import (
    _MISSING,
    _call_logged,
    _TransportBase,
    _unwrap,
)
from rebar_reconciler.adapters.jira_datacenter.retry import _with_connection_retry
from rebar_reconciler.adapters.jira_datacenter.transitions import (
    route_status_to_transition,
    transition_to_status,
)


class _IssuesMixin(_TransportBase):
    """``create``/``read``/``update``/``delete``/``search`` + label + transition
    members — the always-present ``TicketTransport`` surface."""

    if TYPE_CHECKING:
        # Provided by the sibling ``_PeopleMixin``, resolved via the composed
        # transport's MRO. Declared type-only so mypy sees this mixin's surface.
        def _assign(self, remote_id: str, assignee: Any) -> None: ...

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        fields = dict(ticket_data)
        fields.setdefault("project", {"key": self.project})
        issue = _with_connection_retry(lambda: self._client.create_issue(**fields))
        return _unwrap(issue)

    def get_issue(self, remote_id: str) -> dict[str, Any]:
        issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        return _unwrap(issue)

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]:
        """Apply an outbound field update, ROUTING ``status`` to a transition.

        ``status`` is not an editable Jira field. It arrives here anyway —
        ``dispatch_apply_phases._OUTBOUND_BATCH_ALLOWLIST`` contains it and
        ``dispatch_one._update_one_scalar_update`` forwards the whole allowlisted
        dict as ``update_issue(key, **fields)`` — and this method used to hand it
        straight to ``issue.update(fields=…)``, a REST field EDIT. Jira rejected it,
        the rejection was soft-failed, and the outbound status silently never
        changed (bug d067). Cloud does the same translation inside its own transport
        (``adapters/jira/acli.py:170,182-183``); the status→transition seam lives
        PER TRANSPORT, and Data Center simply never got its half.

        ``status`` and ``assignee`` are therefore both popped BEFORE the field edit,
        which then carries only genuinely editable fields — the field edit and the
        transition happen in this one call, in that order, so a mutation that
        changes a summary and a status still does both.
        """
        assignee = kwargs.pop("assignee", _MISSING)
        status = kwargs.pop("status", _MISSING)
        if kwargs:
            issue = _with_connection_retry(lambda: self._client.issue(remote_id))
            _with_connection_retry(lambda: issue.update(fields=kwargs))
        if assignee is not _MISSING:
            self._assign(remote_id, assignee)
        if status is not _MISSING and status is not None:
            route_status_to_transition(self._client, remote_id, str(status))
        return self.get_issue(remote_id)

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None:
        """Move ``remote_id`` to ``target_status``, resolving EITHER spelling.

        A transition's NAME is not its destination STATUS name, and every production
        caller passes the latter (bug 7f93); both resolve here. The resolution rules,
        the ambiguity refusal, and why they are what they are live with the code in
        :func:`transitions.resolve_transition`. Raises ``ValueError`` when no
        transition reaches the requested state — the error type callers already
        expect, so this stays a pure delegation.
        """
        transition_to_status(self._client, remote_id, target_status)

    def add_label(self, remote_id: str, label: str) -> None:
        """Append ``label`` without resetting the issue's existing labels.

        ``add_field_value`` lives on the ISSUE resource, not on the client:
        ``jira.JIRA`` has no such attribute (verified against jira 3.10.5), so the
        earlier client-level call raised ``AttributeError`` on every invocation —
        this method could never have worked. It shipped because it had no test at
        any tier; the live test that now covers it caught the bug on its first
        real execution. ``Issue.add_field_value(field, value)`` is documented as
        "add a value to a field that supports multiple values, without resetting
        the existing values ... should work with: labels", which is exactly the
        append semantics this method's callers expect (a read-modify-write of the
        whole ``labels`` list would clobber concurrent edits).
        """
        issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        _with_connection_retry(lambda: issue.add_field_value("labels", label))

    def remove_label(self, remote_id: str, label: str) -> None:
        """Remove ONE label, leaving every other label intact.

        This is **not** the mirror image of :meth:`add_label`. ``add_label`` uses
        ``Issue.add_field_value("labels", …)``, which has no removal counterpart;
        removal goes through ``Issue.update``'s ``update`` verb —
        ``{"labels": [{"remove": <label>}]}`` — which is target-specific, so a
        concurrent label edit is not clobbered the way a read-modify-write of the
        whole list would clobber it.
        """
        issue = _call_logged("remove_label", remote_id, lambda: self._client.issue(remote_id))
        _call_logged(
            "remove_label",
            remote_id,
            lambda: issue.update(update={"labels": [{"remove": label}]}),
        )

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]:
        results = _with_connection_retry(
            lambda: self._client.search_issues(jql, startAt=start_at, maxResults=max_results)
        )
        return [_unwrap(issue) for issue in results]

    # ------------------------------------------------------------------
    # The twelve members the core reaches for (story J9). Each is written
    # against DC REST **v2** via ``pycontribs/jira`` — never by copying Cloud's
    # v3 endpoint, and never by hand-rolled REST. Every one routes through
    # ``_call_logged`` so a failure at a call site that swallows
    # ``Exception`` still leaves a WARNING naming the member and the remote id.
    # ------------------------------------------------------------------

    def get_issue_by_rest(self, remote_id: str) -> dict[str, Any]:
        """Read an issue straight from the primary store (no search-index lag).

        On DC this is the SAME call ``get_issue`` makes — ``client.issue(key)`` is
        already ``GET /rest/api/2/issue/{key}``. Cloud needs the distinction only
        because its ``get_issue`` goes through ACLI's JQL search; DC has no such
        indirection, so the two coincide. The method still exists separately
        because ``outbound_differ`` calls it by name, and it is the ONE member of
        the twelve whose call site lets the error propagate.
        """
        issue = _call_logged("get_issue_by_rest", remote_id, lambda: self._client.issue(remote_id))
        return _unwrap(issue)

    def delete_issue(self, remote_id: str) -> dict[str, Any]:
        """Delete an issue (``Issue.delete()`` — ``DELETE /rest/api/2/issue/{key}``).

        A 404 is idempotent success (the post-state we want is "gone"), matching
        Cloud's contract; a 403 becomes ``PermissionError`` so the rollback callers
        that special-case a permissions denial keep behaving identically.
        """

        def _delete() -> None:
            issue = self._client.issue(remote_id)
            issue.delete()

        try:
            _call_logged("delete_issue", remote_id, _delete)
        except BackendHTTPError as exc:
            if exc.code == 404:
                return {"status": "already_absent", "key": remote_id}
            if exc.code == 403:
                raise PermissionError(
                    f"permission denied deleting {remote_id} on Jira Data Center: {exc}"
                ) from exc
            raise
        return {"status": "deleted", "key": remote_id}
