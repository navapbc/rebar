"""Outbound differ for bidirectional Jira sync.

Compares local ticket state against the Jira working-set snapshot and emits
a list of OutboundMutation objects describing changes to push from local to
Jira. Uses a BindingStore (from PR #401) to map local ticket IDs to Jira keys.

Local is the source of truth. Unbound local tickets emit "create" mutations;
bound tickets whose fields diverge from Jira emit "update" mutations with
only the changed fields.

This module is predominantly pure, with one controlled I/O seam: when the
caller passes a ``client`` argument to :func:`compute_outbound_mutations`, the
differ may call ``client.get_comments(jira_key)`` for bound tickets whose
snapshot entry lacks a ``comment`` field (the live Jira search shape — Jira
search does NOT return comment data). All other code paths remain pure.

Dependency: BindingStore interface (PR #401). This module codes against the
interface — get_jira_key(local_id) -> str|None, is_bound(local_id) -> bool —
and does not import the concrete class.
"""

from __future__ import annotations

import urllib.error
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from rebar_reconciler._loader import lazy_load

# Ticket 625b: the outbound UPDATE field diff compares in CANONICAL (local) shape via
# this vendor-neutral core helper (the snapshot is canonicalized by the injected
# InboundMapper, diffed locally, then mapped back at the boundary), so this differ
# imports NOTHING from ``adapters.jira`` and names no raw Jira snapshot key. Now used
# only by the extracted outbound_mutation_builders module; re-exported here so
# outbound_differ.compute_update_fields keeps resolving for existing tests.
from rebar_reconciler.outbound_field_diff import compute_update_fields  # noqa: F401

if TYPE_CHECKING:
    from rebar_reconciler._backend import InboundMapper, OutboundMapper

    from ._backend import TicketTransport

# The identity-mapping assignee-resolution cluster (264f) lives in
# outbound_assignee.py (split for module size; a leaf that imports rebar core
# lazily inside its functions). All five symbols are re-exported so
# outbound_differ.<name> keeps resolving for callers and the identity test suite
# (test_identity_264f_resolve.py pins _bootstrap_account_id_via_user_search).
from rebar_reconciler.get_rotation import last_get_pass as _last_get_pass
from rebar_reconciler.outbound_assignee import (  # noqa: F401
    _USER_SEARCH_METHODS,
    _bootstrap_account_id_via_user_search,
    _identity_email,
    _identity_jira_account_id,
    _resolve_assignee_account_id,
)

# The comment-diff cluster lives in outbound_comments.py (split for module size;
# the comment seam is self-contained and imports one-way). _diff_comments +
# _map_comments_for_create are called by compute_outbound_mutations below;
# _normalize_comment_body + RECONCILER_MARKER are re-exported so
# outbound_differ.<name> keeps resolving for the comment-diff test suite.
from rebar_reconciler.outbound_comments import (  # noqa: F401
    RECONCILER_MARKER,
    _decorate_outbound_comment,
    _diff_comments,
    _map_comments_for_create,
    _normalize_comment_body,
)

# The label-diff cluster lives in outbound_labels.py (split for module size; the same
# seam-extraction the comment/link/assignee clusters above already went through). It is
# self-contained — nothing else in this module feeds it. Now consumed only by the
# extracted outbound_mutation_builders module; all four names are re-exported so
# outbound_differ.<name> keeps resolving for existing callers and tests.
from rebar_reconciler.outbound_labels import (  # noqa: F401
    _EXCLUDED_PREFIXES,
    _diff_labels,
    _diff_status_annotation_labels,
)

# The link-diff cluster lives in outbound_links.py (split for module size); ticket
# eefd made it compare in canonical shape via an injected SupportsLinks capability.
# Re-exported so outbound_differ._diff_links keeps resolving for existing tests.
from rebar_reconciler.outbound_links import _diff_links  # noqa: F401

# ---------------------------------------------------------------------------
# Bug 1e08-1a35-0267-4ca6 — bound-but-absent direct-GET sentinels / config
# ---------------------------------------------------------------------------
# A bound local ticket whose Jira key is ABSENT from this pass's search
# snapshot (deleted, or status=Done beyond the fetcher's _DONE_RECENT_CAP
# window) used to diff every field against "" and re-emit every pass. The fix
# replaces ``jira_snapshot.get(jira_key, {})`` with a membership discriminator
# plus a bounded direct GET for the absent case. These module-level singleton
# objects are identity-compared (``is``) so they can never collide with a real
# ``fields`` dict.
_DELETED = object()  # _safe_get_issue: HTTPError 404 (issue gone)
_TRANSPORT_ERROR = object()  # _safe_get_issue: non-404 HTTPError / URLError / timeout

# Per-pass bounded GET budget (K) and consecutive-404 retirement grace. Env
# vars because the reconciler has no dotted-config reader (matches fetcher.py /
# applier.py). Parsed defensively at use-site so a typo'd ops value degrades to
# the default rather than aborting the pass.
_DEFAULT_ABSENT_GET_BUDGET = 20


def _rest_issue_to_snapshot_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """Return the raw ``fields`` block of a REST GET payload (NO normalization).

    The fetcher stores each snapshot entry as a verbatim copy of the issue's
    ``fields`` (``fetcher.py``); ALL normalization happens downstream (now the
    injected InboundMapper). A deliberate one-liner kept for the C2 parity test.
    """
    return issue.get("fields", {})


def _safe_get_issue(client: TicketTransport, jira_key: str) -> Any:
    """Direct GET a single Jira issue's raw fields, classifying failures.

    Returns:
        - the raw ``fields`` dict on HTTP 200,
        - the ``_DELETED`` sentinel on HTTPError 404 (issue gone),
        - the ``_TRANSPORT_ERROR`` sentinel on any non-404 HTTPError, URLError,
          timeout, or OSError (transient — caller emits nothing and defers).

    ``get_issue_by_rest`` re-raises ``HTTPError`` without retry, so a 404 from
    a deleted issue surfaces here as a raised ``HTTPError`` (not a return).
    ``HTTPError`` is a subclass of ``URLError``, so it MUST be caught first.
    """
    try:
        return client.get_issue_by_rest(jira_key).get("fields", {})
    except urllib.error.HTTPError as exc:
        return _DELETED if exc.code == 404 else _TRANSPORT_ERROR
    except (urllib.error.URLError, TimeoutError, OSError):
        return _TRANSPORT_ERROR


def _is_retired(binding_store: Any, jira_key: str) -> bool:
    """``binding_store.is_retired`` with graceful fallback for legacy stubs."""
    return bool(_best_effort(binding_store, "is_retired", jira_key))


def _best_effort(binding_store: Any, member: str, *args: Any) -> Any:
    """Call a binding-store member; ``None`` when the store lacks it or it raises.

    One helper for the bookkeeping calls this module makes (``set_last_get`` /
    ``note_absent_or_rekey`` / ``clear_absent`` / ``note_create_suppressed``) and for
    the read-only store queries (``is_retired`` / ``retired_key_for_local``), which
    carried byte-identical getattr-guarded, exception-swallowing bodies. Bookkeeping
    must never fail a pass, and a duck-typed store may implement only some members.

    Collapsed under bug 7c26 rather than for tidiness: this module sits at the LOCKED
    module-size cap, so wiring the move-aware absence path had to buy its lines back
    from real duplication instead of from comments. Bug 3b5f bought the tombstone
    guard's lines the same way, by returning the member's value instead of discarding
    it (the void call sites are unaffected — they ignore the return).
    """
    fn = getattr(binding_store, member, None)
    if fn is None:
        return None
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — fail-open: a legacy/duck-typed store never fails a pass
        return None


_CONFIG_KEY = "rebar_reconciler.config"
_ConfigModule = None


def _load_config():
    """Lazy-load the sibling config module (same lazy-by-path loader pattern).

    Loaded by file path (not ``from . import``) because the differ may be
    imported via ``importlib.util.spec_from_file_location`` in tests, which does
    not establish package context. Provides ``EXCLUDED_SYNC_TYPES`` (the local
    ticket types — e.g. ``session_log`` — that are never synced to Jira).
    """
    global _ConfigModule
    if _ConfigModule is None:
        _ConfigModule = lazy_load(_CONFIG_KEY, "config.py")
    return _ConfigModule


# ---------------------------------------------------------------------------
# BindingStore protocol — codes against PR #401's interface
# ---------------------------------------------------------------------------


@runtime_checkable
class BindingStoreProtocol(Protocol):
    """Minimal interface for the binding store (PR #401)."""

    def get_jira_key(self, local_id: str) -> str | None: ...
    def is_bound(self, local_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# OutboundMutation dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboundMutation:
    """A single outbound change to push to Jira."""

    local_id: str
    jira_key: str | None  # None for create (not yet assigned)
    action: str  # "create" | "update" | "delete"
    fields: dict[str, Any]  # changed fields only for update; all fields for create
    comments: list[dict[str, Any]] = dataclass_field(default_factory=list)
    labels: list[dict[str, Any]] = dataclass_field(default_factory=list)
    links: list[dict[str, Any]] = dataclass_field(default_factory=list)


@dataclass
class OutboundDiffConfig:
    """Optional inputs to :func:`compute_outbound_mutations`.

    Collapses what used to be five trailing optional parameters into one object
    (the 9-positional-param smell). Every field is optional; the orchestrator
    substitutes the documented defaults for any left unset.

    Fields:
        excluded_statuses: Local statuses to skip (defaults to
            ``{"archived", "deleted"}`` when None).
        local_label_intent: ``local_id -> "ever-seen" tag set`` (bug a06c) gating
            outbound label REMOVE emission. None retains the pre-fix behaviour.
        client: Optional AcliClient used for live comment fetch + the bounded
            bound-but-absent direct GETs. None disables both (the fixture path).
        pass_id: This pass's monotonic id; the rotation bookkeeping key for the
            bound-but-absent direct GETs (bug 1e08).
        prev_snapshot: The previous pass's Jira snapshot, consulted by the inbound
            directionality guard (suppress an outbound field-update when it is a
            Jira-side edit local has not touched since the last sync).
    """

    excluded_statuses: set[str] | None = None
    local_label_intent: dict[str, set[str]] | None = None
    client: Any = None
    pass_id: str = ""
    prev_snapshot: dict[str, Any] | None = None
    # Observability sinks (bugs a713/acd0). When provided, _diff_fields appends
    # (jira_key, field) tuples: conflict_sink for a both-sides field conflict,
    # dropped_field_sink for a mapped-but-allowlist-excluded field that differs. The
    # orchestrator (run_differs) emits deduped bridge alerts from them post-pass.
    conflict_sink: list[tuple[str, str]] | None = None
    dropped_field_sink: list[tuple[str, str]] | None = None
    # Story d19d (many-to-many outbound). The store's projects ``Mapping``. When set
    # AND non-empty, the create path resolves each ticket's target project and stamps
    # it into the create mutation's fields under ``_BRIDGE_TARGET_PROJECT_KEY``; a
    # ticket resolving to a project OUTSIDE the mapping, or to "not synced", emits no
    # create (creates are guard-exempt, so this is the only place that gap closes).
    # None / an empty mapping preserves the legacy single-project behaviour.
    projects_mapping: Any = None
    # Story S2. The store root the reconcile pass runs against. Threaded into
    # ``_effective_status_map_for`` so the per-project ``[mapping]`` status overlay is
    # discovered from the store root (like the fetcher/preflight), never the process CWD.
    # None → ``effective_status_map`` falls back to CWD discovery (the built-in map when
    # no config is found), preserving legacy behaviour.
    repo_root: Any = None


# The reserved create-payload key carrying the resolved target project from the
# differ to whichever transport runs (story d19d). It is NOT a Jira field name:
# Cloud's ``create_issue`` extracts only the fields it names, and the Data Center
# transport DROPS it in ``_translate_create_fields`` before splatting the field
# dict, so it never reaches the tracker.
_BRIDGE_TARGET_PROJECT_KEY = "_bridge_target_project"


def _effective_status_map_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any = None
) -> dict[str, str] | None:
    """The effective per-project local->Jira status map for ``ticket``, or ``None``.

    Story S2: resolve the ticket's project (``projects_store.resolve_project``) and return
    ``config.effective_status_map(project_key, root=repo_root)`` so both create and update
    map status through the project's overlay (map-or-drift). ``repo_root`` is the store root
    the pass runs against; ``[mapping]`` MUST be read from it, never the CWD, consistent with
    the fetcher and preflight. ``None`` (the built-in fallback the mappers already apply) when
    no project key is obtainable or there is no ``[mapping]`` block."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar_reconciler import config, projects_store

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return None
    return config.effective_status_map(project_key, root=repo_root)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def compute_outbound_mutations(
    local_tickets: list[dict[str, Any]],
    jira_snapshot: dict[str, Any],
    binding_store: BindingStoreProtocol,
    config: OutboundDiffConfig | None = None,
    *,
    outbound_mapper: OutboundMapper | None = None,
    inbound_mapper: InboundMapper | None = None,
    links: Any | None = None,
) -> tuple[list[OutboundMutation], dict[str, dict[str, Any]]]:
    """Diff local tickets against Jira snapshot and return outbound mutations.

    Args:
        local_tickets: List of local ticket dicts. Each has: ticket_id, title,
            description, status, priority, ticket_type, assignee, tags, comments,
            deps.
        jira_snapshot: Dict of {jira_key: {fields...}} from the fetcher.
        binding_store: A BindingStore instance providing get_jira_key(local_id),
            is_bound(local_id).
        config: Optional :class:`OutboundDiffConfig` carrying the five optional
            inputs (excluded_statuses, local_label_intent, client, pass_id,
            prev_snapshot). None → all defaults (see OutboundDiffConfig). The
            former trailing ``absent_alive_fields`` out-param is GONE — its
            value is the second element of the return tuple instead.
        outbound_mapper: The injected Backend-port ``OutboundMapper`` (ticket 4af8);
            ``None`` resolves the configured backend's mapper via ``select_backend``.
        links: The injected ``SupportsLinks`` capability (ticket eefd); ``None``
            resolves the configured backend (a backend IS-A ``SupportsLinks``).

    Returns:
        A ``(mutations, absent_alive_fields)`` tuple:
          * ``mutations``: the OutboundMutation objects to push to Jira.
          * ``absent_alive_fields``: ``{jira_key: <raw fields dict>}`` for each
            bound-but-absent key the bounded direct GET resolved as ALIVE
            (HTTP 200) this pass — the inbound-direction GET-sharing seam (bug
            0702-3b6d-c1db-4ed3): the reconcile orchestrator merges these into the
            snapshot it hands to the inbound differ, so each out-of-window-alive
            key is GET'd exactly ONCE per pass and BOTH directions consume the
            result. 404/deleted and transport-error keys are deliberately NOT
            recorded (a gone issue must not be inbound-mirrored; retirement stays
            owned by the outbound 404-counter). Empty when nothing was resolved.
    """
    if config is None:
        config = OutboundDiffConfig()
    # Tickets 4af8/625b/eefd: the local->remote mapper, remote->local mapper, and
    # links capability are each injected via the Backend port (run_differs passes
    # ``backend.outbound``/``backend.inbound``/``backend``). A direct caller that
    # omits any of them resolves it through the neutral registry seam instead —
    # naming no vendor symbol here.
    if outbound_mapper is None or inbound_mapper is None or links is None:
        from rebar.config import compose_config
        from rebar_reconciler._backend_registry import select_backend

        _backend = select_backend(compose_config())
        if outbound_mapper is None:
            outbound_mapper = _backend.outbound
        if inbound_mapper is None:
            inbound_mapper = _backend.inbound
        if links is None:
            links = _backend
    # Bind the config's fields to locals so the diff body below reads unchanged.
    excluded_statuses = config.excluded_statuses
    local_label_intent = config.local_label_intent
    client = config.client
    pass_id = config.pass_id
    prev_snapshot = config.prev_snapshot
    conflict_sink = config.conflict_sink
    dropped_field_sink = config.dropped_field_sink
    # The bound-but-absent ALIVE-GET sharing seam: populated below, returned to
    # the caller (replaces the former mutable out-param).
    absent_alive_fields: dict[str, dict[str, Any]] = {}

    if excluded_statuses is None:
        excluded_statuses = {"archived", "deleted"}

    # Local ticket types that never sync to Jira (e.g. session_log) — verbose,
    # local, agent-facing artifacts with no Jira counterpart. Skipped in both the
    # absent-GET pre-selection and the main mutation loop, alongside the
    # excluded-status check.
    excluded_sync_types: frozenset[str] = _load_config().EXCLUDED_SYNC_TYPES

    mutations: list[OutboundMutation] = []

    _selected_for_get_this_pass = _compute_outbound_select_absent_gets(
        local_tickets, jira_snapshot, binding_store, excluded_statuses, excluded_sync_types, client
    )

    # Hierarchy pre-check map (ticket 8b25): {local_id → ticket_type}. Used to
    # suppress parent diffs whose resolved parent is a non-epic — Jira only
    # permits Epic parents on this project, so emitting such a parent mutation
    # would re-fail (HTTP 400) every pass. Cheap O(n) build over local state.
    local_ticket_types: dict[str, str] = {
        t["ticket_id"]: t.get("ticket_type", "") for t in local_tickets if t.get("ticket_id")
    }

    # Assignee resolution cache (bug 9b94). A local assignee that maps to NO
    # assignable Jira user means "desired = unassigned": the differ must stop
    # re-emitting an assignee update once Jira is unassigned, instead of churning
    # forever on an unmappable agent identity (e.g. "claude"). Resolution is via
    # the client's user search, cached per pass by assignee string (a handful of
    # distinct assignees → a handful of lookups). With no client (unit/fixture
    # path) resolution is non-authoritative and the differ falls back to the
    # permissive string match.
    # 3-tuple (264f): (accountId|None, authoritative, is_account_id). The cache
    # stores 3-tuples so both the identity fast-path and the legacy string-match
    # path unpack identically at every call site.
    _assignee_cache: dict[str, tuple[str | None, bool, bool]] = {}

    def _assignee_resolver(assignee: str, jira_key: str) -> tuple[str | None, bool, bool]:
        """Resolve a local assignee to a Jira accountId (264f).

        Returns ``(account_id_or_None, authoritative, is_account_id)``.
        ``authoritative`` is ``True`` when the result is trustworthy: an empty
        local assignee (→ unassigned), a resolved accountId, or a definitive "no
        assignable user" (→ ``None`` = unassigned); ``False`` when the mapping is
        unknown (no client, or a transient lookup error) — the caller then
        preserves the legacy string-match behavior. ``is_account_id`` is ``True``
        only when the value is an already-resolved accountId (identity fast path or
        the ``/user/search`` bootstrap) so acli skips the assignable search.
        """
        if not assignee:
            return ("", True, False)
        if assignee in _assignee_cache:
            return _assignee_cache[assignee]
        result = _resolve_assignee_account_id(assignee, jira_key, client)
        _assignee_cache[assignee] = result
        return result

    for ticket in local_tickets:
        status = ticket.get("status", "")
        if status in excluded_statuses:
            continue
        if ticket.get("ticket_type", "") in excluded_sync_types:
            continue

        local_id = ticket["ticket_id"]
        jira_key = binding_store.get_jira_key(local_id)

        # Lazy (function-local) import to break the import cycle: the builders
        # module imports shared helpers/sentinels/dataclass from this module at
        # module load, so this module must NOT import it at load time.
        from rebar_reconciler.outbound_mutation_builders import (
            _compute_outbound_create_mutation,
            _compute_outbound_update_mutation,
        )

        if jira_key is None:
            _compute_outbound_create_mutation(
                mutations,
                ticket,
                status,
                local_id,
                binding_store,
                local_ticket_types,
                outbound_mapper,
                dropped_field_sink=dropped_field_sink,
                mapping=config.projects_mapping,
                repo_root=config.repo_root,
            )
        else:
            _compute_outbound_update_mutation(
                mutations,
                ticket,
                status,
                local_id,
                jira_key,
                jira_snapshot,
                binding_store,
                client,
                pass_id,
                _selected_for_get_this_pass,
                prev_snapshot,
                local_label_intent,
                local_ticket_types,
                _assignee_resolver,
                absent_alive_fields,
                outbound_mapper,
                inbound_mapper,
                links,
                conflict_sink=conflict_sink,
                dropped_field_sink=dropped_field_sink,
                mapping=config.projects_mapping,
                repo_root=config.repo_root,
            )

    return mutations, absent_alive_fields


def _compute_outbound_select_absent_gets(
    local_tickets,
    jira_snapshot,
    binding_store,
    excluded_statuses,
    excluded_sync_types,
    client: TicketTransport,
) -> set[str]:
    """Phase: rotation pre-selection of bound-but-absent keys eligible for a direct
    GET this pass (bug 1e08). Returns the K least-recently-GET'd selected keys."""
    # Bug 1e08 — rotation pre-selection for bound-but-absent direct GETs.
    # Compute the set of jira_keys eligible for a GET this pass: bound,
    # non-pending, non-retired, and ABSENT from this pass's search snapshot.
    # Select the K least-recently-GET'd (sorted by last_get_pass ascending; the
    # "" never-GET'd sentinel sorts first), bounding servicing of every absent
    # key to <= ceil(N/K) passes (anti-starvation, I3/I4).
    # Deletion-probe budget (GET probes to confirm a Jira issue is really deleted),
    # resolved through the typed config: [tool.rebar.reconciler].deletion_probe_limit
    # (default 20), overridden by env REBAR_RECONCILER_DELETION_PROBE_LIMIT (deprecated
    # alias RECONCILER_ABSENT_GET_BUDGET), then `rebar -c reconciler.deletion_probe_limit=…`.
    # An unreadable config falls back to the default rather than failing the pass.
    from rebar.config import ConfigError, compose_config

    try:
        _budget = compose_config().reconciler.deletion_probe_limit
    except ConfigError:
        _budget = _DEFAULT_ABSENT_GET_BUDGET
    _absent_candidates: list[str] = []
    _seen_absent: set[str] = set()
    # Without a client we cannot direct-GET, so there is nothing to select.
    for _t in local_tickets if client is not None else ():
        if _t.get("status", "") in excluded_statuses:
            continue
        if _t.get("ticket_type", "") in excluded_sync_types:
            continue
        _lid = _t.get("ticket_id")
        if not _lid:
            continue
        _jk = binding_store.get_jira_key(_lid)
        if _jk is None or _jk in jira_snapshot or _jk in _seen_absent:
            continue
        if _is_retired(binding_store, _jk):
            continue
        _seen_absent.add(_jk)
        _absent_candidates.append(_jk)
    _absent_candidates.sort(key=lambda k: _last_get_pass(binding_store, k))
    _selected_for_get_this_pass: set[str] = set(_absent_candidates[:_budget])
    return _selected_for_get_this_pass
