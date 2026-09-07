"""Core issue CRUD + status/label mutation mixin for the Jira Data Center
transport (ticket 465d, epic e369) — the ``TicketTransport`` capability.

Extracted from ``transport.py`` under the module-size cap (see ADR 0058); no behaviour change.
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
from rebar_reconciler.adapters.jira_family import sanitize_summary as _sanitize_summary
from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec, cutover_clients

#: Bridge-schema keys the create payload carries for Cloud's ``AcliClient`` and that
#: Jira has no field for — forwarded as field ids they 400 the WHOLE create. Their
#: content is not lost: it is translated into ``summary``/``issuetype`` below.
_BRIDGE_ONLY_CREATE_FIELDS: frozenset[str] = frozenset(
    {"title", "ticket_type", "_bridge_target_project"}
)

#: Fields Jira refuses to SET at create time regardless of spelling. ``status`` is
#: not a rejected name — a status is reached by a workflow transition, never by a
#: create-time field write — so it is dropped here exactly as Cloud drops it, and
#: the outbound status lands later through ``route_status_to_transition``.
_UNSETTABLE_AT_CREATE_FIELDS: frozenset[str] = frozenset({"status"})


# Build the REST v2 wiki codec per call so the rich-text cutover and character
# limit remain current. The backend description sanitizer uses the same contract.
def _description_codec() -> WikiTextCodec:
    return WikiTextCodec(rich="dc" in cutover_clients())


def _create_summary(ticket_data: dict[str, Any]) -> str:
    """Prefer ``title``, fall back to ``summary``, and reject an empty value."""
    stripped = ""
    for key in ("title", "summary"):
        # A whitespace-only value counts as absent, so a blank ``title`` falls
        # through to the differ's ``summary`` instead of aborting the create.
        candidate = ticket_data.get(key)
        if candidate is not None and str(candidate).strip():
            stripped = str(candidate).strip()
            break
    if not stripped:
        raise ValueError(
            "cannot create a Jira Data Center issue: neither 'title' nor 'summary' "
            f"carries a non-empty headline (payload keys: {sorted(ticket_data)})"
        )
    # Data Center hard-rejects an over-length summary with a 400 (measured against
    # 8.17.1); truncate rather than fail the pass on one oversize ticket.
    return _sanitize_summary(stripped)


def _create_issuetype(ticket_data: dict[str, Any]) -> dict[str, str]:
    """Normalize ``ticket_type`` or ``issuetype`` into Jira's name object.

    Strings and existing objects are accepted. Missing or unusable values default
    to ``Task``.
    """
    bridge_type = ticket_data.get("ticket_type")
    if isinstance(bridge_type, str) and bridge_type.strip():
        return {"name": bridge_type.strip().capitalize()}
    jira_type = ticket_data.get("issuetype")
    if isinstance(jira_type, dict):
        # Already Jira-canonical (``{"name": "Sub-task"}``) — do NOT recapitalize it.
        name = str(jira_type.get("name") or "").strip()
        if name:
            return {"name": name}
    elif isinstance(jira_type, str) and jira_type.strip():
        return {"name": jira_type.strip()}
    return {"name": "Task"}


def _translate_create_fields(ticket_data: dict[str, Any]) -> dict[str, Any]:
    """Translate a dual-schema create payload into Jira fields.

    Rewrite summary, issue type, and description. Remove bridge-only and
    create-unsettable fields. Preserve other valid Jira fields, including custom
    fields.
    """
    fields = {
        name: value
        for name, value in ticket_data.items()
        if name not in _BRIDGE_ONLY_CREATE_FIELDS and name not in _UNSETTABLE_AT_CREATE_FIELDS
    }
    fields["summary"] = _create_summary(ticket_data)
    fields["issuetype"] = _create_issuetype(ticket_data)
    description = fields.get("description")
    if isinstance(description, str):
        # Render and fit create descriptions through the wiki codec used by updates.
        codec = _description_codec()
        fields["description"] = codec.to_wire(codec.fit_outbound(description))
    # Wrap string priority and assignee values by ``name``, and parent by ``key``.
    # Existing objects remain unchanged.
    for name, wrapper in (("priority", "name"), ("assignee", "name"), ("parent", "key")):
        if name not in fields:
            continue
        value = fields[name]
        if isinstance(value, str) and value:
            fields[name] = {wrapper: value}
        elif not value:
            # Drop empty object-valued fields because Jira rejects null where it
            # expects an object.
            del fields[name]
    return fields


class _IssuesMixin(_TransportBase):
    """``create``/``read``/``update``/``delete``/``search`` + label + transition
    members — the always-present ``TicketTransport`` surface."""

    if TYPE_CHECKING:
        # Provided by the sibling ``_PeopleMixin``, resolved via the composed
        # transport's MRO. Declared type-only so mypy sees this mixin's surface.
        def _assign(self, remote_id: str, assignee: Any) -> None: ...

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        """Create an issue from the translated dual-schema payload.

        Bridge and Jira field names can coexist. Translation removes bridge-only
        names and routes status through a workflow transition rather than a create
        field. Other Jira fields pass through.
        """
        fields = _translate_create_fields(ticket_data)
        fields.setdefault(
            "project",
            {"key": ticket_data.get("_bridge_target_project") or self.project},
        )
        issue = _with_connection_retry(lambda: self._client.create_issue(**fields))
        return _unwrap(issue)

    def get_issue(self, remote_id: str) -> dict[str, Any]:
        issue = _with_connection_retry(lambda: self._client.issue(remote_id))
        return _unwrap(issue)

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]:
        """Apply editable fields, then route status through a workflow transition.

        Assignee uses its dedicated route. Status and assignee are removed before
        the general field update so a mixed mutation applies each operation once.
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
        """Append ``label`` through the issue resource without replacing labels."""
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

    # REST v2 transport members route through ``_call_logged`` so swallowed
    # failures retain member and remote-ID evidence.

    def get_issue_by_rest(self, remote_id: str) -> dict[str, Any]:
        """Read from REST v2 through ``client.issue``.

        The named primary-store entry point remains distinct for
        ``outbound_differ`` even though it shares ``get_issue`` mechanics.
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
