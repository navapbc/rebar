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

from rebar_reconciler import inbound_fields
from rebar_reconciler._backend_registry import register
from rebar_reconciler.adapters.jira import jira_fields, outbound_fields

from .identity import JiraIdentityConvention


class _JiraOutbound:
    """Delegates outbound mapping + field-diff to the ``outbound_fields`` cluster."""

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

    # --- field-diff surface (ticket 4af8): delegate to the pure outbound_fields
    #     helpers so the core differ diffs through the port, not a direct import. ---
    def diff_fields(
        self,
        ticket: dict[str, Any],
        remote_fields: dict[str, Any],
        binding_store: Any = None,
        local_ticket_types: dict[str, str] | None = None,
        assignee_resolver: Any = None,
        jira_key: str = "",
        prev_jira_fields: dict[str, Any] | None = None,
        conflict_sink: list[tuple[str, str]] | None = None,
        dropped_field_sink: list[tuple[str, str]] | None = None,
        local_id: str = "",
    ) -> dict[str, Any]:
        return outbound_fields._diff_fields(
            ticket,
            remote_fields,
            binding_store=binding_store,
            local_ticket_types=local_ticket_types,
            assignee_resolver=assignee_resolver,
            jira_key=jira_key,
            prev_jira_fields=prev_jira_fields,
            conflict_sink=conflict_sink,
            dropped_field_sink=dropped_field_sink,
            local_id=local_id,
        )

    def extract_field(self, remote_fields: dict[str, Any], field: str) -> Any:
        return outbound_fields._extract_jira_field(remote_fields, field)

    def assignee_matches(self, local_val: str, remote_raw: Any) -> bool:
        return outbound_fields._assignee_matches(local_val, remote_raw)

    def local_type_to_remote(self, ticket_type: str) -> str:
        return outbound_fields._LOCAL_TO_JIRA_TYPE.get(ticket_type, "Task")


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

    # --- project accessors (ticket 4af8) ---
    @property
    def project(self) -> str:
        """Effective write/create project, DIG-defaulted to match the create
        client (bug 4fa9). Resolved from settings, never the transport, so a
        JiraBackend built with a fake transport still answers."""
        from rebar_reconciler.adapters.jira import acli_subprocess

        return acli_subprocess.resolve_jira_settings(project_default="DIG").project

    @property
    def query_project(self) -> str:
        """Configured read/query project WITHOUT the create-time default — empty
        when unset so the fetcher fails closed (bug 626d)."""
        from rebar_reconciler.adapters.jira import acli_subprocess

        return acli_subprocess.resolve_jira_settings().project

    def assert_env_ready(self) -> None:
        """Fail-fast when a connection essential (JIRA_URL / JIRA_USER /
        JIRA_API_TOKEN) is missing, BEFORE the transport is used for bootstrap-band
        execution. Preserves the pre-4af8 ``build_acli_client_from_env`` contract:
        a clear RuntimeError naming the missing var(s) rather than a cryptic
        downstream failure."""
        from rebar_reconciler.adapters.jira import acli_subprocess

        settings = acli_subprocess.resolve_jira_settings(project_default="DIG")
        missing = [
            name
            for name, value in (
                ("JIRA_URL", settings.url),
                ("JIRA_USER", settings.user),
                ("JIRA_API_TOKEN", settings.api_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"missing JIRA_* configuration: {', '.join(missing)} "
                "(set via env or [tool.rebar.jira]; JIRA_API_TOKEN is env-only) "
                "(required to build the backend transport for bootstrap band execution)"
            )

    # --- capability: SupportsLinks (delegates to transport) ---
    def set_relationship(
        self, from_id: str, to_id: str, link_type: str = "Blocks"
    ) -> dict[str, Any]:
        return self.transport.set_relationship(from_id, to_id, link_type)

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_issuelinks_map(project_key)

    def link_type_for_relation(self, relation: str) -> tuple[str, bool] | None:
        """Map a rebar relation to its ``(jira_link_type, swap)`` pair (or ``None``
        for an unmapped relation) via the vendor vocabulary — the neutral accessor
        the core outbound link differ calls instead of importing the map."""
        return jira_fields._RELATION_TO_JIRA_LINK.get(relation)

    # --- capability: SupportsComments (delegates to transport) ---
    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        return self.transport.add_comment(remote_id, body)

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_comment_map(project_key)


@register("jira")
def _build_jira_backend(config: Any) -> JiraBackend:
    """Construct a JiraBackend whose transport is an AcliClient from the resolved
    Jira settings — mirroring the pre-story direct construction."""
    from rebar_reconciler.adapters.jira import acli, acli_subprocess

    s = acli_subprocess.resolve_jira_settings(project_default="DIG")
    transport = acli.AcliClient(
        jira_url=s.url, user=s.user, api_token=s.api_token, jira_project=s.project
    )
    return JiraBackend(transport=transport)
