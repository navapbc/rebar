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
from rebar_reconciler._backend import RemoteRef
from rebar_reconciler._backend_registry import register
from rebar_reconciler.adapters.jira import comment_limits, jira_fields, outbound_fields
from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec
from rebar_reconciler.adapters.jira_family import (
    RELATION_TO_JIRA_LINK,
    JiraIdentityConvention,
    instance_from_base_url,
)
from rebar_reconciler.adapters.jira_family import sanitize_label as _shared_sanitize_label
from rebar_reconciler.adapters.jira_family import sanitize_summary as _shared_sanitize_summary
from rebar_reconciler.adapters.jira_family.identity_model import AccountIdIdentity
from rebar_reconciler.adapters.jira_family.outbound_mapper import OutboundFieldMapper


def _fit_description(value: str) -> str:
    """Fit to Jira's ADF length limit, then normalize soft wraps.

    Order is load-bearing: ``fit_outbound`` measures the ADF the send path
    actually serializes, and the body Jira stores is then read back through
    ``decode_inbound`` — i.e. normalized. Composing them in this order makes the
    result its own fixed point (both halves are idempotent and normalization only
    shrinks the ADF), so the send value and every description comparison converge.
    Reached through the ``RichTextCodec`` contract (story J3) rather than the
    pinned ``adf`` module directly, so a Data Center backend can supply its own
    codec without touching this call site.
    """
    codec = AdfCodec()
    return codec.normalize_outbound(codec.fit_outbound(value))


class _JiraOutbound:
    """Delegates outbound mapping to ``outbound_fields._map_local_to_jira_fields``."""

    def __init__(self) -> None:
        # Constructed with Cloud's RichTextCodec (story J3) so exactly one
        # implementation of ``map_fields_to_remote`` exists in the tree, in
        # ``adapters/jira_family/outbound_mapper.py`` — the ADF-vs-wiki
        # difference is a constructor parameter, not a duplicated method.
        self._mapper = OutboundFieldMapper(AdfCodec())

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
        """Delegate to the shared ``jira_family.outbound_mapper.OutboundFieldMapper``
        (story J3), constructed with Cloud's ``AdfCodec``. See that module for the
        mapping rules (field-name reconciliation, value maps, rich-text fit)."""
        return self._mapper.map_fields_to_remote(changed, ticket, binding_store, local_ticket_types)

    def resolve_assignee(
        self, local_value: str, remote_identity: dict[str, Any] | None
    ) -> tuple[Any, bool, bool]:
        """Re-home the assignee resolver fast-path (ticket 625b; 264f semantics).

        Delegates to :class:`AccountIdIdentity` (story J4; ADR 0035 §(d)
        canonical-comparison corollary), Cloud's ``UserIdentityModel``, so the
        3-state state machine lives once in ``adapters/jira_family/identity_model``
        rather than being copied per backend (the mistake PR #120 made). Behaviour
        is verbatim-unchanged from before this delegation — see
        ``AccountIdIdentity.resolve``'s docstring for the full state table.

        The live account-search resolver is threaded in by ``compute_outbound_mutations``
        as ``self._assignee_resolver`` (a ``local_value -> (account|None, authoritative,
        is_account_id)`` callable bound to the current remote key). Reading it via
        ``getattr`` here is the Cloud-side boundary adapter to that PRE-EXISTING
        core attribute-injection (``outbound_field_diff.py``); removing the
        attribute-injection in favour of an explicit parameter is out of scope for
        this story (ticket 65d7)."""
        resolver = getattr(self, "_assignee_resolver", None)
        return AccountIdIdentity(resolver=resolver).resolve(local_value, remote_identity)


class _JiraInbound:
    """Delegates inbound mapping to ``inbound_fields._map_jira_to_local_fields``."""

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        return inbound_fields._map_jira_to_local_fields(remote_fields)

    def normalize_rich_text(self, body: Any) -> str:
        """Decode a rich-text payload to plain text (ticket 21ca; port member)."""
        return inbound_fields.normalize_rich_text(body)


class _JiraSanitizer:
    """Delegates each sanitizer to the corresponding ``jira_fields._sanitize_*``."""

    def sanitize_label(self, label: str) -> str:
        return _shared_sanitize_label(label)

    def sanitize_summary(self, summary: str) -> str:
        return _shared_sanitize_summary(summary)

    def sanitize_description(self, description: str) -> str:
        return jira_fields._sanitize_description(description)

    def sanitize_comment(self, body: str) -> str:
        return jira_fields._sanitize_comment(body)

    def fit_comment(self, body: str) -> str:
        """Pure fit-to-limit for comment-diff comparison (ticket 21ca; no warning)."""
        return comment_limits.truncate_comment_body(body)


class JiraBackend:
    """The Jira backend: five role Protocols + links/comments/absence-probe
    capabilities."""

    vendor = "jira"

    #: The store-facing FAMILY (bug 5f48). Identical to ``vendor`` for Cloud, which is
    #: why the distinction was invisible until Data Center arrived with a vendor string
    #: that is NOT a member of CREATION_CHANNELS. Declared explicitly so the core reads
    #: a family from the backend rather than deriving one from the vendor string.
    identity_family = "jira"

    def remote_ref(self, remote_id: str) -> RemoteRef:
        """This deployment's identity for ``remote_id``. Reads constructor state only."""
        return RemoteRef(vendor=self.vendor, instance=self.instance, remote_id=remote_id)

    def __init__(self, transport: Any, instance: str = "") -> None:
        self.transport = transport
        #: The deployment label for :meth:`remote_ref`, supplied by ``build_backend``.
        self.instance = instance
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

    def map_remote_links(self, remote_fields: dict[str, Any]) -> list[tuple[str | None, str, str]]:
        """Canonicalize a Jira issue's ``issuelinks`` into ``(relation, remote_key,
        opaque_vendor_type)`` entries (ticket eefd; absorbs the former
        ``outbound_links._existing_jira_links`` INCLUDING its direction-agnostic
        dedup — one entry per distinct ``(vendor_type, remote_key)`` regardless of
        which side of the link carries that key). Direction (inward vs outward) is
        resolved via the shared ``resolve_inbound_link`` so this stays consistent
        with the inbound ADD path and the outbound REMOVE path (bug 4b59). An
        unmapped vendor link type still yields an entry, with ``relation=None``, so
        core never removes a link it cannot canonicalize."""
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
                # resolve_inbound_link only returns (None, ...) for an unmapped type or a
                # malformed entry with neither side keyed; fall back to a direction-agnostic
                # key scan so an unmapped vendor type is still surfaced (relation=None).
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
        """``(Jira link type, swap_endpoints)`` for a canonical relation, or ``None``
        for a relation with no reliable Jira link type (ticket eefd)."""
        return RELATION_TO_JIRA_LINK.get(relation)

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
    from rebar_reconciler._backend import BackendEnvError, assert_transport_conforms
    from rebar_reconciler.adapters.jira import acli, acli_subprocess

    s = acli_subprocess.resolve_jira_settings(project_default="DIG")
    # Fail LOUDLY at construction on absent/invalid Cloud credentials, at parity with the
    # DC JIRA_PAT guard (bug ad85). Cloud authenticates with HTTP Basic auth: the Atlassian
    # account EMAIL (JIRA_USER) + an API token (JIRA_API_TOKEN) against JIRA_URL. Without
    # them the AcliClient would build with empty creds and every request would go out
    # effectively ANONYMOUS, failing only later at the first API call (a misleading
    # "project does not exist"/401). Presence + a minimal email-format check only — NO live
    # network probe (which would misattribute an outage as misconfiguration).
    missing = [
        name
        for name, value in (
            ("JIRA_URL", s.url),
            ("JIRA_USER", s.user),
            ("JIRA_API_TOKEN", s.api_token),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise BackendEnvError(
            f"missing Jira Cloud configuration: {', '.join(missing)}. The Jira Cloud "
            "backend authenticates with HTTP Basic auth using your Atlassian account email "
            "(JIRA_USER) and an API token (JIRA_API_TOKEN) against JIRA_URL. Set them "
            "before reconciling:\n"
            "    export JIRA_URL=https://<your-site>.atlassian.net\n"
            "    export JIRA_USER=<your-atlassian-account-email>\n"
            "    export JIRA_API_TOKEN=<your-api-token>\n"
            "Without them the reconciler would fall back to ANONYMOUS access, which "
            'typically surfaces as a misleading "project does not exist"/401 error (Jira '
            "hides projects you cannot browse) or, on a permissive instance, as a silently "
            "empty pass."
        )
    # Minimal email-format check: Cloud uses the account EMAIL as the Basic-auth username, so
    # a bare handle/accountId silently 401s. Require an "@" with a non-empty local and domain
    # part — deliberately NOT a strict RFC-5322 regex, which rejects valid addresses
    # (+tag subaddressing, subdomains): the guard only catches "not an email at all".
    local, sep, domain = s.user.strip().partition("@")
    if not sep or not local or not domain:
        raise BackendEnvError(
            "invalid JIRA_USER: Jira Cloud authenticates with your Atlassian account EMAIL "
            f"as the Basic-auth username, but JIRA_USER={s.user.strip()!r} is not an email "
            "address. Set JIRA_USER to the email of the account that owns the API token "
            "(e.g. export JIRA_USER=you@example.com)."
        )
    transport = acli.AcliClient(
        jira_url=s.url, user=s.user, api_token=s.api_token, jira_project=s.project
    )
    # The Cloud factory gets the same guard as the DC one: the port describes what
    # the CORE requires, so it binds every vendor. Guarding only the new backend
    # would leave the older one free to regress silently (story J9).
    assert_transport_conforms(transport, vendor="jira")
    return JiraBackend(transport=transport, instance=instance_from_base_url(s.url))
