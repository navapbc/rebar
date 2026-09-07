"""Provide Jira Data Center parent reads and writes.

``_resolve_epic_link_field_id`` is shared by inbound maps and outbound updates
so both directions use the same deployment-specific field.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar_reconciler._backend import BackendPaginationStallError
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
        return cached

    def get_parent_map(self, project_key: str, jql: str | None = None) -> dict[str, str | None]:
        """Return ``{issue_key: parent_key | None}`` through REST v2 paging.

        ``fields.parent`` takes precedence for subtasks. Other issues fall back
        to the discovered Epic Link field. Ordinary transport failures log and
        return ``{}``. ``BackendPaginationStallError`` propagates because a
        partial map cannot safely represent parentless state.
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
        except BackendPaginationStallError:
            # A stalled pager means a truncated whole-project map the differ
            # would treat as authoritative. Loud beats fail-open here.
            raise
        except Exception as exc:  # noqa: BLE001 — degradation contract: a parent-map failure must not abort the inbound pass
            logger.warning(
                "jira-datacenter transport: get_parent_map degraded to {} for project %r: %r",
                project_key,
                exc,
            )
            return {}
        return out

    def set_parent(self, remote_id: str, parent_key: str | None) -> None:
        """Set or clear a subtask parent or an ordinary issue's Epic Link.

        Subtasks use ``fields.parent``. Other issues use the discovered
        deployment-specific Epic Link field. A missing field raises
        ``NotImplementedError``. Subtask writes receive a fresh read because Data
        Center can return HTTP 204 while ignoring the update. A key mismatch also
        raises ``NotImplementedError``, classifying the parent as unrepresentable
        rather than retryable.
        """
        issue = _call_logged("set_parent", remote_id, lambda: self._client.issue(remote_id))
        raw = _unwrap(issue)
        fields = raw.get("fields") if isinstance(raw, dict) else None
        issue_type = fields.get("issuetype") if isinstance(fields, dict) else None
        is_subtask = bool(issue_type.get("subtask")) if isinstance(issue_type, dict) else False
        if not is_subtask:
            # Non-subtasks use an ordinary update of the deployment-specific Epic Link
            # field. Neither ``fields.parent`` nor the Agile API represents this operation.
            epic_link_id = self._resolve_epic_link_field_id()
            if epic_link_id is None:
                # Failure to discover an Epic Link field makes the parent unrepresentable
                # and keeps ``dispatch_one`` classification non-retryable.
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
        # Verify the write through a fresh issue read because the pre-write object is stale.
        verified = _call_logged("set_parent", remote_id, lambda: self._client.issue(remote_id))
        verified_raw = _unwrap(verified)
        verified_fields = verified_raw.get("fields") if isinstance(verified_raw, dict) else None
        # An absent or null ``parent`` means no parent. Compare a populated parent
        # by its issue key.
        observed = verified_fields.get("parent") if isinstance(verified_fields, dict) else None
        observed_key = observed.get("key") if isinstance(observed, dict) else None
        wanted_key = parent_key or None
        if observed_key != wanted_key:
            raise NotImplementedError(
                f"set_parent could not move the parent of sub-task {remote_id!r} on Jira Data "
                f"Center: it asked for "
                f"{'no parent' if wanted_key is None else repr(wanted_key)} and Jira accepted "
                f"the fields.parent write, but a fresh read of the issue still reports "
                f"{'no parent' if observed_key is None else repr(observed_key)}. Data Center "
                "accepts this write and silently ignores it (HTTP 204, field unchanged — see "
                "docs/jira-dc-capability-map.md), so the mutation did not happen and retrying "
                "the same write cannot make it happen."
            )
