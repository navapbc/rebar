"""JiraBackend — thin delegation wrapper implementing the reconciler backend port (S2).

``JiraBackend`` wraps today's Jira modules with ZERO behaviour change — each role
Protocol delegates to the existing pure function:

* ``outbound`` → ``outbound_fields._map_local_to_jira_fields``
* ``inbound``  → ``inbound_fields._map_jira_to_local_fields``
* ``sanitizer`` → ``adapters/jira/jira_fields`` sanitizers
* ``identity`` → :class:`JiraIdentityConvention`
* ``transport`` → the injected ``acli.AcliClient`` (a ``TicketTransport``)

Jira supports links + comments, so ``JiraBackend`` also satisfies ``SupportsLinks``
and ``SupportsComments`` (delegating those to the transport). Core call sites now
drive this backend through the port via
:func:`~rebar_reconciler._backend_registry.select_backend`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rebar_reconciler import inbound_fields
from rebar_reconciler._backend import RemoteRef
from rebar_reconciler._backend_registry import register
from rebar_reconciler.adapters.jira import jira_fields, outbound_fields
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
from rebar_reconciler.adapters.jira_family.rich_text import cutover_clients


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
    codec = AdfCodec(rich="cloud" in cutover_clients())
    return codec.normalize_outbound(codec.fit_outbound(value))


class _JiraOutbound:
    """Delegates outbound mapping to ``outbound_fields._map_local_to_jira_fields``."""

    def __init__(self) -> None:
        # Constructed with Cloud's RichTextCodec (story J3) so exactly one
        # implementation of ``map_fields_to_remote`` exists in the tree, in
        # ``adapters/jira_family/outbound_mapper.py`` — the ADF-vs-wiki
        # difference is a constructor parameter, not a duplicated method.
        self._mapper = OutboundFieldMapper(AdfCodec(rich="cloud" in cutover_clients()))

    @property
    def comment_codec(self) -> Any:
        """Cloud's ``AdfCodec`` (emersed-specific-mutt) — the codec the comment-diff
        path normalizes its LOCAL dedup key through, the same instance this outbound
        mapper renders descriptions with, so the key matches the landed ADF wire."""
        return self._mapper.codec

    def map_local_to_remote(
        self,
        ticket: dict[str, Any],
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        emit_detach_clear: bool = False,
        *,
        suppressed_out: list[str] | None = None,
        status_map: dict[str, str] | None = None,
        type_map: dict[str, str] | None = None,
        priority_map: dict[str, str] | None = None,
        create_defaults: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return outbound_fields._map_local_to_jira_fields(
            ticket,
            binding_store,
            local_ticket_types,
            emit_detach_clear,
            suppressed_out=suppressed_out,
            status_map=status_map,
            type_map=type_map,
            priority_map=priority_map,
            create_defaults=create_defaults,
        )

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        status_map: dict[str, str] | None = None,
        priority_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Delegate to the shared ``jira_family.outbound_mapper.OutboundFieldMapper``
        (story J3), constructed with Cloud's ``AdfCodec``. See that module for the
        mapping rules (field-name reconciliation, value maps, rich-text fit)."""
        return self._mapper.map_fields_to_remote(
            changed, ticket, binding_store, local_ticket_types, status_map, priority_map
        )

    def resolve_assignee(
        self,
        local_value: str,
        remote_identity: dict[str, Any] | None,
        *,
        assignee_resolver: Callable[[str], tuple[Any, bool, bool]] | None = None,
    ) -> tuple[Any, bool, bool]:
        """Re-home the assignee resolver fast-path (ticket 625b; 264f semantics).

        Delegates to :class:`AccountIdIdentity` (story J4; ADR 0035 §(d)
        canonical-comparison corollary), Cloud's ``UserIdentityModel``, so the
        3-state state machine lives once in ``adapters/jira_family/identity_model``
        rather than being copied per backend (the mistake PR #120 made). Behaviour
        is verbatim-unchanged from before this delegation — see
        ``AccountIdIdentity.resolve``'s docstring for the full state table.

        ``assignee_resolver`` is the live account search, bound by the core diff to the
        current remote key and passed EXPLICITLY (ticket 65d7 — it used to arrive as an
        attribute this method rediscovered with ``getattr``). ``None`` keeps its existing
        meaning: no live search on this path, so ``AccountIdIdentity`` falls back to the
        permissive, non-authoritative string match."""
        return AccountIdIdentity(resolver=assignee_resolver).resolve(local_value, remote_identity)


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

    def fit_comment(self, body: str) -> str:
        """Pure fit-to-limit for comment-diff comparison (ticket 21ca; no warning).

        Must return exactly the marker-free text Cloud's send path LANDS, because
        ``outbound_comments._diff_comments`` builds its dedup key from this and
        compares it against the (marker-stripped) body read back from Jira. That is
        the convergence requirement ``comment_limits`` states: if the comparison and
        the send apply different fits, an over-length comment can never match and is
        re-posted on every pass.

        So this composes what ``acli_cli_ops.add_comment`` composes —
        ``fit_preserving_marker`` over the DECORATED body, measured by
        ``AdfCodec.fit_outbound`` (which sizes the SERIALIZED ADF, not the raw text,
        and additionally reserves budget for ``RECONCILER_MARKER``) — and strips the
        decoration back off. A plain character cap could not agree with it. The codec
        is built with the same flag-governed constructor :func:`_fit_description`
        uses, so ``reconciler.rich_text_cutover`` governs both alike.

        Deliberately NOT hoisted into the shared differ: Data Center keeps its comment
        ceiling distinct from its description fitter (bug 049e), so the convergence is
        restored per-backend here rather than by coupling the core to a codec.
        """
        from rebar_reconciler.outbound_comments import fit_comment_as_sent

        return fit_comment_as_sent(body, AdfCodec(rich="cloud" in cutover_clients()).fit_outbound)


class JiraBackend:
    """The Jira backend: five role Protocols + links/comments
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

    def __init__(self, transport: Any, instance: str = "", *, scope: Any | None = None) -> None:
        self.transport = transport
        #: The deployment label for :meth:`remote_ref`, supplied by ``build_backend``
        #: or derived from the captured scope's base URL (RP-04 S2) when not given.
        self.instance = instance or (instance_from_base_url(scope.url) if scope is not None else "")
        #: The CAPTURED reconciler settings (RP-04 S2). When present, the project
        #: accessors and ``assert_env_ready`` answer from this frozen scope instead of
        #: re-resolving ambient env/config on each access. ``None`` keeps the legacy
        #: ambient-resolution behaviour (the read-only rollback facade).
        self._scope = scope
        self.outbound = _JiraOutbound()
        self.inbound = _JiraInbound()
        self.sanitizer = _JiraSanitizer()
        self.identity = JiraIdentityConvention()

    def map_local_to_remote(
        self,
        ticket: dict[str, Any],
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        emit_detach_clear: bool = False,
        *,
        suppressed_out: list[str] | None = None,
        status_map: dict[str, str] | None = None,
        type_map: dict[str, str] | None = None,
        priority_map: dict[str, str] | None = None,
        create_defaults: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Convenience delegator to the ``outbound`` role's CREATE mapper, so a caller
        holding the backend can map a local ticket without reaching into ``.outbound``
        (the role object remains the canonical ``OutboundMapper`` — this only forwards)."""
        return self.outbound.map_local_to_remote(
            ticket,
            binding_store,
            local_ticket_types,
            emit_detach_clear,
            suppressed_out=suppressed_out,
            status_map=status_map,
            type_map=type_map,
            priority_map=priority_map,
            create_defaults=create_defaults,
        )

    # --- project accessors (ticket 97f2/bbf1) ---
    @property
    def project(self) -> str:
        """Effective write/create project WITHOUT any implicit create-time default
        (AC2) — empty when unset so the readiness guard fails closed. Resolved from
        settings, never the transport, so a JiraBackend built with a fake transport
        still answers. When the backend was composed with a captured scope (RP-04 S2)
        that scope is authoritative and no ambient re-resolution happens."""
        if self._scope is not None:
            return self._scope.project
        from rebar_reconciler.adapters.jira import acli_subprocess

        return acli_subprocess.resolve_jira_settings().project

    @property
    def query_project(self) -> str:
        """Configured read/query project WITHOUT the create-time default — empty
        when unset so the fetcher fails closed (bug 626d). Answered from the captured
        scope (RP-04 S2) when present, else re-resolved from ambient config."""
        if self._scope is not None:
            return self._scope.query_project
        from rebar_reconciler.adapters.jira import acli_subprocess

        return acli_subprocess.resolve_jira_settings().project

    def assert_env_ready(self) -> None:
        """Fail-fast when a connection essential (JIRA_URL / JIRA_USER /
        JIRA_API_TOKEN) is missing, BEFORE the transport is used for bootstrap-band
        execution. Preserves the pre-97f2 bootstrap env-check contract (the
        historical env-driven client builder, since deleted): a clear error naming
        the missing var(s) rather than a cryptic downstream failure — raises the
        neutral :class:`BackendEnvError` (subclasses ``RuntimeError``). When composed
        with a captured scope (RP-04 S2), the readiness check runs against that scope
        rather than re-resolving ambient state — and is DELIBERATELY stricter than
        this legacy ambient branch (ticket 4698-d85c): ``assert_cloud_scope_ready``
        additionally requires ``JIRA_USER`` to be an email and ``jira.project`` to be
        non-empty. That is an intended, canonical tightening (see its docstring), not
        accidental path drift; the ambient branch below is the compatibility floor."""
        if self._scope is not None:
            from rebar_reconciler.runtime import assert_cloud_scope_ready

            assert_cloud_scope_ready(self._scope)
            return
        from rebar_reconciler._backend import BackendEnvError
        from rebar_reconciler.adapters.jira import acli_subprocess

        settings = acli_subprocess.resolve_jira_settings()
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

    # --- typed Cloud summary operation (REB-3115 S1 T2) ---
    def execute_summary_operation(self, remote_id: str, new_summary: str) -> Any:
        """Execute ONE Cloud summary write as a typed ``OperationOutcome`` (REB-3115
        S1 T2): EXACTLY ONE ``acli jira workitem edit`` process, no adapter-level
        sleep or replay. The shared retry budget owns replay; this only executes and
        classifies. Imported function-locally to keep the summary seam off the
        backend's import path until a call site drives it (core dispatch cutover is
        out of scope for this task)."""
        from rebar_reconciler.adapters.jira import summary_operation

        return summary_operation.execute_cloud_summary_write(self.transport, remote_id, new_summary)

    def observe_summary_operation(
        self, remote_id: str, expected_summary: str, *, budget_remaining: bool
    ) -> Any:
        """Observe the primary store for a prior Cloud summary write with EXACTLY ONE
        one-attempt/no-sleep REST GET, returning a typed ``OperationOutcome`` mapped
        through the shared ``decide_replay`` table (REB-3115 S1 T2)."""
        from rebar_reconciler.adapters.jira import summary_operation

        return summary_operation.observe_summary_via_rest(
            self.transport, remote_id, expected_summary, budget_remaining=budget_remaining
        )


@register("jira")
def _build_jira_backend(config: Any) -> JiraBackend:
    """Construct a JiraBackend whose transport is an AcliClient from the resolved
    Jira settings — mirroring the pre-story direct construction."""
    from rebar_reconciler._backend import BackendEnvError, assert_transport_conforms
    from rebar_reconciler.adapters.jira import acli, acli_subprocess

    s = acli_subprocess.resolve_jira_settings()
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
