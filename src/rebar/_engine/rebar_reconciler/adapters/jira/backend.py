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
    """The Jira backend: five role Protocols + links/comments/absence-probe
    capabilities."""

    vendor = "jira"

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.outbound = _JiraOutbound()
        self.inbound = _JiraInbound()
        self.sanitizer = _JiraSanitizer()
        self.identity = JiraIdentityConvention()

    # --- project accessors (ticket 97f2/bbf1) ---
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
        execution. Preserves the pre-97f2 ``build_acli_client_from_env`` contract:
        a clear error naming the missing var(s) rather than a cryptic downstream
        failure — raises the neutral :class:`BackendEnvError` (subclasses
        ``RuntimeError``)."""
        from rebar_reconciler._backend import BackendEnvError
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
            raise BackendEnvError(
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

    # --- capability: SupportsComments (delegates to transport) ---
    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        return self.transport.add_comment(remote_id, body)

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_comment_map(project_key)

    # --- capability: SupportsAbsenceProbe (delegates to adapters/jira/probe.py) ---
    def probe_remote(self, remote_id: str) -> Any:
        from rebar_reconciler.adapters.jira import probe as jira_probe

        return jira_probe.probe(remote_id)


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
