"""``JiraDataCenterBackend`` — the DC backend port implementation (story J6, epic
e369).

Wires the DC transport (``transport.py``) together with the Jira-family SHARED
layer (``adapters/jira_family``) — the value maps, sanitizers, and identity
convention that Cloud and DC both consume from ONE implementation (PR #120's
mistake was forking these per adapter). The only
DC-specific pieces are: the rich-text codec (``WikiTextCodec`` — plain
text/wiki markup, not ADF) and the user-identity model (``NameIdentity`` — DC's
``name`` username, not Cloud's opaque ``accountId``).

Registered under the key ``"jira-datacenter"``. Importing this module (which
``adapters/__init__.py`` does, alongside the existing Cloud import) is the SIDE
EFFECT that makes ``select_backend("jira-datacenter")`` resolve — see
``_backend_registry.register``.
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
    resolve_outbound_status,
)
from rebar_reconciler.adapters.jira_family.rich_text import (
    _WIKI_TRUNCATION_SUFFIX,
    WikiTextCodec,
    cutover_clients,
)
from rebar_reconciler.adapters.jira_family.value_maps import (
    LOCAL_PRIORITY_TO_JIRA,
)

# Story bd9e (epic 3e73): the local->Jira TYPE map used to be re-declared here as a
# second literal, read by the DC create path below. It is now imported from the
# Jira-family shared layer alongside the priority/status maps, so both create paths
# resolve to ONE object. Kept under the historical private name so the call site
# below needs no change.
from rebar_reconciler.adapters.jira_family.value_maps import (
    LOCAL_TYPE_TO_JIRA as _LOCAL_TO_JIRA_TYPE,
)


def _map_local_to_dc_fields(
    ticket: dict[str, Any], status_map: dict[str, str] | None = None
) -> dict[str, Any]:
    """Full local-ticket -> DC field mapping (the CREATE path).

    Deliberately self-contained rather than delegating to Cloud's
    ``adapters/jira/outbound_fields._map_local_to_jira_fields``: that function's
    sibling module lazy-loads ``adapters/jira/adf.py`` by file path, a Cloud-pinned
    coupling this package must not carry (this package imports nothing from
    ``adapters/jira/``). Uses the SAME Jira-family value maps Cloud uses, so the
    local<->Jira vocabulary stays one definition; only the rich-text fit
    (``WikiTextCodec`` — plain text, not ADF) differs.

    ``status_map`` (S2): the effective per-project local->Jira status map; ``None``
    falls back to the built-in ``LOCAL_STATUS_TO_JIRA``. A local status with NO target
    (map-or-drift) OMITS the ``status`` field entirely, never coercing it.
    """
    codec = WikiTextCodec(rich="dc" in cutover_clients())
    fields: dict[str, Any] = {
        "summary": ticket.get("title") or "",
        # Render, then fit — ``to_wire(fit_outbound(...))``, matching ``_issues.py``'s
        # create path and ``OutboundFieldMapper``'s update path. Fitting WITHOUT
        # ``to_wire`` is the half-cutover bug: this site builds a rich codec but would
        # post raw Markdown on CREATE while every later update posted rendered wiki, so
        # a freshly created issue read as broken formatting until someone edited it.
        "description": codec.to_wire(codec.fit_outbound(ticket.get("description") or "")),
        "issuetype": _LOCAL_TO_JIRA_TYPE.get(ticket.get("ticket_type", "task"), "Task"),
        "priority": LOCAL_PRIORITY_TO_JIRA.get(ticket.get("priority", 2), "Medium"),
        "assignee": ticket.get("assignee") or "",
    }
    target = resolve_outbound_status(ticket.get("status", "open"), status_map)
    if target is not None:
        fields["status"] = target
    return fields


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
        #: DC's live account search, bound ONCE to the deployment's client (it takes no
        #: remote key, unlike Cloud's per-issue closure). A declared constructor parameter
        #: rather than an attribute the backend sets from outside (ticket 65d7): the
        #: side-channel form was invisible to mypy and silently degraded every resolution
        #: to non-authoritative if it ever failed to happen.
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
    ) -> dict[str, Any]:
        # ``suppressed_out`` (ticket 8390) is accepted and IGNORED on purpose:
        # ``_map_local_to_dc_fields`` never maps a parent at all, so this backend has
        # no suppression to report and appending anything here would invent one.
        return _map_local_to_dc_fields(ticket, status_map)

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        status_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._mapper.map_fields_to_remote(
            changed, ticket, binding_store, local_ticket_types, status_map
        )

    def resolve_assignee(
        self,
        local_value: str,
        remote_identity: dict[str, Any] | None,
        *,
        assignee_resolver: Callable[[str], tuple[Any, bool, bool]] | None = None,
    ) -> tuple[Any, bool, bool]:
        """Delegate to :class:`NameIdentity` (story J4), DC's user-identity model
        (compares against the remote identity's ``name``, never an accountId).

        Two resolvers can be in play, so their precedence is explicit (ticket 65d7):
        the core diff's ``assignee_resolver`` — bound to the issue being diffed — WINS,
        and the constructor's deployment-wide search is the fallback. That is exactly
        the old semantics: the core used to *overwrite* the attribute this backend had
        set at construction, so a core-supplied resolver already took precedence."""
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
    """Truncate a DC COMMENT body to ``max_chars`` — the comment path's OWN rule.

    Bug 049e. This exists so the comment path does not borrow ``WikiTextCodec.
    fit_outbound``, the DESCRIPTION fitter: the two coincide today only because DC's
    description fit is a plain character truncation, and a future format-aware change
    to description fitting must not silently retarget comments.

    ``max_chars <= 0`` means UNLIMITED (``jira.text.field.character.limit``'s own
    ``0`` convention) — the body is returned untouched.

    The truncation marker is the SHARED ``_WIKI_TRUNCATION_SUFFIX``, imported rather
    than redeclared: a Jira reader must see ONE marker on DC regardless of which
    field was shortened, and byte-identity with the description rule at the default
    ceiling is pinned by the existing DC characterization/held-out tests. Sharing an
    inert marker STRING is not the coupling this function breaks — sharing the fitter
    FUNCTION is. Idempotent: re-fitting an already-fitted value is a no-op.
    Non-``str`` values pass through, matching the codec's "never coerce" behaviour.
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
        """This instance's comment ceiling, resolved ONCE per sanitizer (bug 049e).

        PROVENANCE OF THE CEILING (story 79d5, made configurable by bug 049e) —
        32767 is a decision backed by a primary source, not an assumption carried
        over from the description: Jira's own ``jpm.xml`` defines the advanced
        setting ``jira.text.field.character.limit`` with ``default-value 32767`` and
        a description covering "Description, Environment, Comments and Text custom
        fields", and JRASERVER-28519 records that 7.0.0 made 32767 the default (6.x
        shipped the same key defaulting to 0 = unlimited, already listing Comments in
        scope). Comments and descriptions are governed by ONE property on DC, so the
        two ceilings agreeing NUMERICALLY is faithful to DC rather than a shortcut.
        Cloud needs its own ``adapters/jira/comment_limits`` module not because its
        numbers differ but because its UNITS do — Cloud descriptions are limited in
        ADF-SERIALIZED size, and DC has no ADF inflation to measure.

        What 049e changed: that property is ADMIN-SETTABLE (``0..2147483647``, ``0`` =
        unlimited), so 32767 is only the DEFAULT. It now comes from
        ``[tool.rebar.reconciler].comment_max_chars`` (see
        ``settings.resolve_comment_max_chars`` for the full citation and for why the
        value is NOT discovered from the instance). Resolution is LAZY — a
        ``_DCSanitizer`` is built in ``JiraDataCenterBackend.__init__``, which must
        not require a resolvable config — and cached, so a hot comment loop resolves
        config once. Tests inject the ceiling via the constructor instead.
        """
        if self._comment_max_chars is None:
            from rebar_reconciler.adapters.jira_datacenter.settings import (
                resolve_comment_max_chars,
            )

            self._comment_max_chars = resolve_comment_max_chars()
        return self._comment_max_chars

    def _fit_raw(self, text: str) -> str:
        """The comment path's OWN vendor fit — a plain right-truncation at the
        deployment-resolved ceiling. DELIBERATELY NOT ``self._codec.fit_outbound``
        (bug 049e): that is the DESCRIPTION fitter. The two agreed today only
        because DC's description fit happens to be a plain character truncation —
        Cloud shows why the coupling is a trap, since its description fitter
        measures ADF-SERIALIZED size and would be catastrophically wrong for a
        comment. ``_truncate_dc_comment_body`` is the comment path's own rule, so a
        future format-aware change to description fitting cannot silently retarget
        comments. Convergence over this ceiling is pinned by ``tests/unit/
        rebar_reconciler/mutate/test_dc_outbound_comment_length_convergence.py``."""
        return _truncate_dc_comment_body(text, self.comment_max_chars())

    def sanitize_comment(self, body: str) -> str:
        # The SEND-path sanitizer (bug b9b4-f460-2d54-4872: now actually wired, via
        # ``JiraDataCenterBackend.add_comment``). The fit runs through the shared
        # ``fit_preserving_marker`` — the same marker-preserving composition Cloud's
        # send path uses (bug 5931) — so an over-length decorated body is truncated
        # in its CONTENT and re-decorated, never cutting RECONCILER_MARKER off the
        # tail. A marker-less body goes straight to ``_fit_raw``, byte-identical to
        # the pre-wiring behaviour. The operator truncation warning stays in the
        # shared ``jira_family.sanitize_comment``.
        from rebar_reconciler.outbound_comments import fit_preserving_marker

        return _shared_sanitize_comment(
            body,
            truncate=lambda text: fit_preserving_marker(text, self._fit_raw),
            max_chars=self.comment_max_chars(),
        )

    def fit_comment(self, body: str) -> str:
        """The differ-side comparison transform: exactly the marker-free body the
        send path LANDS (bug b9b4-f460-2d54-4872, the e339 convergence class).

        ``_diff_comments`` builds its dedup key from this and compares it against
        the marker-stripped body read back from Jira, so it must reproduce the
        send composition — decorate, :func:`fit_preserving_marker` over
        :meth:`_fit_raw`, strip the decoration — or an over-length comment never
        matches and re-posts every pass. ``fit_comment_as_sent`` runs precisely
        that composition; a body already within the limit returns byte-identical.
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

    #: The store-facing FAMILY this deployment belongs to (bug 5f48). ``vendor`` is
    #: per-deployment, but the store's identity provider and its CREATION_CHANNELS
    #: vocabulary are per-family — a human assigned on Cloud and on DC is ONE identity,
    #: so both mint under ``jira``. The deployment is distinguished by
    #: ``RemoteRef.instance``, not by forking the store vocabulary.
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
        #: The deployment label for :meth:`remote_ref`, supplied by ``build_backend``
        #: from the resolved settings, or derived from the captured scope's base URL
        #: (RP-04 S2) when not given. NOT resolved at call time — see the port docstring.
        self.instance = instance or (
            instance_from_base_url(scope.base_url) if scope is not None else ""
        )
        #: The CAPTURED reconciler settings (RP-04 S2). When present, the read scope and
        #: ``assert_env_ready`` answer from this frozen scope instead of re-resolving
        #: ambient env/config on each access; ``None`` keeps the legacy behaviour.
        self._scope = scope
        # The underlying jira.JIRA client, threaded through so this deployment's LIVE
        # assignee search (``_search_users_by_username`` bound to it) reaches the outbound
        # mapper as a DECLARED constructor parameter (ticket 65d7 — it used to be assigned
        # onto the mapper as a private attribute of ``self.outbound`` behind a
        # ``# type: ignore[attr-defined]``). ``None`` for a transport built with a fake
        # client (unit tests), which is fine: the resolver being absent is exactly the
        # "non-authoritative" fixture path ``NameIdentity``/``_resolve`` already define.
        self._client = client
        self.outbound = _DCOutbound(
            assignee_resolver=(
                (lambda name: _search_users_by_username(client, name))
                if client is not None
                else None
            )
        )
        self.inbound = _DCInbound()
        self.sanitizer = _DCSanitizer()
        self.identity = JiraIdentityConvention()

    @property
    def project(self) -> str:
        if self._scope is not None:
            return self._scope.project
        return self.transport.project

    @property
    def query_project(self) -> str:
        """Configured read/query project WITHOUT any create-time default — empty
        when unset so the inbound fetcher fails closed rather than querying every
        project (bug 626d; ticket 97f2).

        Answered from the captured scope (RP-04 S2) when present, else resolved from
        settings rather than the transport, mirroring Cloud's
        ``JiraBackend.query_project``: :attr:`project` answers the transport's
        write scope, but the read scope must reflect the CONFIGURED value alone.
        DC has no create-time default to strip — ``resolve_jira_datacenter_settings``
        returns ``[tool.rebar.jira].project`` (env override ``JIRA_PROJECT``)
        verbatim, so an unset project stays the empty string.
        """
        if self._scope is not None:
            return self._scope.query_project
        from rebar_reconciler.adapters.jira_datacenter.settings import (
            resolve_jira_datacenter_settings,
        )

        return resolve_jira_datacenter_settings().project

    def assert_env_ready(self) -> None:
        """Fail-fast when a DC connection essential is missing, BEFORE the
        transport is used for bootstrap-band execution (ticket 97f2).

        DC's essentials are the base ``url`` (``[tool.rebar.reconciler].base_url``)
        and the ``JIRA_PAT`` bearer token — the env-only Personal Access Token
        (Jira 8.14+) that is deliberately never a file-config key. This is the DC
        analogue of Cloud's JIRA_URL/JIRA_USER/JIRA_API_TOKEN check: DC has no
        separate user credential, because the PAT identifies the account itself.
        EVERY missing essential is named in one message, so an operator is not
        walked through a fix-one-rerun loop. Raises the neutral
        :class:`BackendEnvError` (subclasses ``RuntimeError``) rather than letting
        a downstream connection attempt fail cryptically. When composed with a
        captured scope (RP-04 S2), the check runs against that scope."""
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
        """Canonicalize DC ``issuelinks`` into ``(relation, remote_key,
        opaque_vendor_type)`` entries — identical shape/logic to Cloud's
        ``JiraBackend.map_remote_links`` (the link vocabulary is Jira-family
        general, REST v2 and v3 carry the same ``issuelinks`` shape)."""
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
        return RELATION_TO_JIRA_LINK.get(relation)

    # --- capability: SupportsComments (delegates to transport) ---
    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        # Bug b9b4-f460-2d54-4872: fit the body to the deployment-resolved ceiling
        # BEFORE the transport — the DC transport hands it straight to the jira
        # client, and an over-length body is rejected without landing, re-emitting
        # every pass (bug 6afc's loop). ``sanitize_comment`` fits through
        # ``fit_preserving_marker`` so the RECONCILER_MARKER survives the cut, and
        # ``fit_comment`` (the differ's dedup key) reproduces this exact result.
        return self.transport.add_comment(remote_id, self.sanitizer.sanitize_comment(body))

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_comment_map(project_key)
