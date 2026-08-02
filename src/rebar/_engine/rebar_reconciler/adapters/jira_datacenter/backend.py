"""``JiraDataCenterBackend`` — the DC backend port implementation (story J6, epic
e369).

Wires the DC transport (``transport.py``) together with the Jira-family SHARED
layer (``adapters/jira_family``) — the value maps, sanitizers, identity
convention, and absence-probe classifier that Cloud and DC both consume from ONE
implementation (PR #120's mistake was forking these per adapter). The only
DC-specific pieces are: the rich-text codec (``WikiTextCodec`` — plain
text/wiki markup, not ADF) and the user-identity model (``NameIdentity`` — DC's
``name`` username, not Cloud's opaque ``accountId``).

Registered under the key ``"jira-datacenter"``. Importing this module (which
``adapters/__init__.py`` does, alongside the existing Cloud import) is the SIDE
EFFECT that makes ``select_backend("jira-datacenter")`` resolve — see
``_backend_registry.register``.
"""

from __future__ import annotations

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
from rebar_reconciler.adapters.jira_family.outbound_mapper import OutboundFieldMapper
from rebar_reconciler.adapters.jira_family.rich_text import WIKI_DESCRIPTION_LIMIT, WikiTextCodec
from rebar_reconciler.adapters.jira_family.value_maps import (
    LOCAL_PRIORITY_TO_JIRA,
    LOCAL_STATUS_TO_JIRA,
)

_LOCAL_TO_JIRA_TYPE = {"task": "Task", "story": "Story", "bug": "Bug", "epic": "Epic"}


def _map_local_to_dc_fields(ticket: dict[str, Any]) -> dict[str, Any]:
    """Full local-ticket -> DC field mapping (the CREATE path).

    Deliberately self-contained rather than delegating to Cloud's
    ``adapters/jira/outbound_fields._map_local_to_jira_fields``: that function's
    sibling module lazy-loads ``adapters/jira/adf.py`` by file path, a Cloud-pinned
    coupling this package must not carry (this package imports nothing from
    ``adapters/jira/``). Uses the SAME Jira-family value maps Cloud uses, so the
    local<->Jira vocabulary stays one definition; only the rich-text fit
    (``WikiTextCodec`` — plain text, not ADF) differs.
    """
    codec = WikiTextCodec()
    return {
        "summary": ticket.get("title") or "",
        "description": codec.fit_outbound(ticket.get("description") or ""),
        "issuetype": _LOCAL_TO_JIRA_TYPE.get(ticket.get("ticket_type", "task"), "Task"),
        "priority": LOCAL_PRIORITY_TO_JIRA.get(ticket.get("priority", 2), "Medium"),
        "status": LOCAL_STATUS_TO_JIRA.get(ticket.get("status", "open"), "To Do"),
        "assignee": ticket.get("assignee") or "",
    }


class _DCOutbound:
    """Delegates changed-field mapping to the SHARED ``OutboundFieldMapper``,
    constructed with DC's ``WikiTextCodec`` (story J3) — the one place Cloud and
    DC diverge in this role."""

    def __init__(self) -> None:
        self._mapper = OutboundFieldMapper(WikiTextCodec())

    def map_local_to_remote(
        self,
        ticket: dict[str, Any],
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        emit_detach_clear: bool = False,
    ) -> dict[str, Any]:
        return _map_local_to_dc_fields(ticket)

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._mapper.map_fields_to_remote(changed, ticket, binding_store, local_ticket_types)

    def resolve_assignee(
        self, local_value: str, remote_identity: dict[str, Any] | None
    ) -> tuple[Any, bool, bool]:
        """Delegate to :class:`NameIdentity` (story J4), DC's user-identity model
        (compares against the remote identity's ``name``, never an accountId).
        The live account-search resolver is threaded in by
        ``compute_outbound_mutations`` as ``self._assignee_resolver`` — the same
        pre-existing core attribute-injection Cloud's ``_JiraOutbound`` reads
        (``outbound_field_diff.py``); see that method's docstring."""
        resolver = getattr(self, "_assignee_resolver", None)
        return NameIdentity(resolver=resolver).resolve(local_value, remote_identity)


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


class _DCSanitizer:
    """Delegates to the SHARED Jira-family sanitizers, binding DC's
    ``WikiTextCodec``/plain-text limit as the injected rich-text contract
    (``sanitize_description``/``sanitize_comment`` take theirs as a parameter
    precisely so Cloud and DC each bind their own — see
    ``jira_family/sanitizers.py``)."""

    def __init__(self) -> None:
        self._codec = WikiTextCodec()

    def sanitize_label(self, label: str) -> str:
        return _shared_sanitize_label(label)

    def sanitize_summary(self, summary: str) -> str:
        return _shared_sanitize_summary(summary)

    def sanitize_description(self, description: str) -> str:
        return _shared_sanitize_description(description, fit=self._codec.fit_outbound)

    def sanitize_comment(self, body: str) -> str:
        # PROVENANCE OF THE COMMENT CEILING (story 79d5) — 32767 here is a decision
        # backed by a primary source, not an assumption carried over from the
        # description. Jira's own ``jpm.xml`` defines the advanced setting
        # ``jira.text.field.character.limit`` with ``default-value 32767`` and a
        # description covering "Description, Environment, Comments and Text custom
        # fields"; JRASERVER-28519 records that 7.0.0 made 32767 the default (6.x
        # shipped the same key defaulting to 0 = unlimited, already listing Comments
        # in scope). Comments and descriptions are governed by ONE property on DC, so
        # binding both to ``WIKI_DESCRIPTION_LIMIT`` is faithful to DC rather than a
        # shortcut. Cloud needs its own ``adapters/jira/comment_limits`` module not
        # because its numbers differ but because its UNITS do — Cloud descriptions are
        # limited in ADF-SERIALIZED size, and DC has no ADF inflation to measure.
        # Two known limits of this binding, both tracked elsewhere:
        #   * the property is admin-configurable (0-2147483647), and rebar hardcodes
        #     the default — on a raised instance rebar truncates comments Jira would
        #     have accepted in full. Tracked as rebar:049e-9fac-a821-4ea2.
        #   * ``fit_outbound`` is the DESCRIPTION fitter. Correct today (it is a plain
        #     character truncation), but any future format-aware change to it would
        #     silently retarget comments — Cloud shows why that coupling is dangerous.
        # Convergence over this ceiling is pinned by
        # ``tests/unit/rebar_reconciler/mutate/test_dc_outbound_comment_length_convergence.py``.
        return _shared_sanitize_comment(
            body, truncate=self._codec.fit_outbound, max_chars=WIKI_DESCRIPTION_LIMIT
        )

    def fit_comment(self, body: str) -> str:
        return self._codec.fit_outbound(body)


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
    transport = JiraDataCenterTransport(
        client=client, project=settings.project, resolved_statuses=settings.resolved_statuses
    )
    # Conformance is asserted HERE, before the backend can be handed to a pass:
    # a missing member must be a loud construction failure, not a crash partway
    # through a writing pass that has already mutated the remote (story J9).
    assert_transport_conforms(transport, vendor="jira-datacenter")
    return JiraDataCenterBackend(
        transport=transport, client=client, instance=instance_from_base_url(settings.url)
    )


class JiraDataCenterBackend:
    """The Data Center backend: five role Protocols + links/comments/absence-probe
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

    def __init__(self, transport: Any, client: Any | None = None, instance: str = "") -> None:
        self.transport = transport
        #: The deployment label for :meth:`remote_ref`, supplied by ``build_backend``
        #: from the resolved settings. NOT resolved at call time — see the port docstring.
        self.instance = instance
        self.outbound = _DCOutbound()
        self.inbound = _DCInbound()
        self.sanitizer = _DCSanitizer()
        self.identity = JiraIdentityConvention()
        # The underlying jira.JIRA client, threaded through so a caller can wire
        # a LIVE assignee resolver (``_search_users_by_username`` bound to it) as
        # ``self.outbound._assignee_resolver`` — mirroring Cloud's pre-existing
        # attribute-injection seam (``outbound_field_diff.py``). ``None`` for a
        # transport built with a fake client (unit tests), which is fine: the
        # resolver being absent is exactly the "non-authoritative" fixture path
        # ``NameIdentity``/``_resolve`` already define.
        self._client = client
        if client is not None:
            self.outbound._assignee_resolver = (  # type: ignore[attr-defined]
                lambda name: _search_users_by_username(client, name)
            )

    @property
    def project(self) -> str:
        return self.transport.project

    @property
    def query_project(self) -> str:
        """Configured read/query project WITHOUT any create-time default — empty
        when unset so the inbound fetcher fails closed rather than querying every
        project (bug 626d; ticket 97f2).

        Resolved from settings rather than the transport, mirroring Cloud's
        ``JiraBackend.query_project``: :attr:`project` answers the transport's
        write scope, but the read scope must reflect the CONFIGURED value alone.
        DC has no create-time default to strip — ``resolve_jira_datacenter_settings``
        returns ``[tool.rebar.jira].project`` (env override ``JIRA_PROJECT``)
        verbatim, so an unset project stays the empty string.
        """
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
        a downstream connection attempt fail cryptically.
        """
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
        return self.transport.add_comment(remote_id, body)

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        return self.transport.get_comment_map(project_key)

    # --- capability: SupportsAbsenceProbe (delegates to transport) ---
    def probe_remote(self, remote_id: str) -> Any:
        return self.transport.probe_remote(remote_id)
