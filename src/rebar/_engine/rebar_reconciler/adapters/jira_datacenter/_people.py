"""Provide identity, assignment, reporter, and issue-property operations.

``AssigneeNotFoundError`` stays beside the Jira Data Center methods that raise
it and is re-exported by ``transport``.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler._backend import BackendAssigneeNotFoundError, BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter._base import (
    _call_logged,
    _TransportBase,
    _unwrap,
    _user_attr,
)
from rebar_reconciler.adapters.jira_datacenter.retry import _with_connection_retry


class AssigneeNotFoundError(BackendAssigneeNotFoundError, ValueError):
    """A requested DC assignee (Jira ``name``) resolves to no assignable user.

    Subclasses the vendor-neutral ``BackendAssigneeNotFoundError`` (``_backend.py``)
    so core apply-path ``except`` clauses catch it without importing anything
    DC-specific — mirroring ``adapters/jira/acli_subprocess.AssigneeNotFoundError``.
    """


class _PeopleMixin(_TransportBase):
    """Assignment (``_assign``), reporter writes, and assignee validation."""

    def _assign(self, remote_id: str, assignee: Any) -> None:
        """Assign a Data Center username, or pass ``None`` to unassign blank input.

        The client treats empty strings as user searches, so whitespace is
        normalized before calling it. A definitive lookup miss becomes
        ``AssigneeNotFoundError`` after shared HTTP translation.
        """
        if assignee is None or (isinstance(assignee, str) and not assignee.strip()):
            assignee = None
        try:
            _with_connection_retry(lambda: self._client.assign_issue(remote_id, assignee))
        except BackendHTTPError as exc:
            raise AssigneeNotFoundError(
                f"assignee {assignee!r} could not be resolved to a DC user on {remote_id}: {exc}"
            ) from exc

    def set_reporter(self, remote_id: str, account_id: str) -> None:
        """Set the reporter to a DC **username**.

        DC has no ``accountId``, so the payload is ``{"reporter": {"name": …}}``
        rather than Cloud's ``{"accountId": …}``. The parameter keeps Cloud's name
        because the core's identity seam (``jira_account_id``) is already
        vendor-neutral: it hands back whatever ``external_id`` the family stored,
        which for DC identities is the username. No call-site change is needed.

        Idempotence depends on the INBOUND half: ``inbound_fields._identity_of``
        must carry DC's ``name`` into the canonical identity's ``account_id`` key,
        or the next snapshot reads ``None`` and the differ re-emits the reporter
        mutation on every pass.
        """
        issue = _call_logged("set_reporter", remote_id, lambda: self._client.issue(remote_id))
        _call_logged(
            "set_reporter",
            remote_id,
            lambda: issue.update(fields={"reporter": {"name": account_id}}),
        )

    def validate_assignee_exists(
        self,
        assignee: str,
        *,
        issue_key: str | None = None,
        project_key: str | None = None,
    ) -> str:
        """Resolve an assignee to its Data Center username.

        Match exact username, email, then display name because server search can
        return partial matches. Return the username as the authoritative DC
        identity. A definitive miss raises ``AssigneeNotFoundError``.
        """
        scope = issue_key or project_key or assignee
        users = _call_logged(
            "validate_assignee_exists",
            scope,
            lambda: self._client.search_users(user=assignee, maxResults=50),
        )
        candidates = list(users or [])
        for field in ("name", "emailAddress", "displayName"):
            for user in candidates:
                if _user_attr(user, field) == assignee:
                    return str(_user_attr(user, "name") or assignee)
        raise AssigneeNotFoundError(
            f"validate_assignee_exists: no assignable Data Center user exactly matches "
            f"{assignee!r} (scope {scope!r})"
        )


class _PropertiesMixin(_TransportBase):
    """Issue-property reads and issue/entity-property writes."""

    def get_issue_property(self, remote_id: str, property_key: str) -> Any:
        """Return the JSON value stored under one issue-property key."""
        prop = _call_logged(
            "get_issue_property",
            remote_id,
            lambda: self._client.issue_property(remote_id, property_key),
        )
        return _unwrap(prop.value)

    def _put_property(self, member: str, remote_id: str, property_key: str, value: Any) -> None:
        """Store ``value`` verbatim under the issue property key.

        ``member`` carries the public operation name into failure logs.
        """
        _call_logged(
            member,
            remote_id,
            lambda: self._client.add_issue_property(remote_id, property_key, value),
        )

    def set_issue_property(self, remote_id: str, property_key: str, value: Any) -> None:
        """Set an issue property (``PUT issue/{key}/properties/{prop}``, value verbatim)."""
        self._put_property("set_issue_property", remote_id, property_key, value)

    def set_entity_property(self, remote_id: str, prop_name: str, value: Any) -> None:
        """Set an entity property — the same endpoint as :meth:`set_issue_property`
        (Cloud's is literally an alias of it, ``acli_rest.py:266``).

        This is the member whose absence crashed the first live DC writing pass.
        """
        self._put_property("set_entity_property", remote_id, prop_name, value)
