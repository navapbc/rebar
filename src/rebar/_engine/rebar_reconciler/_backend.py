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
  (:class:`SupportsLinks`, :class:`SupportsComments`).
* Callers detect a capability by an ``isinstance``-guarded check against the
  backend (behavioural, not structural introspection); the capability Protocols
  are therefore ``@runtime_checkable``.
* :class:`RemoteRef` is the identity tuple ``{vendor, instance, remote_id}`` that
  replaces the hardcoded ``"jira"`` provider literal and the bare remote key.

This module *defines* the port and lands a thin ``JiraBackend`` +
``JiraIdentityConvention`` implementation of it. Config-driven selection (S3,
:func:`~rebar_reconciler._backend_registry.select_backend`) and routing core call
sites through the port (S4, e.g. ``run_differs`` injecting ``backend.outbound`` /
``backend.inbound`` / ``backend.transport``) have both landed.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class BackendEnvError(RuntimeError):
    """Vendor-neutral "connection essentials missing" error (ticket 97f2/bbf1).

    Raised by ``Backend.assert_env_ready`` when a required connection setting
    (e.g. Jira's ``JIRA_URL``/``JIRA_USER``/``JIRA_API_TOKEN``) is absent.
    Subclasses ``RuntimeError`` so the pre-port ``except RuntimeError`` contract
    at the bootstrap call sites is preserved even as those sites move to
    catching this neutral type.
    """


class BackendPaginationStallError(RuntimeError):
    """A paged whole-project read's server stopped advancing (ticket 18a4).

    Raised when a pager can prove the server is no longer honouring its paging
    parameter — an offset pager whose ``startAt`` is ignored (the same page comes
    back at a new offset), or a cursor walk handed the same non-null token twice.
    Every further request would return the same page forever.

    **Readers must re-raise this PAST their fail-open handlers** — a stalled pager
    is a TRUNCATED whole-project read, not a transient fault. The contract, and the
    three incidents (bugs deac / 9263 / cabc) that made it one, are in ADR 0062.

    Lives in core beside the other ``Backend*`` errors so BOTH the adapters that
    raise it and the core ``fetcher`` handlers that must re-raise it can name one
    type: core must never import ``adapters/``, so an adapter-local error would
    be unnameable at exactly the boundary that absorbs it.
    """


class BackendAssigneeNotFoundError(Exception):
    """Vendor-neutral base for "a requested assignee resolves to no assignable
    remote user" (ticket 97f2/bbf1).

    The core apply path catches THIS base so it never imports a vendor-specific
    error type; each adapter's concrete assignee error (Jira:
    ``acli_subprocess.AssigneeNotFoundError``) subclasses it, so existing raises
    are unchanged while core-side ``except`` clauses stay backend-neutral.
    """


class BackendHTTPError(urllib.error.HTTPError):
    """The transport error contract: what EVERY backend raises for an HTTP failure.

    A backend's underlying client library has its own error type (Cloud's urllib
    transport raises ``urllib.error.HTTPError`` natively; the Data Center transport's
    ``pycontribs/jira`` client raises ``jira.exceptions.JIRAError``). The core must
    never learn one ``except`` clause per vendor, so each adapter **translates at its
    own boundary** into this single type, carrying the HTTP status through.

    It **subclasses** ``urllib.error.HTTPError`` deliberately, and that is the whole
    point: the core's existing ``except urllib.error.HTTPError`` clauses
    (``outbound_differ._safe_get_issue``, which maps ``.code == 404`` to ``_DELETED``;
    ``dispatch_apply_phases._update_one_apply_reporter``, which degrades softly on a
    4xx) keep matching with NO edit, ``.code`` keeps reading the status, and the
    live-validated Cloud path — which keeps raising the plain base type — is untouched.

    Construct it with urllib's own signature, ``(url, code, msg, hdrs, fp)``.
    """


@dataclass(frozen=True)
class RemoteRef:
    """A backend-neutral identity for one remote work item.

    ``vendor`` names the backend family (e.g. ``"jira"``); ``instance`` names the
    concrete deployment (e.g. a Jira site / project host); ``remote_id`` is the
    backend's own opaque key for the item (e.g. a Jira issue key ``"DIG-1234"``).
    Frozen + value-equal so it can be a dict key and compared by identity content.

    **WHAT ``instance`` DOES NOT DO** (corrected by ticket 6a91; this docstring
    previously claimed "so two instances of the same vendor never collide", which
    overstated it). It distinguishes two deployments of the SAME vendor *within this
    value* — Cloud vs Data Center is already separated by ``vendor`` itself
    (``"jira"`` vs ``"jira-datacenter"``). It does **NOT** prevent LOCAL-ID collision
    between two same-vendor deployments: ``inbound_translate._jira_key_to_local_id``
    is ``"jira-" + jira_key.lower()`` and consults nothing else, so two DC
    deployments that each own a project ``DIG`` both mint ``jira-dig-123``. Making
    the local id instance-aware would change the id scheme for every existing
    Jira-sourced ticket — a breaking, store-wide migration, deliberately not done
    here. A `RemoteRef` is also NOT persisted anywhere.
    """

    vendor: str
    instance: str
    remote_id: str


# ---------------------------------------------------------------------------
# Required role Protocols
# ---------------------------------------------------------------------------


#: The transport members the core requires of EVERY backend: the original CRUD
#: surface plus every additional operation the core calls unconditionally.
#: ``isinstance(x, TicketTransport)`` is defined against exactly this set — see
#: :class:`_TransportPortMeta`.
_REQUIRED_TRANSPORT_MEMBERS = (
    "create_issue",
    "get_issue",
    "update_issue",
    "transition_issue_by_name",
    "add_label",
    "search_issues",
    "get_issue_by_rest",
    "delete_issue",
    "get_comments",
    "get_issue_links",
    "delete_issue_link",
    "get_parent_map",
    "set_parent",
    "remove_label",
    "get_issue_property",
    "set_issue_property",
    "set_entity_property",
    "set_reporter",
    "validate_assignee_exists",
)


class _TransportPortMeta(type(Protocol)):  # type: ignore[misc]
    """Makes :class:`TicketTransport` conformance mean what the story needs it to
    mean, in two respects.

    **1. The check is uncached, so it is always current.** ``typing``'s own
    ``isinstance`` against a runtime-checkable Protocol goes through
    ``ABCMeta``'s subclass CACHE, which memoises the first positive answer per
    class: a class that conformed once keeps reporting ``True`` even after a
    member is removed. That is harmless for a static class definition and fatal
    for a guard whose entire job is detecting a missing member — including in the
    test that proves the guard has teeth. :meth:`__instancecheck__` therefore
    evaluates the required set directly, every time.

    **2. It carries the CAPABILITY members** the core also reaches for on a
    transport receiver — ``set_relationship`` / ``get_issuelinks_map``
    (``SupportsLinks``) and ``add_comment`` / ``get_comment_map``
    (``SupportsComments``).

    Two facts about :class:`TicketTransport` are BOTH true and pull in opposite
    directions. (1) The core calls those four directly on a transport object, so
    an audit asking "is every transport member a core module reaches for declared
    on the port?" must be able to find them here. (2) They are nonetheless the
    OPT-IN capability surface: a tracker with no link model, or no comment model,
    is still a perfectly valid transport, and the whole point of the capability
    Protocols is that a backend advertises them rather than being forced to
    provide them. Declaring them in the class body would collapse (2) into (1) and
    make every future non-Jira transport fail conformance for lacking a feature it
    was never obliged to have.

    Attaching them to the METACLASS resolves that: ``hasattr(TicketTransport,
    "add_comment")`` is ``True`` (the audit finds them, attributed to the right
    capability), while ``typing``'s protocol-attribute collection — which walks the
    CLASS's ``__mro__``, in every Python from 3.11 through 3.14 — never sees them,
    so structural conformance stays exactly the always-required set below. They are
    reference markers, not implementations; nothing calls them.
    """

    #: ``SupportsLinks``: create one link between two remote items.
    set_relationship = None
    #: ``SupportsLinks``: every item's links across a project.
    get_issuelinks_map = None
    #: ``SupportsComments``: post one comment.
    add_comment = None
    #: ``SupportsComments``: every item's comments across a project.
    get_comment_map = None

    def __instancecheck__(cls, instance: Any) -> bool:
        """``isinstance(x, TicketTransport)`` ⇔ ``x`` has every always-required
        member. Evaluated fresh (no ``ABCMeta`` subclass cache) so the answer
        tracks the object as it actually is. Any other protocol using this
        metaclass falls back to ``typing``'s standard behaviour."""
        if cls is not TicketTransport:
            return bool(super().__instancecheck__(instance))
        return all(hasattr(instance, member) for member in _REQUIRED_TRANSPORT_MEMBERS)


@runtime_checkable
class TicketTransport(Protocol, metaclass=_TransportPortMeta):
    """CRUD transport against the remote tracker (today: ``acli.AcliClient``).

    The always-present read/write surface the core drives regardless of which
    optional capabilities a backend advertises.

    **This Protocol must state what the CORE actually requires, not a comfortable
    subset of it.** It originally declared six members while the core reached for
    many more. The methods below are the always-required subset, and the remaining
    four (``set_relationship`` / ``get_issuelinks_map`` /
    ``add_comment`` / ``get_comment_map``) are the opt-in capability surface, so a
    links-less transport still conforms. The omitted methods were unchecked by
    every conformance test, and a transport missing all of them passed
    ``isinstance``, the backend contract
    suite, and 1600+ unit tests while being unable to complete a single writing
    pass (story J9, epic ``e369``). Conformance to an incomplete port proves less
    than it appears to — so a member the core calls belongs HERE, even when only
    one backend implements it today.

    It is ``@runtime_checkable`` for the same reason the capability Protocols are:
    without the decorator ``isinstance(x, TicketTransport)`` raises ``TypeError``
    rather than returning ``False``, so a construction-time conformance guard
    cannot be written against it at all. (``isinstance`` against a runtime-checkable
    Protocol checks member PRESENCE, not signatures — which is exactly the check
    that would have caught this defect.)
    """

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]: ...

    def get_issue(self, remote_id: str) -> dict[str, Any]: ...

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None: ...

    def add_label(self, remote_id: str, label: str) -> None: ...

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]: ...

    # -- additional operations the core also reaches for -----------------
    # Grouped, not merged into the block above, so the audit trail stays legible:
    # every member below has at least one call site in a core module, and most of
    # those sites swallow ``Exception``, which is why their absence degraded
    # SILENTLY rather than crashing.

    def get_issue_by_rest(self, remote_id: str) -> dict[str, Any]:
        """Read an issue from the primary store, bypassing any search-index lag
        (``outbound_differ._safe_get_issue``). The one call site of the twelve that
        lets the error PROPAGATE."""
        ...

    def delete_issue(self, remote_id: str) -> dict[str, Any]:
        """Delete an issue. An already-absent issue is idempotent success."""
        ...

    def get_comments(self, remote_id: str) -> list[dict[str, Any]]:
        """Every comment on an issue, as raw payload dicts."""
        ...

    def get_issue_links(self, remote_id: str) -> list[dict[str, Any]]:
        """The issue's links in REST-nested shape (``[{"id", "type", …}]``)."""
        ...

    def delete_issue_link(self, link_id: str) -> dict[str, Any]:
        """Delete one issue link by id. A concurrent removal (404/409) is
        idempotent success and must be absorbed by the TRANSPORT — the core's
        handler for it is written against a Cloud-specific exception type."""
        ...

    def get_parent_map(self, project_key: str, jql: str | None = None) -> dict[str, str | None]:
        """``{issue_key → parent_key | None}`` for a project. Degradation contract
        (``fetcher``): log a warning and return ``{}`` on failure, never raise."""
        ...

    def set_parent(self, remote_id: str, parent_key: str | None) -> None:
        """Set the parent, or clear it when ``parent_key`` is falsy."""
        ...

    def remove_label(self, remote_id: str, label: str) -> None:
        """Remove ONE label, leaving the issue's other labels intact."""
        ...

    def get_issue_property(self, remote_id: str, property_key: str) -> Any:
        """Return the stored JSON value for one issue property."""
        ...

    def set_issue_property(self, remote_id: str, property_key: str, value: Any) -> None:
        """Store ``value`` VERBATIM as an issue property. Wrapping it (e.g. as
        ``{"value": …}``) stores the wrong shape and breaks correlation without
        ever raising."""
        ...

    def set_entity_property(self, remote_id: str, prop_name: str, value: Any) -> None:
        """Store an entity property — the same endpoint and the same verbatim-value
        contract as :meth:`set_issue_property`."""
        ...

    def set_reporter(self, remote_id: str, account_id: str) -> None:
        """Set the reporter to the identity seam's ``external_id`` for the backend
        (Cloud: an accountId; Data Center: a username)."""
        ...

    def validate_assignee_exists(
        self,
        assignee: str,
        *,
        issue_key: str | None = None,
        project_key: str | None = None,
    ) -> str:
        """Resolve ``assignee`` to the backend's assignable-user identifier, or
        raise the backend's ``AssigneeNotFoundError``. The RETURN VALUE flows on as
        the resolved assignee identity, so it must be the backend's own identifier
        shape — never a bare truthy value."""
        ...


def assert_transport_conforms(transport: Any, *, vendor: str) -> None:
    """Fail LOUDLY, at backend CONSTRUCTION, if ``transport`` is missing a
    required :class:`TicketTransport` member.

    Story J9 exists because a transport missing TWELVE members passed
    ``isinstance``, the backend contract suite, and 1600+ unit tests, and then
    crashed mid-writing-pass on ``set_entity_property``. Declaring the members on
    the port (above) closes the *declaration* half of that gap; this closes the
    *enforcement* half. Without it the port is a description that nothing checks
    at the moment a backend is assembled, and the first evidence of a missing
    member is again a partial pass that has already written to the remote.

    Construction is the right choke point precisely because the call sites are
    not: seven of those twelve members are invoked from core paths that swallow
    ``Exception`` at EVERY site, so a missing member there produces no crash and
    no record. A failure raised here happens before any mutation is applied.

    Raises ``BackendEnvError`` — the same type the factories already raise for a
    mis-configured environment, which is what a non-conforming transport is.
    """
    missing = [m for m in _REQUIRED_TRANSPORT_MEMBERS if not hasattr(transport, m)]
    if missing:
        raise BackendEnvError(
            f"the {vendor!r} backend's transport "
            f"({type(transport).__module__}.{type(transport).__qualname__}) is missing "
            f"required TicketTransport member(s): {sorted(missing)}. A reconcile pass "
            f"would fail partway through — after writing — rather than here."
        )


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
        *,
        suppressed_out: list[str] | None = None,
        status_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """``suppressed_out`` (ticket 8390): an optional sink an implementation MAY
        append to when it drops a parent it could otherwise have sent, so the caller
        can report the loss. Reporting only — it never changes the mapped dict, and a
        backend with no such suppression (Data Center maps no parent at all) accepts
        and ignores it.

        ``status_map`` (S2): the effective per-project local->Jira status map
        (``config.effective_status_map``); ``None`` falls back to the built-in map. A
        local status with NO target is OMITTED (map-or-drift), never coerced."""
        ...

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        status_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Map a CANONICAL changed-fields dict (local field names → local values) to the
        backend's mutation-field shapes, at the emission boundary (ticket 625b). The core
        diffs in local shape; this translates only the changed subset back — field-name
        reconciliation and value mapping (incl. rich-text fit) happen HERE.

        ``status_map`` (S2): the effective per-project local->Jira status map; ``None``
        falls back to the built-in map. A local status with NO target is OMITTED
        (map-or-drift), never coerced to ``"To Do"``."""
        ...

    def resolve_assignee(
        self,
        local_value: str,
        remote_identity: dict[str, Any] | None,
        *,
        assignee_resolver: Callable[[str], tuple[Any, bool, bool]] | None = None,
    ) -> tuple[Any, bool, bool]:
        """Resolve a local assignee against the remote identity, returning
        ``(value, authoritative, is_account_id)`` (ticket 625b). Encapsulates the
        3-state account-resolution fast-path (converged / desired-unassigned /
        accountId) the core diff consults before emitting an assignee change.

        ``assignee_resolver`` is the LIVE account search, bound by the caller to the
        current remote key: ``local_value -> (account|None, authoritative, is_account_id)``.
        It is a declared parameter rather than an attribute the implementation discovers
        by ``getattr`` (ticket 65d7) — the side-channel form failed silently, was invisible
        to the type checker, and let PR #120 ship a backend whose whole authoritative
        branch was dead code.

        ``None`` (the default) means "no live account search on this path" and keeps its
        long-standing meaning: the permissive, non-authoritative string match. That is now
        a default the caller CHOOSES, not the residue of an injection that did not happen."""
        ...

    @property
    def comment_codec(self) -> Any:
        """The deployment's ``RichTextCodec`` for comment bodies (emersed-specific-mutt).

        Cloud's ``AdfCodec`` / DC's ``WikiTextCodec``, the SAME instance this mapper
        renders descriptions through, so the outbound comment-diff normalizes its LOCAL
        dedup key exactly the way the landed wire reads. Injected as a contract via the
        Backend port so the shared comment-diff layer never reaches for a concrete codec
        (the Jira-family import boundary). Typed ``Any`` to keep the port neutral of the
        jira-family ``RichTextCodec`` symbol."""
        ...


class InboundMapper(Protocol):
    """Map a backend issue payload back to local ticket field shapes.

    Delegates, for Jira, to ``inbound_fields._map_jira_to_local_fields``.
    """

    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]: ...

    def normalize_rich_text(self, body: Any) -> str:
        """Decode a rich-text payload to plain text (ticket 21ca).

        Jira: an ADF dict decodes via ``adf_to_text``; a plain string passes
        through unchanged; ``None`` yields ``""``. Serves BOTH the inbound apply
        path (defense-in-depth, bug 1bb2-5da5) and the outbound comment-diff
        decode (formerly each a private vendor reach-through).
        """
        ...


class FieldSanitizer(Protocol):
    """Defend the backend's hard limits on field values (send-side only).

    Delegates, for Jira, to the ``adapters/jira/jira_fields.py`` sanitizers +
    ``comment_limits``. Each method returns a value fitted to the backend's limit
    (idempotent) or raises on an unfixable value (e.g. an invalid label).
    """

    def sanitize_label(self, label: str) -> str: ...

    def sanitize_summary(self, summary: str) -> str: ...

    def sanitize_description(self, description: str) -> str: ...

    def fit_comment(self, body: str) -> str:
        """Fit a comment body to the backend's hard length limit, silently.

        A pure fit-to-limit used by the comment-diff comparison: it must return
        exactly the marker-free body the backend's SEND path lands (see
        ``outbound_comments.fit_comment_as_sent``), or an over-length comment can
        never match on the next pass and re-posts forever. The port deliberately
        carries no ``sanitize_comment`` member: the warning-bearing send-side
        sanitizer is a backend-internal concern (DC wires its own inside
        ``add_comment``; Cloud fits inside ``acli_cli_ops.add_comment``), and a
        port member nothing calls is a false assurance (bug b9b4-f460-2d54-4872).
        """
        ...


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

    def map_remote_links(self, remote_fields: dict[str, Any]) -> list[tuple[str | None, str, str]]:
        """Canonicalize a remote issue's raw link payload into the core's relation
        vocabulary (ticket eefd). Returns one entry per distinct
        ``(opaque_vendor_type, remote_key)`` pair: ``(relation, remote_key,
        opaque_vendor_type)``. ``relation`` is ``None`` when the vendor link type has
        no canonical rebar relation (the entry is still returned — carrying its
        remote key + vendor type — so core never mistakes an unmapped link type for
        "absent" and never removes it)."""
        ...

    def link_payload_for_relation(self, relation: str) -> tuple[str, bool] | None:
        """Map a canonical rebar ``relation`` to this backend's outbound link payload
        ``(opaque_vendor_type, swap_endpoints)`` (ticket eefd), or ``None`` when the
        relation has no reliable vendor link type (core skips emitting an ADD for
        it)."""
        ...


@runtime_checkable
class SupportsComments(Protocol):
    """A backend that can enact + read comments (Jira does)."""

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]: ...

    def get_comment_map(self, project_key: str) -> dict[str, Any]: ...


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

    def remote_ref(self, remote_id: str) -> RemoteRef:
        """This backend's identity for ``remote_id``, naming vendor AND deployment.

        DECLARED HERE because the test double has implemented it since J7
        (``tests/unit/rebar_reconciler/backend_support.py``) while the port never declared it and
        no real backend provided it — so the contract tests ran against a fake strictly more
        capable than production. Ticket 6a91 closed that gap.

        Implementations MUST NOT resolve configuration when called: the ``instance`` value is
        supplied at CONSTRUCTION by the ``build_backend`` factory, which already holds the
        resolved settings. A call-time resolve reaches into ambient config, which makes the
        backend unusable in any context that has none — and a settings-resolving PROPERTY has
        already earned a ``Verified -1`` on this project once (change cd78: Python 3.12 changed
        runtime-checkable ``Protocol`` ``isinstance`` from ``hasattr`` to
        ``inspect.getattr_static``, so a property that raises breaks ``isinstance`` on CI's 3.11
        while passing locally).
        """
        ...

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
        97f2). Tolerates a settings-less test fake: it never reads the transport,
        so a fake transport without a project attribute still resolves."""
        ...

    @property
    def query_project(self) -> str:
        """The backend's configured read/query project scope, WITHOUT any
        create-time default (empty string when unset). The inbound fetcher scopes
        its search to this and FAILS CLOSED on an empty/invalid value rather than
        querying everything (bug 626d), so — unlike :attr:`project` — no default is
        substituted here (ticket 97f2)."""
        ...

    def assert_env_ready(self) -> None:
        """Fail fast when a connection essential (e.g. Jira's JIRA_URL / JIRA_USER /
        JIRA_API_TOKEN) is missing, BEFORE the transport is used for bootstrap-band
        execution. Raises the neutral :class:`BackendEnvError` naming the missing
        var(s) rather than letting a downstream call fail with a cryptic error
        (ticket 97f2)."""
        ...


# ---------------------------------------------------------------------------
# Capability narrowing (ticket cc77)
# ---------------------------------------------------------------------------
#
# The four opt-in capability members (``set_relationship`` / ``get_issuelinks_map``
# / ``add_comment`` / ``get_comment_map``) live on :class:`_TransportPortMeta`, NOT
# in :class:`TicketTransport`'s body — deliberately, so ``hasattr`` attributes them
# while ``typing``'s protocol-attribute collection does not, letting a links-less or
# comment-less transport still conform. mypy does not consult metaclass attributes
# for a Protocol-typed value, so a core call site reaching for one through a
# ``TicketTransport``-annotated parameter is reported ``[attr-defined]`` — correctly:
# the annotated VIEW does not offer the member.
#
# Such a site is resolved by narrowing to the capability Protocol that declares it,
# spelled ``cast("SupportsComments", client).add_comment(...)`` (a string forward
# reference, so the import stays under ``TYPE_CHECKING`` and nothing changes at
# runtime). The narrowed value is still attribute-checked — a typo in the member
# name remains an error — so this is a NARROWING, not a widening to ``Any``. Widening
# is the one resolution cc77 forbids: it reinstates exactly the blindness the story
# removes. Adding the member to :class:`TicketTransport`'s body is also wrong — it
# would oblige every future transport to provide an opt-in feature.
