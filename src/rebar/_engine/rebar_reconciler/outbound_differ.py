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

import os
import sys
import urllib.error
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Protocol, runtime_checkable

from rebar_reconciler._loader import lazy_load

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

# The field-diff cluster lives in outbound_fields.py (split for module size).
# _map_local_to_jira_fields + _diff_fields are called by compute_outbound_mutations
# below; _assignee_matches + _LOCAL_TO_JIRA_TYPE are re-exported so
# outbound_differ.<name> keeps resolving for the field-diff test suite.
from rebar_reconciler.outbound_fields import (  # noqa: F401
    _LOCAL_TO_JIRA_TYPE,
    _assignee_matches,
    _diff_fields,
    _extract_jira_field,
    _map_local_to_jira_fields,
)

# The link-diff cluster lives in outbound_links.py (split for module size).
# _diff_links is called by compute_outbound_mutations below; _existing_jira_links
# is re-exported so outbound_differ.<name> keeps resolving for the link-diff tests.
from rebar_reconciler.outbound_links import (  # noqa: F401
    _diff_links,
    _existing_jira_links,
)


def _rebar_env(name: str, default: str | None = None) -> str | None:
    """Read ``REBAR_<name>`` from the environment."""
    return os.environ.get(f"REBAR_{name}", default)


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


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an int env var defensively: malformed → default; clamp >= minimum."""
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except (ValueError, TypeError):
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _rest_issue_to_snapshot_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """Return the raw ``fields`` block of a REST GET payload (NO normalization).

    The fetcher stores each snapshot entry as a verbatim copy of the issue's
    ``fields`` (``fetcher.py``), and ALL normalization happens downstream in
    ``_diff_fields`` / ``_extract_jira_field``. Re-normalizing here would
    double-normalize and reintroduce phantom re-emits — so this helper is a
    deliberate one-liner kept only so the C2 parity test has a real symbol.
    """
    return issue.get("fields", {})


def _safe_get_issue(client: Any, jira_key: str) -> Any:
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
    fn = getattr(binding_store, "is_retired", None)
    if fn is None:
        return False
    try:
        return bool(fn(jira_key))
    except Exception:  # noqa: BLE001 — fail-open: legacy-stub fallback returns False
        return False


def _last_get_pass(binding_store: Any, jira_key: str) -> str:
    """``binding_store.last_get_pass`` with fallback to the "" sentinel."""
    fn = getattr(binding_store, "last_get_pass", None)
    if fn is None:
        return ""
    try:
        return fn(jira_key) or ""
    except Exception:  # noqa: BLE001 — fail-open: legacy-stub fallback returns "" sentinel
        return ""


def _set_last_get(binding_store: Any, jira_key: str, pass_id: str) -> None:
    """``binding_store.set_last_get`` no-op when the store predates the method."""
    fn = getattr(binding_store, "set_last_get", None)
    if fn is not None:
        try:
            fn(jira_key, pass_id)
        except Exception:  # noqa: BLE001 — fail-open: best-effort set_last_get no-op on failure
            pass


def _note_absent(binding_store: Any, jira_key: str) -> None:
    """``binding_store.note_absent`` no-op when the store predates the method."""
    fn = getattr(binding_store, "note_absent", None)
    if fn is not None:
        try:
            fn(jira_key)
        except Exception:  # noqa: BLE001 — fail-open: best-effort note_absent no-op on failure
            pass


def _clear_absent(binding_store: Any, jira_key: str) -> None:
    """``binding_store.clear_absent`` no-op when the store predates the method."""
    fn = getattr(binding_store, "clear_absent", None)
    if fn is not None:
        try:
            fn(jira_key)
        except Exception:  # noqa: BLE001 — fail-open: best-effort clear_absent no-op on failure
            pass


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
    # Convergence rollout Phase-3 (story a118). When True, _diff_fields consumes
    # the per-binding baseline (BindingStore.get_baseline(local_id)) as the
    # arbitration ancestor in place of prev_snapshot. Off by default end-to-end.
    baseline_consumer_swap: bool = False


# ---------------------------------------------------------------------------
# Label diff
# ---------------------------------------------------------------------------

# NOTE: applier.py writes the bridge-internal binding label as
# f"rebar-id:{local_id}" (COLON separator). Legacy code paths used a HYPHEN
# separator ("rebar-id-<local_id>"); both forms must be excluded from outbound
# diffs to avoid emitting spurious remove mutations for identity labels.
# See bug 68a4-f9d5-5540-4b95.
# rebar-status: labels are reconciler-managed annotation labels (emitted/removed
# by status logic only); they must be excluded from the normal user-tag diff
# so that rebar-status: labels on Jira do not produce spurious REMOVE mutations
# via the tag diff path (ticket 929a).
_EXCLUDED_PREFIXES: tuple[str, ...] = ("rebar-id:", "rebar-id-", "imported:", "rebar-status:")


def _diff_labels(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    intent_set: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compare local tags to Jira labels. Exclude bridge-internal labels.

    Bug a06c — REMOVE intent gating: when ``intent_set`` is provided
    (non-None), a label in ``jira_labels - local_tags`` only produces a
    REMOVE mutation when it appears in ``intent_set`` (the local
    "ever-seen" set computed by ``local_label_intent``). This prevents
    spurious REMOVEs for labels Jira side-added that local never had —
    the root cause of T3 IB-ADD silently dropping under PR #457 bidir
    suppression.

    When ``intent_set`` is None, the legacy "remove anything in jira
    but not in local" behavior is preserved (backwards compatible for
    every existing test and call site).
    """
    local_tags: set[str] = set(
        t for t in ticket.get("tags", []) if not any(t.startswith(p) for p in _EXCLUDED_PREFIXES)
    )
    jira_labels: set[str] = set(
        label
        for label in (jira_fields.get("labels") or [])
        if not any(label.startswith(p) for p in _EXCLUDED_PREFIXES)
    )

    mutations: list[dict[str, Any]] = []
    for label in sorted(local_tags - jira_labels):
        if intent_set is not None and label not in intent_set:
            # Label is in local's current tag set but was never user-added
            # (only inbound-applied). Suppress the outbound ADD so a
            # subsequent Jira-side REMOVE is not cancelled by a spurious
            # re-ADD (T4 IB-REMOVE regression). See bug a06c.
            continue
        mutations.append({"action": "add", "label": label})
    for label in sorted(jira_labels - local_tags):
        if intent_set is not None and label not in intent_set:
            # Label was never in local's history -> suppress spurious REMOVE.
            continue
        mutations.append({"action": "remove", "label": label})
    return mutations


# ---------------------------------------------------------------------------
# Status annotation label helpers (ticket 929a)
# ---------------------------------------------------------------------------

# Local statuses that need an annotation label to preserve lossless intent.
# Maps local_status -> rebar-status:<label> emitted when that status is active.
# (blocked/cancelled have no direct equivalent in the live DIG workflow, so the
# nearest live state plus this annotation label is the lossless encoding.)
_STATUS_ANNOTATION_LABEL: dict[str, str] = {
    "blocked": "rebar-status:blocked",
    "cancelled": "rebar-status:cancelled",
}


def _diff_status_annotation_labels(
    local_status: str,
    jira_labels: list[str],
) -> list[dict[str, Any]]:
    """Compute add/remove mutations for rebar-status: annotation labels.

    These labels encode lossless status information for statuses that have no
    direct equivalent in the live DIG Jira workflow (currently blocked and
    cancelled, which map to In Progress and Done respectively).

    Rules:
    - When local_status is in _STATUS_ANNOTATION_LABEL, emit ADD for the
      corresponding rebar-status: label if Jira does not already carry it.
    - When a rebar-status: annotation label is present on Jira but local_status
      no longer matches it, emit REMOVE to clean up the stale label.
    """
    mutations: list[dict[str, Any]] = []
    desired_annotation = _STATUS_ANNOTATION_LABEL.get(local_status)
    jira_annotation_labels = {label for label in jira_labels if label.startswith("rebar-status:")}

    # Add desired annotation if not already present
    if desired_annotation is not None and desired_annotation not in jira_annotation_labels:
        mutations.append({"action": "add", "label": desired_annotation})

    # Remove stale annotations (rebar-status: labels that no longer match)
    for stale in sorted(jira_annotation_labels):
        if stale != desired_annotation:
            mutations.append({"action": "remove", "label": stale})

    return mutations


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def compute_outbound_mutations(
    local_tickets: list[dict[str, Any]],
    jira_snapshot: dict[str, Any],
    binding_store: BindingStoreProtocol,
    config: OutboundDiffConfig | None = None,
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
    _assignee_cache: dict[str, tuple[str | None, bool]] = {}

    def _assignee_resolver(assignee: str, jira_key: str) -> tuple[str | None, bool]:
        """Resolve a local assignee to a Jira accountId.

        Returns ``(account_id_or_None, authoritative)``. ``authoritative`` is
        ``True`` when the result is trustworthy: an empty local assignee
        (→ unassigned), a successful resolution (→ accountId), or a definitive
        "no assignable user" (→ ``None`` = unassigned). It is ``False`` when we
        could not determine the mapping (no client, or a transient lookup
        error) — the caller then preserves the legacy string-match behavior.
        """
        if not assignee:
            return ("", True)
        if assignee in _assignee_cache:
            return _assignee_cache[assignee]
        if client is None or not jira_key:
            result: tuple[str | None, bool] = (None, False)
        else:
            try:
                acct = client.validate_assignee_exists(assignee, issue_key=jira_key)
                result = (acct or None, True)
            except Exception as exc:  # noqa: BLE001 — classify the resolution outcome
                # AssigneeNotFoundError ⇒ definitively unassignable (→ unassigned).
                # Any other (transient/transport) error ⇒ unknown → string-match
                # fallback, so a Jira blip never spuriously unassigns a ticket.
                if type(exc).__name__ == "AssigneeNotFoundError":
                    result = (None, True)
                else:
                    result = (None, False)
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

        if jira_key is None:
            _compute_outbound_create_mutation(
                mutations, ticket, status, local_id, binding_store, local_ticket_types
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
                conflict_sink=conflict_sink,
                dropped_field_sink=dropped_field_sink,
                baseline_consumer_swap=config.baseline_consumer_swap,
            )

    return mutations, absent_alive_fields


def _compute_outbound_select_absent_gets(
    local_tickets, jira_snapshot, binding_store, excluded_statuses, excluded_sync_types, client
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
    from rebar.config import ConfigError, load_config

    try:
        _budget = load_config().reconciler.deletion_probe_limit
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


def _compute_outbound_create_mutation(
    mutations, ticket, status, local_id, binding_store, local_ticket_types
) -> None:
    """Phase: append the outbound CREATE mutation for an unbound local ticket."""
    # Unbound -> outbound create
    # ticket 929a: for new issues the Jira side has no labels yet,
    # so the annotation label only needs an ADD (never a REMOVE).
    annotation_mutations = _diff_status_annotation_labels(
        local_status=status,
        jira_labels=[],
    )
    mutations.append(
        OutboundMutation(
            local_id=local_id,
            jira_key=None,
            action="create",
            fields=_map_local_to_jira_fields(
                ticket,
                binding_store=binding_store,
                local_ticket_types=local_ticket_types,
            ),
            comments=_map_comments_for_create(ticket),
            labels=(
                [
                    {"action": "add", "label": t}
                    for t in sorted(ticket.get("tags", []))
                    if not any(t.startswith(p) for p in _EXCLUDED_PREFIXES)
                ]
                + annotation_mutations
            ),
            links=[],  # links resolved after all creates
        )
    )


def _compute_outbound_update_mutation(
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
    *,
    conflict_sink: list[tuple[str, str]] | None = None,
    dropped_field_sink: list[tuple[str, str]] | None = None,
    baseline_consumer_swap: bool = False,
) -> None:
    """Phase: for a bound ticket, resolve jira_fields (including the bounded
    bound-but-absent direct GET) and append an outbound UPDATE mutation when anything
    diverged. A bare ``return`` skips the ticket (emit nothing)."""
    # Bound -> compare fields, emit update if different.
    #
    # Bug 1e08-1a35-0267-4ca6: discriminate on MEMBERSHIP, not value.
    # A bound key ABSENT from this pass's search snapshot must NOT diff
    # against ``{}`` (that re-emits every field every pass). Two absence
    # sub-classes: (a) deleted → direct GET 404; (b) status=Done beyond
    # _DONE_RECENT_CAP → alive (HTTP 200) but absent from the search
    # snapshot. We resolve the real fields via a bounded direct GET.
    if jira_key in jira_snapshot:
        # EXISTING path — key present in the search snapshot.
        jira_fields = jira_snapshot[jira_key]
        comment_snapshot = jira_snapshot
    else:
        # Bound-but-absent from THIS pass's working set.
        if client is None:
            # No client → we cannot direct-GET to resolve the absence.
            # Skip (defer) rather than diff against {} — that re-emit
            # against an empty dict was the original defect (bug 1e08).
            # Mirrors the _diff_comments no-client safety pattern.
            return
        if _is_retired(binding_store, jira_key):
            return  # known-dead; no GET, no emit (budget preserved)
        if jira_key not in _selected_for_get_this_pass:
            return  # not selected this pass → DEFERRED (no emit)

        fields = _safe_get_issue(client, jira_key)
        # Record the GET regardless of outcome (rotation bookkeeping).
        _set_last_get(binding_store, jira_key, pass_id)

        if fields is _DELETED:
            # HTTPError 404 — issue gone. Bump the consecutive-404
            # counter (may retire at GRACE). Emit nothing.
            _note_absent(binding_store, jira_key)
            return
        if fields is _TRANSPORT_ERROR:
            # Non-404 HTTPError / URLError / timeout — transient.
            # Emit nothing, warn, defer; counter untouched.
            print(  # noqa: T201
                f"WARNING: outbound_differ: direct GET for bound-but-absent "
                f"{jira_key!r} failed (transport error). Deferring this "
                f"key's sync to a later pass (no mutation emitted).",
                file=sys.stderr,
            )
            return

        # HTTP 200 — issue is alive (out-of-window). Reset the absence
        # counter and build a one-key overlay so the SAME diff path runs.
        _clear_absent(binding_store, jira_key)
        jira_fields = fields
        comment_snapshot = dict(jira_snapshot)
        comment_snapshot[jira_key] = fields
        # Bug 0702: share this alive GET result with the inbound differ
        # so the out-of-window key is mirrored Jira→local without a
        # second GET. Only the alive (200) case is recorded — 404 and
        # transport errors are intentionally left out so a gone issue is
        # never inbound-mirrored (retirement stays outbound-owned).
        absent_alive_fields[jira_key] = fields

    changed = _diff_fields(
        ticket,
        jira_fields,
        binding_store=binding_store,
        local_ticket_types=local_ticket_types,
        assignee_resolver=_assignee_resolver,
        jira_key=jira_key,
        prev_jira_fields=(prev_snapshot or {}).get(jira_key),
        conflict_sink=conflict_sink,
        dropped_field_sink=dropped_field_sink,
        local_id=local_id,
        baseline_consumer_swap=baseline_consumer_swap,
    )
    # Comments use the resolved snapshot (the one-key overlay for the
    # bounded-GET path) so the GET's native fields.comment.comments is
    # consulted with NO second network call (C3).
    comment_mutations = _diff_comments(ticket, jira_key, comment_snapshot, client=client)
    # bug a06c: intent-gated REMOVE. When local_label_intent is
    # provided but lacks an entry for this local_id, fall back to
    # an empty intent set (lazy first-pass safety: suppresses all
    # REMOVEs for tickets we have no event-log evidence for).
    intent_set: set[str] | None = None
    if local_label_intent is not None:
        intent_set = local_label_intent.get(local_id, set())
    label_mutations = _diff_labels(ticket, jira_fields, intent_set)
    # ticket 929a: status annotation labels (rebar-status:blocked/cancelled)
    # are managed separately from user tags (excluded from _diff_labels via
    # _EXCLUDED_PREFIXES). Compute and merge annotation mutations here.
    annotation_mutations = _diff_status_annotation_labels(
        local_status=status,
        jira_labels=list(jira_fields.get("labels") or []),
    )
    label_mutations = label_mutations + annotation_mutations
    # story 25ae Cycle 2: diff local deps -> Jira issuelinks (ADD-only,
    # deduped against the snapshot's existing issuelinks so an
    # already-present link emits nothing — no per-pass churn).
    link_mutations = _diff_links(ticket, jira_fields, binding_store)

    if changed or comment_mutations or label_mutations or link_mutations:
        # Sync-hardening P5 / bug 57d1 diagnosis enabler: emit a
        # one-line CHANGED-FIELD BREADCRUMB whenever a bound key gets
        # an outbound UPDATE carrying field diffs. Logs the changed
        # FIELD NAMES only (never values — descriptions/assignees may
        # be large or sensitive) so a re-emitting (non-converging)
        # field becomes visible in CI logs without live Jira creds.
        # The field list is the keys of the same `changed` dict
        # _diff_fields already computed — no recomputation. Comment-
        # only / label-only updates carry no field diff, so `changed`
        # is empty and the breadcrumb is skipped (keeps stderr quiet
        # for the common comment-mirror case).
        print(  # noqa: T201
            f"RECON: outbound_update key={jira_key} "
            f"changed=[{','.join(sorted(changed))}] "
            f"comments={len(comment_mutations)} "
            f"labels={len(label_mutations)} "
            f"links={len(link_mutations)}",
            file=sys.stderr,
        )
        mutations.append(
            OutboundMutation(
                local_id=local_id,
                jira_key=jira_key,
                action="update",
                fields=changed,
                comments=comment_mutations,
                labels=label_mutations,
                links=link_mutations,
            )
        )
