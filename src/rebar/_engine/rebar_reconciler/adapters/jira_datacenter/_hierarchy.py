"""Parent/hierarchy mixin for the Jira Data Center transport (ticket 465d,
epic e369) — sub-task ``parent`` writes, the Epic Link custom-field lookup,
and the bulk parent-map reader. Where 39c1 / 9bb9 / future parent work lands.

RELOCATED VERBATIM out of ``transport.py``; this module changes no behaviour.
``_resolve_epic_link_field_id`` (ticket 9bb9) is SHARED by ``set_parent``
(outbound write) and ``get_parent_map`` (inbound read) — one discovery,
never two definitions that could disagree about which field they mean.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar_reconciler.adapters.jira_datacenter._base import (
    _MISSING,
    _call_logged,
    _TransportBase,
    _unwrap,
)

logger = logging.getLogger(__name__)


class _HierarchyMixin(_TransportBase):
    """``set_parent`` (sub-task ``parent`` + Epic Link custom field) and the
    bulk ``get_parent_map`` reader."""

    def _resolve_epic_link_field_id(self) -> str | None:
        """Discover + cache the "Epic Link" custom field id BY NAME (id differs per
        deployment, never hardcoded). ``getattr``/``setattr`` on ``_epic_link_field_id``
        (default ``_MISSING``) rather than a bare attribute read: a transport built via
        ``__new__`` (some pagination tests) skips ``__init__``'s assignment entirely,
        so a bare read would raise. SHARED by ``set_parent`` (outbound, 39c1) and
        ``get_parent_map`` (inbound, 9bb9) — one discovery, never two that could disagree.
        """
        cached = getattr(self, "_epic_link_field_id", _MISSING)
        if cached is _MISSING:
            lister = getattr(self._client, "fields", None)
            cached = (
                None
                if lister is None
                else next((f.get("id") for f in lister() if f.get("name") == "Epic Link"), None)
            )
            self._epic_link_field_id = cached
        return cached  # type: ignore[return-value]

    def get_parent_map(self, project_key: str, jql: str | None = None) -> dict[str, str | None]:
        """``{issue_key → parent_key | None}`` for a project, via DC REST **v2**
        OFFSET pagination.

        Deliberately NOT a port of Cloud's ``get_parent_map``: that one POSTs to
        ``/rest/api/3/search/jql`` and pages with an opaque ``nextPageToken``
        cursor, and its own docstring records (live-proven, ticket 8b25) that the
        legacy endpoint is retired with HTTP 410 and that sending ``startAt`` is
        rejected with HTTP 400. Both the endpoint and the pagination model are
        Cloud-v3 only. DC serves ``/rest/api/2/search`` with ``startAt`` /
        ``maxResults``, which is exactly what ``jira.JIRA.search_issues`` drives —
        so the library's native offset paging IS the DC-correct mechanism.

        Also reads a non-sub-task's parent back from the "Epic Link" field
        :meth:`_resolve_epic_link_field_id` discovers, closing 9bb9's round trip
        with ``set_parent``; ``fields.parent`` wins where both are present.

        Degradation contract (mirrors Cloud, and what ``fetcher`` expects): a
        failure logs a WARNING and returns ``{}``, so the inbound pass falls back
        to its parentless path rather than aborting.
        """
        query = jql or f"project = {project_key}"
        out: dict[str, str | None] = {}
        try:
            # Discovery is called INSIDE this try (it never swallows its own ``fields()``
            # failure — ``set_parent`` relies on that propagating) so it still hits the
            # degradation contract below. Pager choice per 9263: was correct here first.
            epic_field = self._resolve_epic_link_field_id()
            search_fields = "parent" if epic_field is None else f"parent,{epic_field}"
            for issue in self._paged_search(query, fields=search_fields, rate_limit_retry=True):
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                if not key:
                    continue
                fields = issue.get("fields")
                parent = fields.get("parent") if isinstance(fields, dict) else None
                parent_key = parent.get("key") if isinstance(parent, dict) else None
                if not parent_key and epic_field and isinstance(fields, dict):
                    epic_value = fields.get(epic_field)
                    parent_key = epic_value if isinstance(epic_value, str) and epic_value else None
                out[key] = parent_key
        except Exception as exc:  # noqa: BLE001 — degradation contract: a parent-map failure must not abort the inbound pass
            logger.warning(
                "jira-datacenter transport: get_parent_map degraded to {} for project %r: %r",
                project_key,
                exc,
            )
            return {}
        return out

    def set_parent(self, remote_id: str, parent_key: str | None) -> None:
        """Set or clear a SUB-TASK's parent via ``fields.parent``.

        DC splits what Cloud unifies. A sub-task's parent genuinely lives in
        ``fields.parent`` and this method writes it (``{"parent": {"key": …}}``, or
        ``{"parent": None}`` to clear — the same call path for a SET and a CLEAR,
        which is what ``dispatch_one`` relies on). But EPIC membership on DC is not
        ``parent`` at all: it is the "Epic Link" **custom field**
        (``customfield_NNNNN``, whose id is instance-specific).

        **Ticket 39c1 lifted the former decline and replaced its route.** The
        original lift wrote the Epic Link through the Agile API
        (``add_issues_to_epic``); a live run against DC 8.17.1 answered that call
        with HTTP 404 "null for uri" — ``POST /rest/greenhopper/1.0/epic/{key}/issue``
        does not exist on this instance. That route is REFUTED. The Epic Link is
        instead written as an ORDINARY field update, same mechanism as the
        sub-task branch below: discover the field's id by matching ``name ==
        "Epic Link"`` in ``self._client.fields()`` (``GET /rest/api/2/field``,
        never hardcoded — the id differs per deployment), then
        ``issue.update(fields={field_id: parent_key})``.

        The decline survives only where the parent is genuinely unrepresentable —
        no "Epic Link" field discoverable on this instance — because writing
        ``fields.parent`` for a non-sub-task would be silently no-op'd by DC, which is
        the failure mode this method exists to refuse.
        """
        issue = _call_logged("set_parent", remote_id, lambda: self._client.issue(remote_id))
        raw = _unwrap(issue)
        fields = raw.get("fields") if isinstance(raw, dict) else None
        issue_type = fields.get("issuetype") if isinstance(fields, dict) else None
        is_subtask = bool(issue_type.get("subtask")) if isinstance(issue_type, dict) else False
        if not is_subtask:
            # Ticket 39c1: a NON-sub-task's parent is the EPIC LINK custom field, written as a
            # plain field update — never ``fields.parent``, which DC silently no-ops, and never
            # ``add_issues_to_epic`` (REFUTED: DC 8.17.1 404s on the greenhopper epic-issue path).
            # Discovery is SHARED with ``get_parent_map`` (9bb9) via ``_resolve_epic_link_field_id``
            epic_link_id = self._resolve_epic_link_field_id()
            if epic_link_id is None:
                # Whether the client can't enumerate fields at all, or it can and this
                # instance simply has none named "Epic Link", the parent is equally
                # unrepresentable — both must raise NotImplementedError (not AttributeError)
                # so `dispatch_one` classifies it as `outbound-parent-unrepresentable`
                # (change 1305), not the retryable `outbound-parent-failed`.
                raise NotImplementedError(
                    f"set_parent cannot represent the parent of {remote_id!r} on Jira Data "
                    "Center: the issue is not a sub-task, so its parent is the 'Epic Link' "
                    "custom field, but this instance's field inventory has no field named "
                    "'Epic Link'. Declining rather than writing fields.parent, which DC would "
                    "silently no-op."
                )
            body: dict[str, Any] = {epic_link_id: parent_key}
            _call_logged("set_parent", remote_id, lambda: issue.update(fields=body))
            return
        body = {"parent": {"key": parent_key}} if parent_key else {"parent": None}
        _call_logged("set_parent", remote_id, lambda: issue.update(fields=body))
