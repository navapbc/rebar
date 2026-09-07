"""Register the ``jira-datacenter`` backend.

The implementation combines shared Jira-family value maps and sanitizers with
Data Center wiki text and username identity. Importing the module registers the
backend. Data Center uses ``WikiTextCodec`` rather than ADF and ``NameIdentity``
rather than Cloud ``accountId``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rebar_reconciler._backend import RemoteRef
from rebar_reconciler._backend_registry import register
from rebar_reconciler.adapters.jira_family import (
    RELATION_TO_JIRA_LINK,
    JiraIdentityConvention,
    instance_from_base_url,
)
from rebar_reconciler.adapters.jira_family import sanitize_comment as _shared_sanitize_comment
from rebar_reconciler.adapters.jira_family import (
    sanitize_description as _shared_sanitize_description,
)
from rebar_reconciler.adapters.jira_family import sanitize_label as _shared_sanitize_label
from rebar_reconciler.adapters.jira_family import sanitize_summary as _shared_sanitize_summary
from rebar_reconciler.adapters.jira_family.identity_model import NameIdentity
from rebar_reconciler.adapters.jira_family.outbound_mapper import (
    OutboundFieldMapper,
    merge_create_defaults,
    resolve_outbound_priority,
    resolve_outbound_status,
    resolve_outbound_type,
)
from rebar_reconciler.adapters.jira_family.rich_text import (
    _WIKI_TRUNCATION_SUFFIX,
    WikiTextCodec,
    cutover_clients,
)

# Preserve the shared integer priority map as a module attribute for parity tests.
# ``resolve_outbound_priority`` applies project maps or the same default.
from rebar_reconciler.adapters.jira_family.value_maps import (  # noqa: F401
    LOCAL_PRIORITY_TO_JIRA,
)

# Preserve the shared ticket-type map as a module attribute for parity tests.
# ``resolve_outbound_type`` applies project maps or this default.
from rebar_reconciler.adapters.jira_family.value_maps import (  # noqa: F401
    LOCAL_TYPE_TO_JIRA as _LOCAL_TO_JIRA_TYPE,
)


def _map_local_to_dc_fields(
    ticket: dict[str, Any],
    status_map: dict[str, str] | None = None,
    type_map: dict[str, str] | None = None,
    priority_map: dict[str, str] | None = None,
    create_defaults: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map a local ticket into Data Center create fields.

    Use shared map-or-drift status and priority resolvers and the shared
    map-or-default type resolver. Omit status or priority when no target exists.
    Render descriptions with ``WikiTextCodec``. Merge string create defaults
    beneath computed fields.
    """
    codec = WikiTextCodec(rich="dc" in cutover_clients())
    fields: dict[str, Any] = {
        "summary": ticket.get("title") or "",
        # Render and fit descriptions before create, matching create and update paths.
        "description": codec.to_wire(codec.fit_outbound(ticket.get("description") or "")),
        "issuetype": resolve_outbound_type(ticket.get("ticket_type", "task"), type_map),
        "assignee": ticket.get("assignee") or "",
    }
    priority_target = resolve_outbound_priority(ticket.get("priority", 2), priority_map)
    if priority_target is not None:
        fields["priority"] = priority_target
    target = resolve_outbound_status(ticket.get("status", "open"), status_map)
    if target is not None:
        fields["status"] = target
    return merge_create_defaults(create_defaults, fields)


class _DCOutbound:
    """Delegates changed-field mapping to the SHARED ``OutboundFieldMapper``,
    constructed with DC's ``WikiTextCodec`` (story J3) — the one place Cloud and
    DC diverge in this role."""

    def __init__(
        self,
        *,
        assignee_resolver: Callable[[str], tuple[Any, bool, bool]] | None = None,
    ) -> None:
        self._mapper = OutboundFieldMapper(WikiTextCodec(rich="dc" in cutover_clients()))
        # Capture the deployment account search through a declared constructor
        # parameter.
        self._assignee_search = assignee_resolver

    @property
    def comment_codec(self) -> Any:
        """DC's ``WikiTextCodec`` (emersed-specific-mutt) — the codec the comment-diff
        path normalizes its LOCAL dedup key through, the same instance this outbound
        mapper renders descriptions with, so the key matches the landed wiki wire."""
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
        # ``suppressed_out`` (ticket 8390) is accepted and IGNORED on purpose:
        # ``_map_local_to_dc_fields`` never maps a parent at all, so this backend has
        # no suppression to report and appending anything here would invent one.
        return _map_local_to_dc_fields(ticket, status_map, type_map, priority_map, create_defaults)

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        status_map: dict[str, str] | None = None,
        priority_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
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
        """Resolve assignees through ``NameIdentity``.

        The issue-scoped resolver supplied by the core takes precedence over the
        deployment-wide search captured at construction.
        """
        return NameIdentity(resolver=assignee_resolver or self._assignee_search).resolve(
            local_value, remote_identity
        )


class _DCInbound:
    """Delegates to the SAME ``inbound_fields`` mapper Cloud uses.

    ``_map_jira_to_local_fields`` and ``normalize_rich_text`` are already
    format-agnostic on ``description``/comment bodies: a ``dict`` decodes via
    Cloud's ADF walker, but a plain ``str`` (DC's REST v2 shape) passes through
    unchanged — so no DC-specific override is needed here, only DC's own
    construction of this role."""

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        from rebar_reconciler import inbound_fields

        return inbound_fields._map_jira_to_local_fields(remote_fields)

    def normalize_rich_text(self, body: Any) -> str:
        from rebar_reconciler import inbound_fields

        return inbound_fields.normalize_rich_text(body)


def _truncate_dc_comment_body(body: str, max_chars: int) -> str:
    """Fit a Data Center comment to an independent character ceiling.

    Nonpositive limits mean unlimited. Truncation uses the shared wiki suffix and
    is idempotent. Non-string values pass through unchanged. This function remains
    separate from description fitting so format changes cannot retarget comments.
    """
    if not isinstance(body, str) or max_chars <= 0 or len(body) <= max_chars:
        return body
    keep = max_chars - len(_WIKI_TRUNCATION_SUFFIX)
    if keep <= 0:
        # A ceiling smaller than the marker itself: a bare hard cut is the only
        # option that still respects the limit.
        return body[:max_chars]
    return body[:keep] + _WIKI_TRUNCATION_SUFFIX


class _DCSanitizer:
    """Delegates to the SHARED Jira-family sanitizers, binding DC's
    ``WikiTextCodec``/plain-text limit as the injected rich-text contract
    (``sanitize_description``/``sanitize_comment`` take theirs as a parameter
    precisely so Cloud and DC each bind their own — see
    ``jira_family/sanitizers.py``)."""

    def __init__(self, comment_max_chars: int | None = None) -> None:
        self._codec = WikiTextCodec(rich="dc" in cutover_clients())
        #: ``None`` = resolve from config on first use (see :meth:`comment_max_chars`).
        #: An explicit value is the injection seam tests and callers use to bind a
        #: known ceiling without touching the process config.
        self._comment_max_chars = comment_max_chars

    def sanitize_label(self, label: str) -> str:
        return _shared_sanitize_label(label)

    def sanitize_summary(self, summary: str) -> str:
        return _shared_sanitize_summary(summary)

    def sanitize_description(self, description: str) -> str:
        return _shared_sanitize_description(description, fit=self._codec.fit_outbound)

    def comment_max_chars(self) -> int:
        """Return the configured comment ceiling, resolving it lazily once.

        Data Center defaults ``jira.text.field.character.limit`` to 32767 and
        treats zero as unlimited. The value is configurable because reading the
        deployment setting requires Jira administrator permission. Constructor
        injection supports tests.
        """
        if self._comment_max_chars is None:
            from rebar_reconciler.adapters.jira_datacenter.settings import (
                resolve_comment_max_chars,
            )

            self._comment_max_chars = resolve_comment_max_chars()
        return self._comment_max_chars

    def _fit_raw(self, text: str) -> str:
        """Fit raw comment text through the comment-specific truncator.

        This path does not use the description codec, so description format
        changes cannot alter comment limits.
        """
        return _truncate_dc_comment_body(text, self.comment_max_chars())

    def sanitize_comment(self, body: str) -> str:
        # Fit decorated comments with ``fit_preserving_marker`` so truncation
        # retains ``RECONCILER_MARKER``. Markerless bodies use the raw fitter.
        from rebar_reconciler.outbound_comments import fit_preserving_marker

        return _shared_sanitize_comment(
            body,
            truncate=lambda text: fit_preserving_marker(text, self._fit_raw),
            max_chars=self.comment_max_chars(),
        )

    def fit_comment(self, body: str) -> str:
        """Reproduce the marker-free comment body used for differ deduplication.

        Apply the send path's decorate, marker-preserving fit, and strip
        composition. In-limit bodies remain byte-identical.
        """
        from rebar_reconciler.outbound_comments import fit_comment_as_sent

        return fit_comment_as_sent(body, self._fit_raw)


def _search_users_by_username(client: Any, username: str) -> tuple[str | None, bool, bool]:
    """Resolve a DC username via ``jira.JIRA.search_users`` — the live lookup this
    story supplies for :class:`NameIdentity` (ticket 0a94-e104-7304-4d85).

    Returns ``(resolved_name | None, authoritative, is_account_id)``. Always
    authoritative (``True``) — this IS the live search, distinct from the
    "no resolver injected" fixture path (``NameIdentity(resolver=None)``), which
    is the non-authoritative case. ``is_account_id`` is always ``False``: DC has
    no accountId concept at all.
    """
    users = client.search_users(user=username, maxResults=2)
    for user in users:
        name = getattr(user, "name", None)
        if name == username:
            return (name, True, False)
    return (None, True, False)


@register("jira-datacenter")
def _build_jira_datacenter_backend(config: Any) -> JiraDataCenterBackend:
    """Construct a ``JiraDataCenterBackend`` whose transport is a real
    ``jira.JIRA`` client built from the resolved DC settings."""
    from rebar_reconciler._backend import assert_transport_conforms
    from rebar_reconciler.adapters.jira_datacenter.settings import (
        resolve_jira_datacenter_settings,
    )
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        JiraDataCenterTransport,
        build_client_from_settings,
    )

    settings = resolve_jira_datacenter_settings()
    client = build_client_from_settings(settings)
    transport = JiraDataCenterTransport(client=client, project=settings.project)
    # Conformance is asserted HERE, before the backend can be handed to a pass:
    # a missing member must be a loud construction failure, not a crash partway
    # through a writing pass that has already mutated the remote (story J9).
    assert_transport_conforms(transport, vendor="jira-datacenter")
    return JiraDataCenterBackend(
        transport=transport, client=client, instance=instance_from_base_url(settings.url)
    )


class JiraDataCenterBackend:
    """The Data Center backend: five role Protocols + links/comments
    capabilities, built on ``JiraDataCenterTransport``."""

    vendor = "jira-datacenter"

    # Cloud and Data Center share the store identity family ``jira``.
    # ``RemoteRef.instance`` distinguishes deployments without partitioning identities.
    identity_family = "jira"

    def remote_ref(self, remote_id: str) -> RemoteRef:
        """This deployment's identity for ``remote_id``. Reads constructor state only."""
        return RemoteRef(vendor=self.vendor, instance=self.instance, remote_id=remote_id)

    def __init__(
        self,
        transport: Any,
        client: Any | None = None,
        instance: str = "",
        *,
        scope: Any | None = None,
    ) -> None:
        self.transport = transport
        # Capture the normalized deployment label for ``remote_ref`` at construction.
        self.instance = instance or (
            instance_from_base_url(scope.base_url) if scope is not None else ""
        )
        # Captured settings supply read scope and readiness checks without ambient
        # re-resolution.
        self._scope = scope
        # Retain the injected client for deployment account search. A missing test
        # client leaves identity resolution non-authoritative.
        self._client = client
        self.outbound = _DCOutbound(
            assignee_resolver=(
                (lambda name: _search_users_by_username(client, name))
                if client is not None
                else None
            )
        )
        self.inbound = _DCInbound()
        # Ticket 2048-d289: a captured scope binds the compose-captured comment
        # ceiling via the sanitizer's existing injection seam; scope-less
        # construction keeps the legacy lazy ambient resolve (bug 049e).
        self.sanitizer = (
            _DCSanitizer(comment_max_chars=scope.comment_max_chars)
            if scope is not None
            else _DCSanitizer()
        )
        self.identity = JiraIdentityConvention()

    @property
    def project(self) -> str:
        if self._scope is not None:
            return self._scope.project
        return self.transport.project

    @property
    def query_project(self) -> str:
        """Return the configured read project from captured scope or settings.

        An unset value remains empty so inbound fetches fail closed instead of
        querying every project. This differs from the transport's write scope.
        """
        if self._scope is not None:
            return self._scope.query_project
        from rebar_reconciler.adapters.jira_datacenter.settings import (
            resolve_jira_datacenter_settings,
        )

        return resolve_jira_datacenter_settings().project

    def assert_env_ready(self) -> None:
        """Raise ``BackendEnvError`` when URL or environment-only PAT is missing.

        Report every missing essential before client use. Captured scope supplies
        the URL when present.
        """
        if self._scope is not None:
            from rebar_reconciler.runtime import assert_datacenter_scope_ready

            assert_datacenter_scope_ready(self._scope)
            return
        from rebar_reconciler._backend import BackendEnvError
        from rebar_reconciler.adapters.jira_datacenter.settings import (
            resolve_jira_datacenter_settings,
        )

        settings = resolve_jira_datacenter_settings()
        missing = [
            name
            for name, value in (
                ("url", settings.url),
                ("JIRA_PAT", settings.pat),
            )
            if not value
        ]
        if missing:
            raise BackendEnvError(
                f"missing Jira Data Center configuration: {', '.join(missing)} "
                "(set url via [tool.rebar.reconciler].base_url; JIRA_PAT is env-only) "
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
        """Delegate Jira-family ``issuelinks`` canonicalization to the core helper."""
        from rebar_reconciler.link_direction import canonicalize_jira_issue_links

        return canonicalize_jira_issue_links(remote_fields)

    def link_payload_for_relation(self, relation: str) -> tuple[str, bool] | None:
        return RELATION_TO_JIRA_LINK.get(relation)

    # --- capability: SupportsComments (delegates to transport) ---
    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        # Sanitize before transport send. ``fit_comment`` reproduces this
        # composition for differ deduplication.
        return self.transport.add_comment(remote_id, self.sanitizer.sanitize_comment(body))

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_comment_map(project_key)
