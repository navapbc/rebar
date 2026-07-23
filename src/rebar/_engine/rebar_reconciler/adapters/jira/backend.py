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

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Map a CANONICAL changed-fields dict (local field names → local values) to
        the vendor-shaped mutation fields, at the emission boundary (ticket 625b).

        Field-name reconciliation (local ``title`` → Jira ``summary``) and value
        mapping (``status``/``priority`` → the Jira name; ``description`` fitted to
        Jira's ADF limit) reuse the existing local→Jira maps in ``outbound_fields``.
        ``assignee``/``parent``/``reporter`` values are already resolved by the core
        diff and pass through unchanged, as does the ``_assignee_is_account_id``
        dispatch sentinel."""
        out: dict[str, Any] = {}
        for name, value in changed.items():
            if name == "title":
                out["summary"] = value
            elif name == "description":
                out["description"] = (
                    outbound_fields._load_adf().fit_text_to_adf_limit(value)
                    if isinstance(value, str)
                    else value
                )
            elif name == "status":
                out["status"] = outbound_fields._LOCAL_TO_JIRA_STATUS.get(value, "To Do")
            elif name == "priority":
                out["priority"] = outbound_fields._LOCAL_TO_JIRA_PRIORITY.get(value, "Medium")
            else:
                # assignee / parent / reporter (already resolved) + the
                # _assignee_is_account_id sentinel pass through by their own name.
                out[name] = value
        return out

    def resolve_assignee(
        self, local_value: str, remote_identity: dict[str, Any] | None
    ) -> tuple[Any, bool, bool]:
        """Re-home the assignee resolver fast-path (ticket 625b; 264f semantics).

        Returns ``(value, authoritative, is_account_id)``:

        * empty/None ``local_value`` → ``("", False, False)`` (nothing to resolve —
          non-authoritative, no live account search);
        * no injected resolver (fixture path) → ``(local_value, False, False)`` so the
          caller keeps the legacy permissive string match;
        * authoritative + the resolved account matches the remote identity's
          ``account_id`` → ``(None, True, …)`` (the CONVERGED signal — caller emits
          nothing);
        * authoritative + unmappable (no account) → ``("", True, False)`` (desired
          unassigned);
        * authoritative accountId fast-path → ``(account_id, True, True)`` (caller
          emits the accountId and sets ``_assignee_is_account_id``);
        * authoritative resolvable-but-mismatched → ``(local_value, True, False)``.

        The live account-search resolver is threaded in by ``compute_outbound_mutations``
        as ``self._assignee_resolver`` (a ``local_value -> (account|None, authoritative,
        is_account_id)`` callable bound to the current remote key)."""
        if not local_value:
            return ("", False, False)
        resolver = getattr(self, "_assignee_resolver", None)
        if resolver is None:
            return (local_value, False, False)
        acct, authoritative, is_account_id = resolver(local_value)
        if not authoritative:
            return (local_value, False, is_account_id)
        remote_acct = (remote_identity or {}).get("account_id")
        if (acct or None) == (remote_acct or None):
            return (None, True, is_account_id)  # converged
        if acct is None:
            return ("", True, False)  # unmappable → desired unassigned
        if is_account_id:
            return (acct, True, True)  # accountId fast-path
        return (local_value, True, False)  # resolvable but mismatched → emit local string


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
