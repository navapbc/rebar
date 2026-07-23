"""The reconciler backend port — pinned by ADR 0035 §(d) (epic ``bbf1``).

This module defines the vendor-neutral interface the reconciler core drives a
backend through. It is *pure interface*: ``typing.Protocol`` declarations plus
the ``RemoteRef`` identity value — no behavior, no vendor imports, stdlib +
``typing`` only, so it loads in every context the reconciler is exec'd in
(normal import and ``spec_from_file_location`` by-path).

The design (ADR 0035 §(d)):

* rebar's **local** ticket is the canonical model — the seam speaks the
  local-field vocabulary and each adapter maps vendor⇄local.
* **Core owns diff/apply; adapters only read + enact.** A backend never diffs.
* A backend is one :class:`Backend` object exposing **five required role
  Protocols** (:class:`TicketTransport`, :class:`OutboundMapper`,
  :class:`InboundMapper`, :class:`FieldSanitizer`, :class:`IdentityConvention`)
  plus zero or more **opt-in capability Protocols**
  (:class:`SupportsLinks`, :class:`SupportsComments`,
  :class:`SupportsIncremental`).
* Callers detect a capability by an ``isinstance``-guarded check against the
  backend (behavioural, not structural introspection); the capability Protocols
  are therefore ``@runtime_checkable``.
* :class:`RemoteRef` is the identity tuple ``{vendor, instance, remote_id}`` that
  replaces the hardcoded ``"jira"`` provider literal and the bare remote key.

S2 (this story) only *defines* the port and lands a thin ``JiraBackend`` +
``JiraIdentityConvention`` implementation of it; routing core call sites through
the port is S4, config-driven selection is S3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class RemoteRef:
    """A backend-neutral identity for one remote work item.

    ``vendor`` names the backend family (e.g. ``"jira"``); ``instance`` names the
    concrete deployment (e.g. a Jira site / project host) so two instances of the
    same vendor never collide; ``remote_id`` is the backend's own opaque key for
    the item (e.g. a Jira issue key ``"DIG-1234"``). Frozen + value-equal so it can
    be a dict key and compared by identity content.
    """

    vendor: str
    instance: str
    remote_id: str


class BackendAssigneeNotFoundError(Exception):
    """Vendor-neutral base for "a requested assignee resolves to no assignable
    remote user" (ticket 4af8).

    The core apply path catches THIS base so it never imports a vendor-specific
    error type; each adapter's concrete assignee error (Jira:
    ``acli_subprocess.AssigneeNotFoundError``) subclasses it, so existing raises
    are unchanged while core-side ``except`` clauses stay backend-neutral.
    """


# ---------------------------------------------------------------------------
# Required role Protocols
# ---------------------------------------------------------------------------


class TicketTransport(Protocol):
    """CRUD transport against the remote tracker (today: ``acli.AcliClient``).

    The always-present read/write surface the core drives regardless of which
    optional capabilities a backend advertises.
    """

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]: ...

    def get_issue(self, remote_id: str) -> dict[str, Any]: ...

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None: ...

    def add_label(self, remote_id: str, label: str) -> None: ...

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]: ...


class OutboundMapper(Protocol):
    """Map a local ticket to the backend's field/value shapes (+ rich text).

    Delegates, for Jira, to ``outbound_fields._map_local_to_jira_fields`` (which
    itself fits rich text via ``adf``). No diffing — that stays in the core.
    """

    def map_local_to_remote(
        self,
        ticket: dict[str, Any],
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        emit_detach_clear: bool = False,
    ) -> dict[str, Any]: ...

    # Field-diff surface (ticket 4af8): the core outbound differ diffs a local
    # ticket against the remote snapshot through these instead of importing the
    # vendor's ``outbound_fields`` helpers directly. For Jira they delegate to
    # ``outbound_fields._diff_fields`` / ``._extract_jira_field`` /
    # ``._assignee_matches`` / the ``_LOCAL_TO_JIRA_TYPE`` map. Pure functions — no
    # diffing policy lives in the adapter; the core owns when they are called.
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
    ) -> dict[str, Any]: ...

    def extract_field(self, remote_fields: dict[str, Any], field: str) -> Any: ...

    def assignee_matches(self, local_val: str, remote_raw: Any) -> bool: ...

    def local_type_to_remote(self, ticket_type: str) -> str: ...


class InboundMapper(Protocol):
    """Map a backend issue payload back to local ticket field shapes.

    Delegates, for Jira, to ``inbound_fields._map_jira_to_local_fields``.
    """

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]: ...


class FieldSanitizer(Protocol):
    """Defend the backend's hard limits on field values (send-side only).

    Delegates, for Jira, to the ``adapters/jira/jira_fields.py`` sanitizers +
    ``comment_limits``. Each method returns a value fitted to the backend's limit
    (idempotent) or raises on an unfixable value (e.g. an invalid label).
    """

    def sanitize_label(self, label: str) -> str: ...

    def sanitize_summary(self, summary: str) -> str: ...

    def sanitize_description(self, description: str) -> str: ...

    def sanitize_comment(self, body: str) -> str: ...


class IdentityConvention(Protocol):
    """How a backend stores + reads the ``rebar-id`` back-pointer label.

    The back-pointer binds a remote issue to its local rebar ticket by stamping
    the **local id** into a label on the remote item (Jira: ``rebar-id:<local_id>``).
    Unlike the other four roles this had no single existing delegate — the
    convention was inlined at four core call sites (``f"rebar-id:{local_id}"``
    writes at ``dispatch_one``/``binding_store``/``apply_inbound_records`` + a
    ``rebar-id:``/``rebar-id-`` prefix scan on read at ``binding_walk``). S2
    introduces it as a self-contained pure object so the string convention lives
    in exactly one place instead of being hand-inlined.

    ``format_label`` produces the back-pointer label a backend stores for a local
    id; ``parse_label`` recovers the local id from a stored label (or ``None`` if
    the label is not an identity marker); ``is_identity_label`` is the cheap
    membership predicate the read/exclusion paths use. Behaviour is pinned to the
    current inlined convention (both the canonical ``rebar-id:`` colon form and
    the legacy ``rebar-id-`` hyphen form are recognised on read).
    """

    def format_label(self, local_id: str) -> str: ...

    def parse_label(self, label: str) -> str | None: ...

    def is_identity_label(self, label: str) -> bool: ...


# ---------------------------------------------------------------------------
# Opt-in capability Protocols (runtime-checkable for isinstance detection)
# ---------------------------------------------------------------------------


@runtime_checkable
class SupportsLinks(Protocol):
    """A backend that can enact + read issue links (Jira does).

    Core asks a backend to sync links only when ``isinstance(backend,
    SupportsLinks)``; a backend that does not implement this is never asked.
    """

    def set_relationship(
        self, from_id: str, to_id: str, link_type: str = "Blocks"
    ) -> dict[str, Any]: ...

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]: ...

    def link_type_for_relation(self, relation: str) -> tuple[str, bool] | None:
        """Map a rebar link ``relation`` to the backend's ``(link_type, swap)`` pair,
        or ``None`` when the relation has no reliable backend link type (skip it).

        Neutral accessor over the vendor relation↔link-type vocabulary (Jira:
        ``jira_fields._RELATION_TO_JIRA_LINK``) so the core outbound link differ never
        imports that map directly (ticket 4af8)."""
        ...


@runtime_checkable
class SupportsComments(Protocol):
    """A backend that can enact + read comments (Jira does)."""

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]: ...

    def get_comment_map(self, project_key: str) -> dict[str, Any]: ...


@runtime_checkable
class SupportsIncremental(Protocol):
    """A backend that can fetch only items changed since a watermark.

    Core uses an incremental fetch only when ``isinstance(backend,
    SupportsIncremental)``; otherwise it falls back to a full scan.
    """

    def search_incremental(self, project_key: str, since: str) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# The Backend facade
# ---------------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """One backend: the five role Protocols behind a single object.

    A concrete backend (e.g. ``JiraBackend``) exposes ``transport``, ``outbound``,
    ``inbound``, ``sanitizer`` and ``identity`` and may *additionally* implement
    any capability Protocol. ``vendor`` names the backend family for
    :class:`RemoteRef` construction.
    """

    @property
    def vendor(self) -> str: ...

    @property
    def transport(self) -> TicketTransport: ...

    @property
    def outbound(self) -> OutboundMapper: ...

    @property
    def inbound(self) -> InboundMapper: ...

    @property
    def sanitizer(self) -> FieldSanitizer: ...

    @property
    def identity(self) -> IdentityConvention: ...

    @property
    def project(self) -> str:
        """The backend's effective write/create project scope, with the backend's
        own create-time default applied (Jira: ``resolve_jira_settings`` with
        ``project_default="DIG"``). Used by the applier's cross-project safety
        guard, whose create client targets the SAME defaulted project (ticket
        4af8). Tolerates a settings-less test fake: it never reads the transport,
        so a fake transport without a project attribute still resolves."""
        ...

    @property
    def query_project(self) -> str:
        """The backend's configured read/query project scope, WITHOUT any
        create-time default (empty string when unset). The inbound fetcher scopes
        its search to this and FAILS CLOSED on an empty/invalid value rather than
        querying everything (bug 626d), so — unlike :attr:`project` — no default is
        substituted here (ticket 4af8)."""
        ...
