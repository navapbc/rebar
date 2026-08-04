"""Identity + property mixins for the Jira Data Center transport (ticket 465d,
epic e369) — assignment, reporter, assignee validation, and the
issue/entity-property writes.

Co-located in one module: read against the current method inventory, the
property-write cluster alone (one shared helper + two thin public callers)
falls under the module-size policy's 100-LOC floor for a split fragment, and
both clusters are single-field outbound writes against ``self._client`` with
no capability Protocol of their own in ``_backend.py`` (unlike
``SupportsLinks``/``SupportsComments``) — so they stand together rather than
as a full module plus a sub-100-line fragment.

RELOCATED VERBATIM out of ``transport.py``; this module changes no behaviour.
``AssigneeNotFoundError`` moves WITH the members that raise it (``_assign``,
``validate_assignee_exists``); ``transport.py`` re-exports it so
``transport.AssigneeNotFoundError`` keeps resolving for existing importers.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler._backend import BackendAssigneeNotFoundError, BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter._base import _call_logged, _TransportBase, _user_attr
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
        """Assign ``remote_id`` to ``assignee`` (a DC username), or UNASSIGN it when
        ``assignee`` is empty, raising ``AssigneeNotFoundError`` when the library/server
        reports a real user as unresolvable rather than letting a bare HTTP error escape.

        The HTTP error is caught as the already-translated ``BackendHTTPError``
        (:func:`_with_connection_retry` converts it), so this behaves exactly as
        before while needing no vendor import of its own.

        Bug 751e — THE EMPTY ASSIGNEE IS AN UNASSIGN, AND IT NEEDS TRANSLATING HERE.
        ``outbound_differ._assignee_resolver`` resolves an empty local assignee to the
        EMPTY STRING (not ``None``), and ``assignee`` is in
        ``dispatch_apply_phases._OUTBOUND_BATCH_ALLOWLIST``, so ``update_issue(key,
        assignee="")`` is what arrives. ``pycontribs``' ``JIRA._get_user_id`` passes only
        ``None``/``-1``/``"-1"`` through and SEARCHES for anything else, so an empty string
        is not an unassign instruction on that library at all: it either raises
        ``JIRAError("No matching user found for: ''")`` — soft-failed upstream, so the pass
        reports success while the assignee never moved — or, worse, returns a hit and
        assigns an ARBITRARY user. Verified at runtime against ``jira==3.10.5``: on a
        self-hosted instance ``None`` PUTs ``{"name": null}`` (Unassigned) while
        ``-1``/``"-1"`` PUT ``{"name": "-1"}``, which is Jira's *Automatic* (assign to the
        project default) — a DIFFERENT operation, hence ``None`` and not ``-1``.

        Cloud already routes an empty/``None`` assignee away from the vendor call inside
        ITS OWN transport (``adapters/jira/acli.py:342-345,357-359``, bug 85a1); as with the
        status→transition seam of bug d067, the translation lives PER TRANSPORT and Data
        Center simply never got its half. Blank-but-not-empty strings are normalised too:
        they carry the same arbitrary-match hazard through ``search_users(user=" ")``.
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
        """Resolve ``assignee`` to an assignable DC **username**, or raise
        :class:`AssigneeNotFoundError`.

        RETURN CONTRACT, which differs from Cloud's on purpose: the caller does
        ``acct = client.validate_assignee_exists(...)`` and then flows the return
        value ON as the resolved assignee identity. Cloud returns an ``accountId``;
        DC has none, so DC returns the ``name`` — consistent with ``NameIdentity``
        and with the ``external_id`` DC identities are minted under. Returning an
        accountId-shaped value, or ``True``, would corrupt the identity.

        ERROR CONTRACT: a definitive miss raises ``AssigneeNotFoundError``
        *specifically*, because ``outbound_assignee`` branches on
        ``type(exc).__name__ == "AssigneeNotFoundError"``. Any other type — and in
        particular a ``NotImplementedError`` — would downgrade EVERY DC assignee
        resolution to the non-authoritative string-match fallback, permanently and
        silently. That is why this member is implemented rather than declined.

        Matching is EXACT (username, then email, then display name) — DC's user
        search is substring/relevance-based, so returning its first hit would
        mis-assign a local handle that is not a DC user at all.
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
    """Issue-property and entity-property writes, both PUT-verbatim."""

    def _put_property(self, member: str, remote_id: str, property_key: str, value: Any) -> None:
        """PUT ``value`` to ``issue/{remote_id}/properties/{property_key}`` via the
        library's ``add_issue_property``.

        THE value-shape contract: the value is passed **verbatim**. Jira's
        issue-properties API stores whatever JSON is PUT as the property's value,
        and an earlier Cloud implementation wrapped it as ``{"value": …}`` — which
        stored the wrong shape and broke correlation WITHOUT ever raising (bug
        0b27). So "it did not error" is not evidence here; the key and the value
        must reach the endpoint intact.

        ``member`` is the PUBLIC method name so the WARNING names the member the
        core actually called, not this private helper.
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
