"""JiraDataCenterBackend — the reconciler backend port for Jira Server / Data Center.

The Data Center sibling of ``adapters/jira/backend.JiraBackend``. It reuses the
vendor-neutral field maps, sanitizers, identity convention, and link vocabulary that
the Cloud adapter already factored out, and swaps in three Data Center seams:

* ``transport`` → :class:`JiraDataCenterClient` (REST v2 + PAT bearer auth) instead of
  the ACLI subprocess client;
* rich text → plain text / wiki markup (``rich_text``) instead of Cloud's ADF, so the
  description sanitizer fits characters rather than an ADF document;
* user identity → name-based, so ``resolve_assignee`` never emits an ``accountId``.

Data Center supports links + comments + absence probing, so the backend also satisfies
``SupportsLinks``, ``SupportsComments`` and ``SupportsAbsenceProbe``.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler import inbound_fields
from rebar_reconciler._backend_registry import register
from rebar_reconciler.adapters.jira import comment_limits, jira_fields, outbound_fields
from rebar_reconciler.adapters.jira.identity import JiraIdentityConvention

from . import rich_text
from .rest_client import JiraDataCenterClient
from .settings import resolve_jira_dc_settings


class _JiraDcOutbound:
    """Outbound mapping: reuses the vendor-neutral maps, plain-text rich text."""

    def map_local_to_remote(
        self,
        ticket: dict[str, Any],
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        emit_detach_clear: bool = False,
    ) -> dict[str, Any]:
        return outbound_fields._map_local_to_jira_fields(
            ticket, binding_store, local_ticket_types, emit_detach_clear
        )

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Map a canonical changed-fields dict to Data Center mutation fields.

        Field-name reconciliation and status/priority value mapping reuse the
        shared ``outbound_fields`` maps; the description is fitted as plain text
        (``rich_text.fit_text_to_limit``) rather than to Cloud's ADF limit.
        """
        out: dict[str, Any] = {}
        for name, value in changed.items():
            if name == "title":
                out["summary"] = value
            elif name == "description":
                out["description"] = (
                    rich_text.fit_text_to_limit(value) if isinstance(value, str) else value
                )
            elif name == "status":
                out["status"] = outbound_fields._LOCAL_TO_JIRA_STATUS.get(value, "To Do")
            elif name == "priority":
                out["priority"] = outbound_fields._LOCAL_TO_JIRA_PRIORITY.get(value, "Medium")
            else:
                out[name] = value
        return out

    def resolve_assignee(
        self, local_value: str, remote_identity: dict[str, Any] | None
    ) -> tuple[Any, bool, bool]:
        """Resolve an assignee by NAME (Data Center has no accountId).

        The returned tuple keeps the ``(value, authoritative, is_account_id)``
        shape the core diff consumes, but ``is_account_id`` is always ``False``:
        Data Center identifies users by ``name``, so the outbound value is a bare
        username. Without an injected resolver (the normal Data Center path) the
        legacy permissive string match is preserved.
        """
        if not local_value:
            return ("", False, False)
        resolver = getattr(self, "_assignee_resolver", None)
        if resolver is None:
            return (local_value, False, False)
        name, authoritative, _ = resolver(local_value)
        if not authoritative:
            return (local_value, False, False)
        remote_name = (remote_identity or {}).get("name")
        if (name or None) == (remote_name or None):
            return (None, True, False)
        if name is None:
            return ("", True, False)
        return (name, True, False)


class _JiraDcInbound:
    """Inbound mapping: reuses the vendor-neutral Jira→local map + passthrough decode."""

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        return inbound_fields._map_jira_to_local_fields(remote_fields)

    def normalize_rich_text(self, body: Any) -> str:
        """Data Center v2 bodies are plain text / wiki markup — decode identity."""
        return rich_text.plain_text_of(body)


class _JiraDcSanitizer:
    """Sanitizers: shared label/summary/comment fits; plain-text description fit."""

    def sanitize_label(self, label: str) -> str:
        return jira_fields._sanitize_label(label)

    def sanitize_summary(self, summary: str) -> str:
        return jira_fields._sanitize_summary(summary)

    def sanitize_description(self, description: str) -> str:
        if not isinstance(description, str):
            raise ValueError(
                f"Description must be str, got {type(description).__name__}: {description!r}"
            )
        return rich_text.fit_text_to_limit(description)

    def sanitize_comment(self, body: str) -> str:
        return jira_fields._sanitize_comment(body)

    def fit_comment(self, body: str) -> str:
        return comment_limits.truncate_comment_body(body)


class JiraDataCenterBackend:
    """The Jira Data Center backend: five role Protocols + links/comments/probe."""

    vendor = "jira-datacenter"

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.outbound = _JiraDcOutbound()
        self.inbound = _JiraDcInbound()
        self.sanitizer = _JiraDcSanitizer()
        self.identity = JiraIdentityConvention()

    # --- project accessors ---
    @property
    def project(self) -> str:
        return resolve_jira_dc_settings().project

    @property
    def query_project(self) -> str:
        return resolve_jira_dc_settings().project

    def assert_env_ready(self) -> None:
        """Fail fast when JIRA_URL or JIRA_PAT is missing (no user for a PAT)."""
        from rebar_reconciler._backend import BackendEnvError

        settings = resolve_jira_dc_settings()
        missing = [
            name
            for name, value in (("JIRA_URL", settings.url), ("JIRA_PAT", settings.pat))
            if not value
        ]
        if missing:
            raise BackendEnvError(
                f"missing Jira Data Center configuration: {', '.join(missing)} "
                "(set JIRA_URL via env or [tool.rebar.jira]; JIRA_PAT is env-only) "
                "(required to build the backend transport for bootstrap band execution)"
            )

    # --- capability: SupportsLinks ---
    def set_relationship(
        self, from_id: str, to_id: str, link_type: str = "Blocks"
    ) -> dict[str, Any]:
        return self.transport.set_relationship(from_id, to_id, link_type)

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_issuelinks_map(project_key)

    def map_remote_links(self, remote_fields: dict[str, Any]) -> list[tuple[str | None, str, str]]:
        """Canonicalize a Data Center issue's ``issuelinks`` into ``(relation,
        remote_key, opaque_vendor_type)`` entries. The v2 link payload carries the
        same ``type.name`` + ``inwardIssue``/``outwardIssue`` shape as Cloud, so the
        shared ``resolve_inbound_link`` direction resolver applies unchanged."""
        from rebar_reconciler.link_direction import resolve_inbound_link

        seen: set[tuple[str, str]] = set()
        out: list[tuple[str | None, str, str]] = []
        for link in remote_fields.get("issuelinks") or []:
            if not isinstance(link, dict):
                continue
            link_type = link.get("type") or {}
            type_name = link_type.get("name") if isinstance(link_type, dict) else None
            if not type_name:
                continue
            other_key, relation = resolve_inbound_link(link)
            if other_key is None:
                for side_key in ("inwardIssue", "outwardIssue"):
                    side = link.get(side_key)
                    if isinstance(side, dict) and side.get("key"):
                        other_key = side["key"]
                        break
            if not other_key:
                continue
            dedup_key = (type_name, other_key)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            out.append((relation, other_key, type_name))
        return out

    def link_payload_for_relation(self, relation: str) -> tuple[str, bool] | None:
        return jira_fields._RELATION_TO_JIRA_LINK.get(relation)

    # --- capability: SupportsComments ---
    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        return self.transport.add_comment(remote_id, body)

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_comment_map(project_key)

    # --- capability: SupportsAbsenceProbe ---
    def probe_remote(self, remote_id: str) -> Any:
        """Probe via a v2 GET and classify with the shared pure classifier."""
        from rebar_reconciler.adapters.jira.probe import classify_probe_response

        status_code, payload = self.transport.probe_issue(remote_id)
        return classify_probe_response(remote_id, status_code, payload)


@register("jira-datacenter")
def _build_jira_datacenter_backend(config: Any) -> JiraDataCenterBackend:
    """Build a JiraDataCenterBackend whose transport is a JiraDataCenterClient from
    the resolved Data Center settings (JIRA_URL / JIRA_PROJECT + env-only JIRA_PAT)."""
    s = resolve_jira_dc_settings()
    transport = JiraDataCenterClient(base_url=s.url, pat=s.pat, project=s.project)
    return JiraDataCenterBackend(transport=transport)
