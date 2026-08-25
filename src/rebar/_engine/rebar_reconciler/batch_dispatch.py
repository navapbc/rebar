#!/usr/bin/env python3
"""Outbound batch-dispatch facade: delete_one + mutation->batch-dict + re-exports.

The bulk of per-mutation dispatch — ``create_one`` / ``update_one`` plus the
``_call_with_retry`` backoff wrapper, the link-probe helpers, and the
``_is_illegal_transition_400`` predicate — was extracted to the sibling
``dispatch_one.py`` (module-size split, ticket b043-9490) and is re-exported here
so this module stays the stable public surface (``rebar_reconciler.batch_dispatch.*``)
that ``applier`` / ``apply_handlers`` / ``apply_outbound`` / ``apply_inbound`` and
the reconciler tests import from.

This module still OWNS ``delete_one`` (close = delete the Jira issue, 404
tolerated) and ``_mutation_to_batch_dict`` (normalise a typed Mutation into the
legacy batch-dict shape). Imports only downward (``_errors``, ``dispatch_one``);
never imports ``applier``, so the orchestrator can import these back without a
cycle.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._backend import TicketTransport

from rebar_reconciler._errors import (
    JiraAPIError,
    RetryExhaustedError,
    is_not_found,
)
from rebar_reconciler.dispatch_one import (
    _call_with_retry,
    _find_link_id,
    _index_existing_links,
    _is_illegal_transition_400,
    create_one,
    update_one,
)

try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)
    lazy_load = sys.modules[_loader_key].lazy_load

__all__ = [
    "COORDINATOR_ROUTE",
    "LEGACY_ROUTE",
    "NON_CREATE_FAMILIES",
    "CutoverOutcome",
    "CutoverReport",
    "JiraAPIError",
    "RetryExhaustedError",
    "_call_with_retry",
    "_find_link_id",
    "_index_existing_links",
    "_is_illegal_transition_400",
    "_mutation_to_batch_dict",
    "build_pass_tally",
    "coordinate_and_fuse",
    "create_one",
    "delete_one",
    "route_for",
    "update_one",
]


def delete_one(mutation: dict, client: TicketTransport) -> None:
    """Close a Jira issue by transitioning it to 'Closed'.

    F5: tolerate 404 — when the differ emits a delete it's precisely because
    the issue is no longer present in Jira; the subsequent transition_issue
    call therefore targets a key that may have already been removed. A 404 on
    the transition means the desired post-state ('issue gone') is already
    satisfied, so we treat it as success rather than letting the JiraAPIError
    unwind the entire pass. Other JiraAPIError statuses propagate normally.
    """
    # AcliClient exposes delete_issue (REST DELETE), not transition_issue.
    # The "close = transition to Closed" model belongs to a different bridge
    # surface that we don't use here — delete the Jira issue directly to
    # achieve the desired post-state ("issue gone from Jira").
    try:
        _call_with_retry(client.delete_issue, mutation.get("key"))
    except JiraAPIError as exc:
        if is_not_found(exc):
            return  # already-gone is the goal of a delete mutation
        raise


def _mutation_to_batch_dict(mutation) -> dict:
    """Convert a Mutation dataclass instance to the legacy batch-dict shape.

    The legacy batch consumer (_apply_batch) expects a dict with keys:
    action, fields, key, local_id, follow_on, direction. Map the Mutation
    attributes accordingly so the batch path can iterate without crashing.

    Note: this dict is later passed through `json.dumps` when the manifest
    is written. Every value here MUST be JSON-serializable. Do NOT store
    the original Mutation object as a back-reference — non-serializable.
    """
    payload = dict(mutation.payload) if mutation.payload else {}
    action_value = getattr(mutation.action, "value", str(mutation.action))
    direction_value = getattr(mutation.direction, "value", str(mutation.direction))
    # Bug 87e4: outbound mutations from reconcile.py have two different
    # payload shapes depending on action:
    #
    #   - CREATE: payload has create fields at the TOP LEVEL (summary,
    #     description, priority, issuetype, assignee, ...) alongside
    #     bookkeeping keys (local_id, comments, labels). create_one needs
    #     the full set of fields.
    #
    #   - UPDATE: payload has changed fields under "changed_fields", with
    #     "comments" and "labels" as separate top-level keys. update_one
    #     needs ONLY the scalar field changes — passing the whole payload
    #     would unpack bogus `changed_fields=`, `comments=`, `labels=`
    #     kwargs to client.update_issue (the original bug symptom).
    #
    # Distinguish by action and read the appropriate shape.
    _BOOKKEEPING_KEYS = {
        "changed_fields",
        "comments",
        "labels",
        "local_id",
        "follow_on",
    }
    if action_value == "update":
        fields = payload.get("changed_fields")
        if fields is None:
            fields = payload.get("fields", {})
    elif action_value == "create":
        # Two CREATE payload shapes coexist:
        #   - Legacy (test fixtures + older callers): payload has a nested
        #     "fields" key.  Honor it explicitly (including the
        #     intentionally-empty {} case — the original "fields=={}
        #     must NOT fall through to full payload" contract).
        #   - New (reconcile.py:524-535): payload spreads create fields
        #     at the TOP LEVEL via `**om.fields`, alongside bookkeeping
        #     keys.  Strip bookkeeping; everything else is a field.
        if "fields" in payload:
            fields = payload.get("fields", {})
        else:
            fields = {k: v for k, v in payload.items() if k not in _BOOKKEEPING_KEYS}
    else:
        # Other actions (delete, probe, etc.) don't carry field maps.
        fields = payload.get("fields", {})
    return {
        "action": action_value,
        "direction": direction_value,
        "key": mutation.target,
        "fields": fields,
        "local_id": payload.get("local_id", ""),
        "follow_on": payload.get("follow_on"),
        # Surface comments and labels so update_one can dispatch them via
        # add_comment / add_label / remove_label respectively (bug 87e4).
        "comments": payload.get("comments", []),
        "labels": payload.get("labels", []),
        # Surface links so update_one can dispatch them via set_relationship
        # (bug 3f04). Previously omitted here, so the production batch path
        # silently dropped every outbound blocks/relates link — the link was
        # reported "applied" (the mutation succeeded) but never created in Jira.
        "links": payload.get("links", []),
    }


# ── Non-create cutover surface (RP-03 S3 T3) ─────────────────────────────────────
#
# Routes the six non-create mutation families through the S3 coordinator + fuse
# instead of the legacy per-mutation path, then normalizes their terminal outcomes.
# The canonical ``coordinator`` / ``pass_fuse`` / ``failure_policy`` /
# ``operation_outcome`` siblings are loaded lazily by file path (the package's shared
# ``lazy_load`` idiom) INSIDE the functions, mirroring ``coordinator.py``, so importing
# this facade never triggers those sibling imports (no cycle). The decision logic is
# pure given the injected ``execute`` / ``locate`` / ``budget_factory`` / ``now_ms``:
# it reads no clock and does no I/O.


NON_CREATE_FAMILIES = frozenset(
    {"update", "delete", "probe", "clean_label", "repair_property", "conflict"}
)
COORDINATOR_ROUTE = "coordinator"
LEGACY_ROUTE = "legacy"


def route_for(action, overrides=None) -> str:
    """Select EXACTLY ONE route for a mutation family (never dual-send).

    Every family in :data:`NON_CREATE_FAMILIES` defaults to
    :data:`COORDINATOR_ROUTE`; any other action (``create``, unknown strings)
    stays on :data:`LEGACY_ROUTE`. An optional ``overrides`` mapping flips a single
    family; a route value that is not exactly ``coordinator`` or ``legacy`` raises
    ``ValueError``.
    """
    default = COORDINATOR_ROUTE if action in NON_CREATE_FAMILIES else LEGACY_ROUTE
    if overrides is None or action not in overrides:
        return default
    if action not in NON_CREATE_FAMILIES:
        # The selector only governs the non-create families; an override that tries to
        # route ``create`` (or any non-migrated action) onto the coordinator would be a
        # silent mis-route, so reject it rather than honor it.
        raise ValueError(f"route override not permitted for non-cutover family {action!r}")
    route = overrides[action]
    if route not in (COORDINATOR_ROUTE, LEGACY_ROUTE):
        raise ValueError(f"invalid route override for {action!r}: {route!r}")
    return route


@dataclass(frozen=True, slots=True)
class CutoverOutcome:
    """One ticket's normalized cutover result: the coordinator's terminal
    disposition/bucket/scope, plus the fuse ``FuseDecision`` when it was reclassified
    to deferred under an already-open scope (``None`` otherwise)."""

    identity: str
    disposition: object
    bucket: str
    failure_scope: object
    observation_version: object
    fuse_decision: object


@dataclass(frozen=True, slots=True)
class CutoverReport:
    """The full cutover result: per-ticket outcomes, the five-bucket tally, the
    distinct fuse decisions raised this pass, and a ``degraded`` flag (any failure OR an
    opened fuse)."""

    outcomes: tuple
    tallies: Mapping
    fuse_decisions: tuple
    degraded: bool

    def outcome_for(self, identity: str) -> CutoverOutcome:
        for outcome in self.outcomes:
            if outcome.identity == identity:
                return outcome
        raise KeyError(identity)


def _empty_locate(_identity):
    return {}


def _fuse_one(outcome, fuse, policy, outcome_mod) -> CutoverOutcome:
    """Normalize one coordinator outcome against the fuse.

    PRE-check the fuse BEFORE recording this outcome: if a matching scope is
    already open and this outcome is fuse-ELIGIBLE (a budget-exhaustion / retryable
    outcome the open scope says not to keep retrying), it is remaining matching work
    under that open scope, so reclassify it to a ``deferred`` / ``scope_deferred``
    CutoverOutcome carrying the decision (do NOT record it — a deferred op is held
    back, not a consumed eligible outcome). A genuine terminal failure
    (``permanent_failure``) or success is NOT fuse-eligible, so it keeps its own
    coordinator bucket — an open scope must never MASK a real failure as deferred.
    Otherwise record it (which opens/resets scopes on this terminal outcome) and carry
    the coordinator's own bucket/disposition/scope through.
    """
    decision = fuse.decision_for(outcome.identity)
    if decision is not None and policy.is_fuse_eligible(outcome.disposition):
        return CutoverOutcome(
            identity=outcome.identity,
            disposition=outcome_mod.Disposition.scope_deferred,
            bucket="deferred",
            # ``decision.scope`` is a plain scope string; coerce it back to the
            # ``FailureScope`` enum so ``CutoverOutcome.failure_scope`` is the SAME
            # type on both the record path and this defer path.
            failure_scope=outcome_mod.FailureScope(decision.scope),
            observation_version=outcome.observation_version,
            fuse_decision=decision,
        )
    fuse.record(outcome)
    return CutoverOutcome(
        identity=outcome.identity,
        disposition=outcome.disposition,
        bucket=outcome.bucket,
        failure_scope=outcome.failure_scope,
        observation_version=outcome.observation_version,
        fuse_decision=None,
    )


def coordinate_and_fuse(
    ticket_plans,
    *,
    execute,
    locate=None,
    budget_factory=None,
    now_ms=None,
    cooldown_ms=None,
) -> CutoverReport:
    """Run the S3 coordinator then fold its terminal outcomes through the pass fuse.

    Walks ``report.outcomes`` in the coordinator's dependency/topological order,
    reclassifying remaining matching work under an already-open scope to ``deferred``
    (attaching the raising ``FuseDecision``) while independent scopes continue. Returns
    a :class:`CutoverReport` with the recomputed five-bucket tally, the distinct fuse
    decisions in first-seen order, and a ``degraded`` flag set when any ticket failed.
    """
    coordinator = lazy_load("rebar_reconciler.coordinator", "coordinator.py")
    fuse_mod = lazy_load("rebar_reconciler.pass_fuse", "pass_fuse.py")
    policy = lazy_load("rebar_reconciler.failure_policy", "failure_policy.py")
    outcome_mod = lazy_load("rebar_reconciler.operation_outcome", "operation_outcome.py")

    report = coordinator.coordinate(
        ticket_plans, execute=execute, budget_factory=budget_factory, locate=locate
    )
    fuse = fuse_mod.PassFuse(
        locate=locate or _empty_locate,
        now_ms=now_ms,
        cooldown_ms=fuse_mod.FUSE_COOLDOWN_MS if cooldown_ms is None else cooldown_ms,
    )
    outcomes: list[CutoverOutcome] = []
    fuse_decisions: list = []
    seen: set = set()
    for outcome in report.outcomes:
        cutover = _fuse_one(outcome, fuse, policy, outcome_mod)
        outcomes.append(cutover)
        decision = cutover.fuse_decision
        if decision is not None and id(decision) not in seen:
            seen.add(id(decision))
            fuse_decisions.append(decision)
    tallies = {bucket: 0 for bucket in policy.OUTCOME_BUCKETS}
    for cutover in outcomes:
        tallies[cutover.bucket] += 1
    # A pass is degraded when any ticket failed OR the fuse opened this pass: an open
    # fuse means matching work was safety-deferred rather than converged, so the pass
    # did not cleanly complete even if its ``failed`` bucket is empty (a fuse can open
    # purely on budget-deferred ``retryable_deferred`` outcomes).
    degraded = tallies["failed"] > 0 or bool(fuse_decisions)
    return CutoverReport(tuple(outcomes), tallies, tuple(fuse_decisions), degraded)


def build_pass_tally(report) -> dict:
    """Project a :class:`CutoverReport` onto the LIVE pass-tally shape the reconciler
    consumes. ``applied_count`` folds recovered into applied; the raw five-bucket map is
    preserved under ``buckets``; ``deferred_count`` / ``skipped_count`` account for the
    fuse-held and data-skipped mutations the legacy consumer would otherwise drop; and
    ``degraded`` carries the exact degraded-pass exit signal (any failure OR an opened
    fuse) so a fuse-deferred pass with no failures still exits non-zero."""
    tallies = report.tallies
    return {
        "applied_count": tallies["applied"] + tallies["recovered"],
        "failed_count": tallies["failed"],
        "deferred_count": tallies["deferred"],
        "skipped_count": tallies["skipped"],
        "recovered_count": tallies["recovered"],
        "degraded": bool(report.degraded),
        "buckets": dict(tallies),
    }
