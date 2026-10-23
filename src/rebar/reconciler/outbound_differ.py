"""Diff local tickets against the Jira snapshot to build outbound mutations.

Unbound tickets create issues; bound tickets update changed fields. The binding
store maps local IDs to Jira keys. With a client, the differ can fetch missing
comments and probe bound issues absent from the search snapshot. It accepts the
binding-store interface without importing its concrete class.
"""

from __future__ import annotations

import importlib
import urllib.error
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

# Compare updates in canonical local shape: the injected inbound mapper converts
# the snapshot before comparison. Re-export for existing callers and tests.
from rebar.reconciler.outbound_field_diff import compute_update_fields  # noqa: F401

if TYPE_CHECKING:
    from rebar.reconciler._backend import InboundMapper, OutboundMapper

    from ._backend import TicketTransport

# Re-export assignee resolution helpers for existing callers and identity tests.
from rebar.reconciler.get_rotation import last_get_pass as _last_get_pass
from rebar.reconciler.outbound_assignee import (  # noqa: F401
    _USER_SEARCH_METHODS,
    _bootstrap_account_id_via_user_search,
    _identity_email,
    _identity_jira_account_id,
    _resolve_assignee_account_id,
)

# Re-export comment diff helpers for existing callers and tests.
from rebar.reconciler.outbound_comments import (  # noqa: F401
    RECONCILER_MARKER,
    _decorate_outbound_comment,
    _diff_comments,
    _map_comments_for_create,
    _normalize_comment_body,
)

# Re-export per-project effective-map resolvers for existing callers and tests.
from rebar.reconciler.outbound_effective_maps import (  # noqa: F401
    _effective_create_defaults_for,
    _effective_excluded_sync_types_for,
    _effective_link_map_for,
    _effective_priority_map_for,
    _effective_status_map_for,
    _effective_type_map_for,
    _memoize_effective,
)

# Re-export label diff helpers for existing callers and tests.
from rebar.reconciler.outbound_labels import (  # noqa: F401
    _EXCLUDED_PREFIXES,
    _diff_labels,
    _diff_status_annotation_labels,
    _diff_type_annotation_labels,
)

# The link-diff cluster lives in outbound_links.py (split for module size); ticket
# eefd made it compare in canonical shape via an injected SupportsLinks capability.
# Re-exported so outbound_differ._diff_links keeps resolving for existing tests.
from rebar.reconciler.outbound_links import _diff_links  # noqa: F401

# ---------------------------------------------------------------------------
# Bug 1e08-1a35-0267-4ca6 — bound-but-absent direct-GET sentinels / config
# ---------------------------------------------------------------------------
# Bound issues absent from the search snapshot get a bounded direct GET instead
# of a diff against empty fields. Identity sentinels cannot equal real fields.
_DELETED = object()  # _safe_get_issue: HTTPError 404 (issue gone)
_TRANSPORT_ERROR = object()  # _safe_get_issue: non-404 HTTPError / URLError / timeout

# Default per-pass direct-GET budget; config may override it.
_DEFAULT_ABSENT_GET_BUDGET = 20


def _rest_issue_to_snapshot_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """Return raw REST ``fields``; the inbound mapper normalizes them later."""
    return issue.get("fields", {})


def _safe_get_issue(client: TicketTransport, jira_key: str) -> Any:
    """GET raw issue fields; classify 404 as deleted and other I/O errors as transient.

    Catch ``HTTPError`` before its ``URLError`` base so 404 gets its own sentinel.
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
    """Call an optional binding-store member, returning ``None`` if absent or failing.

    Bookkeeping failures do not fail the pass; read-only queries return their value.
    """
    fn = getattr(binding_store, member, None)
    if fn is None:
        return None
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — fail-open: a legacy/duck-typed store never fails a pass
        return None


_CONFIG_KEY = "rebar.reconciler.config"
_ConfigModule = None


def _load_config():
    """Load reconciler config on first use for ``EXCLUDED_SYNC_TYPES``."""
    global _ConfigModule
    if _ConfigModule is None:
        _ConfigModule = importlib.import_module("rebar.reconciler.config")
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
    requires_create: tuple[str, ...] = ()


@dataclass
class OutboundDiffConfig:
    """Caller inputs and derived state shared by outbound builders for one pass.

    Caller inputs are ``excluded_statuses``, ``local_label_intent``, ``client``,
    ``pass_id``, ``prev_snapshot``, ``conflict_sink``, ``dropped_field_sink``,
    ``projects_mapping``, and ``repo_root``. Direct `mapping=` / `repo_root=`
    keyword arguments override their config values. The remaining fields are
    populated during the pass.

    ``excluded_statuses`` defaults to archived/deleted. ``local_label_intent``
    gates label removals; ``client`` enables live comments and absent-issue GETs;
    ``pass_id`` keys GET rotation; ``prev_snapshot`` guards Jira-side edits.
    ``conflict_sink`` and ``dropped_field_sink`` collect field diagnostics.
    ``projects_mapping`` and ``repo_root`` resolve project overlays.

    Derived fields hold the links capability, effective-map cache, selected GET
    keys, alive GET fields, Jira snapshot, local parents, and local ticket types.
    Per ADR 0107, narrow backend collaborators stay explicit builder arguments.
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
    # A nonempty projects mapping selects each create's target; tickets outside it
    # emit no create. None or empty preserves single-project behavior.
    projects_mapping: Any = None
    # Store root for per-project mapping discovery; None retains CWD fallback.
    repo_root: Any = None
    # --- Ticket 6452: additional pass-scoped fields, populated internally by
    # compute_outbound_mutations before its per-ticket loop (never caller-supplied
    # like the fields above; kept on this same type per ADR 0107's disposition). ---
    links: Any = None
    effective_cache: dict[tuple[str, str], Any] | None = None
    selected_for_get: Any = None
    absent_alive_fields: dict[str, dict[str, Any]] | None = None
    jira_snapshot: dict[str, Any] | None = None
    local_parents: dict[str, Any] | None = None
    local_ticket_types: dict[str, str] | None = None


# Create-payload target-project key; transports consume it before sending fields.
_BRIDGE_TARGET_PROJECT_KEY = "_bridge_target_project"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _assert_type_decisions_for_pass(
    local_tickets: list[dict[str, Any]], projects_mapping: Any, repo_root: Any
) -> None:
    """Require a type decision for each target project before building mutations.

    Tickets without a target project are skipped. Without project mapping, the
    built-in decisions apply and this check does nothing.
    """
    if projects_mapping is None or not getattr(projects_mapping, "projects", None):
        return
    from rebar.reconciler import config as _cfg
    from rebar.reconciler import projects_store as _projects_store

    decided_project_keys: set[str] = set()
    for gate_ticket in local_tickets:
        gate_key = _projects_store.resolve_project(gate_ticket, projects_mapping)
        if gate_key and gate_key not in decided_project_keys:
            decided_project_keys.add(gate_key)
            _cfg.assert_type_decisions_complete(gate_key, root=repo_root)


def _apply_pass_overrides(config: OutboundDiffConfig, mapping: Any, repo_root: Any) -> None:
    """Apply explicit mapping/root overrides; ``None`` keeps config values."""
    if mapping is not None:
        config.projects_mapping = mapping
    if repo_root is not None:
        config.repo_root = repo_root


def compute_outbound_mutations(
    local_tickets: list[dict[str, Any]],
    jira_snapshot: dict[str, Any],
    binding_store: BindingStoreProtocol | None = None,
    config: OutboundDiffConfig | None = None,
    *,
    outbound_mapper: OutboundMapper | None = None,
    inbound_mapper: InboundMapper | None = None,
    links: Any | None = None,
    mapping: Any | None = None,
    repo_root: Any | None = None,
) -> tuple[list[OutboundMutation], dict[str, dict[str, Any]]]:
    """Return outbound mutations and raw fields from alive, absent-issue GETs.

    ``config`` holds optional pass inputs; direct ``mapping`` and ``repo_root``
    override its values. Missing backend collaborators resolve from the configured
    backend. The second return value contains only HTTP 200 fields from bounded
    GETs of bound issues absent from the snapshot. The caller shares those fields
    with the inbound differ; deleted issues and transport errors are excluded.
    """
    if config is None:
        config = OutboundDiffConfig()
    _apply_pass_overrides(config, mapping, repo_root)
    # Resolve omitted mapper/link capabilities from the configured backend.
    if outbound_mapper is None or inbound_mapper is None or links is None:
        from rebar.config import compose_config
        from rebar.reconciler._backend_registry import select_backend

        _backend = select_backend(compose_config())
        if outbound_mapper is None:
            outbound_mapper = _backend.outbound
        if inbound_mapper is None:
            inbound_mapper = _backend.inbound
        if links is None:
            links = _backend
    # Bind the config's fields to locals so the diff body below reads unchanged.
    excluded_statuses = config.excluded_statuses
    client = config.client
    # The bound-but-absent ALIVE-GET sharing seam: populated below, returned to
    # the caller (replaces the former mutable out-param).
    absent_alive_fields: dict[str, dict[str, Any]] = {}

    if excluded_statuses is None:
        excluded_statuses = {"archived", "deleted"}

    # Exclude built-in local-only types and types mapped to SKIP per project in
    # both absent-GET selection and the mutation loop.
    excluded_sync_types: frozenset[str] = _load_config().EXCLUDED_SYNC_TYPES

    # S3 fail-closed gate — see :func:`_assert_type_decisions_for_pass`. Run ONCE up
    # front, after config load and before the absent-GET pre-selection and the per-ticket
    # mutation loop, so an undecided type raises before any mutation is built.
    _assert_type_decisions_for_pass(local_tickets, config.projects_mapping, config.repo_root)

    mutations: list[OutboundMutation] = []

    # Cache effective maps once per axis/project for this pass; refresh next pass.
    _effective_cache: dict[tuple[str, str], Any] = {}

    _selected_for_get_this_pass = _compute_outbound_select_absent_gets(
        local_tickets,
        jira_snapshot,
        binding_store,
        excluded_statuses,
        excluded_sync_types,
        client,
        mapping=config.projects_mapping,
        repo_root=config.repo_root,
        effective_cache=_effective_cache,
    )

    # Suppress parent diffs for non-epic parents, which Jira rejects.
    local_ticket_types: dict[str, str] = {
        t["ticket_id"]: t.get("ticket_type", "") for t in local_tickets if t.get("ticket_id")
    }

    # S7: {local_id → parent_id} companion map, used by the outbound comment differ
    # to walk upward to the nearest ancestor bound in Jira when a non-epic parent's
    # field was suppressed (parent-drift breadcrumb).
    local_parents: dict[str, Any] = {
        t["ticket_id"]: t.get("parent_id") for t in local_tickets if t.get("ticket_id")
    }

    # Cache assignee resolution per pass. No assignable user means unassigned;
    # without a client, fall back to permissive string matching. Tuple fields are
    # (accountId|None, authoritative, is_account_id).
    _assignee_cache: dict[str, tuple[str | None, bool, bool]] = {}

    def _assignee_resolver(assignee: str, jira_key: str) -> tuple[str | None, bool, bool]:
        """Return (accountId|None, authoritative, is_account_id) for an assignee.

        Empty or definitively unassignable names are authoritative. Missing
        client or transient lookup failure keeps legacy string matching.
        ``is_account_id`` skips a second assignable-user search.
        """
        if not assignee:
            return ("", True, False)
        if assignee in _assignee_cache:
            return _assignee_cache[assignee]
        result = _resolve_assignee_account_id(assignee, jira_key, client)
        _assignee_cache[assignee] = result
        return result

    # Share pass-derived values with both builders through the existing config.
    config.links = links
    config.effective_cache = _effective_cache
    config.selected_for_get = _selected_for_get_this_pass
    config.absent_alive_fields = absent_alive_fields
    config.jira_snapshot = jira_snapshot
    config.local_parents = local_parents
    config.local_ticket_types = local_ticket_types

    for ticket in local_tickets:
        status = ticket.get("status", "")
        if status in excluded_statuses:
            continue
        # S3: the per-project effective excluded set (built-in base UNION any type this
        # project maps to SKIP), so a type-granular skip drops the ticket here too.
        _ticket_excluded_types = _effective_excluded_sync_types_for(
            ticket,
            config.projects_mapping,
            config.repo_root,
            builtin=excluded_sync_types,
            cache=_effective_cache,
        )
        if ticket.get("ticket_type", "") in _ticket_excluded_types:
            continue

        local_id = ticket["ticket_id"]
        # A syncable ticket (survived the exclusion guards above) requires a binding
        # store; the optional signature only supports callers whose tickets are all
        # excluded (e.g. a type-granular SKIP pass) and so never reach this point.
        assert binding_store is not None
        jira_key = binding_store.get_jira_key(local_id)

        # Lazy (function-local) import to break the import cycle: the builders
        # module imports shared helpers/sentinels/dataclass from this module at
        # module load, so this module must NOT import it at load time.
        from rebar.reconciler.outbound_mutation_builders import (
            _compute_outbound_create_mutation,
            _compute_outbound_update_mutation,
        )

        if jira_key is None:
            _compute_outbound_create_mutation(
                mutations,
                ticket,
                status,
                local_id,
                config,
                binding_store,
                outbound_mapper,
            )
        else:
            _compute_outbound_update_mutation(
                mutations,
                ticket,
                status,
                local_id,
                jira_key,
                config,
                binding_store,
                outbound_mapper,
                inbound_mapper,
                _assignee_resolver,
            )

    return mutations, absent_alive_fields


def _compute_outbound_select_absent_gets(
    local_tickets,
    jira_snapshot,
    binding_store,
    excluded_statuses,
    excluded_sync_types,
    client: TicketTransport,
    mapping: Any = None,
    repo_root: Any = None,
    effective_cache: Any = None,
) -> set[str]:
    """Select least-recently-GET'd bound keys absent from the search snapshot.

    Apply the same per-project type exclusions as the mutation loop. Without a
    mapping, use the pass-level excluded type set.
    """
    # Rotate through eligible keys, oldest GET first, within the configured
    # deletion-probe budget. Unreadable config uses the default budget.
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
        _t_excluded_types = _effective_excluded_sync_types_for(
            _t, mapping, repo_root, builtin=excluded_sync_types, cache=effective_cache
        )
        if _t.get("ticket_type", "") in _t_excluded_types:
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
