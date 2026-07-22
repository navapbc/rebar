"""JiraBackend — thin delegation wrapper implementing the reconciler backend port (S2).

``JiraBackend`` wraps today's Jira modules with ZERO behaviour change — each role
Protocol delegates to the existing pure function:

* ``outbound`` → ``outbound_fields._map_local_to_jira_fields``
* ``inbound``  → ``inbound_fields._map_jira_to_local_fields``
* ``sanitizer`` → ``adapters/jira/jira_fields`` sanitizers
* ``identity`` → :class:`JiraIdentityConvention`
* ``transport`` → the injected ``acli.AcliClient`` (a ``TicketTransport``)

Jira supports links + comments, so ``JiraBackend`` also satisfies ``SupportsLinks``
and ``SupportsComments`` (delegating those to the transport). No core call site is
rewired here (that is S4); no logic is relocated (that is S4/S5).
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler import inbound_fields, outbound_fields
from rebar_reconciler.adapters.jira import jira_fields

from .identity import JiraIdentityConvention


class _JiraOutbound:
    """Delegates outbound mapping to ``outbound_fields._map_local_to_jira_fields``."""

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


class _JiraInbound:
    """Delegates inbound mapping to ``inbound_fields._map_jira_to_local_fields``."""

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        return inbound_fields._map_jira_to_local_fields(remote_fields)


class _JiraSanitizer:
    """Delegates each sanitizer to the corresponding ``jira_fields._sanitize_*``."""

    def sanitize_label(self, label: str) -> str:
        return jira_fields._sanitize_label(label)

    def sanitize_summary(self, summary: str) -> str:
        return jira_fields._sanitize_summary(summary)

    def sanitize_description(self, description: str) -> str:
        return jira_fields._sanitize_description(description)

    def sanitize_comment(self, body: str) -> str:
        return jira_fields._sanitize_comment(body)


class JiraBackend:
    """The Jira backend: five role Protocols + links/comments capabilities."""

    vendor = "jira"

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.outbound = _JiraOutbound()
        self.inbound = _JiraInbound()
        self.sanitizer = _JiraSanitizer()
        self.identity = JiraIdentityConvention()

    # --- capability: SupportsLinks (delegates to transport) ---
    def set_relationship(
        self, from_id: str, to_id: str, link_type: str = "Blocks"
    ) -> dict[str, Any]:
        return self.transport.set_relationship(from_id, to_id, link_type)

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_issuelinks_map(project_key)

    # --- capability: SupportsComments (delegates to transport) ---
    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        return self.transport.add_comment(remote_id, body)

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_comment_map(project_key)
