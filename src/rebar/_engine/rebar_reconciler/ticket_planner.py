"""Pure ticket planner — groups already-computed Mutations into per-ticket plans.

``plan_pass`` builds one immutable ``Observation`` for a reconcile pass and
partitions the pass's ``Mutation`` values by ``Mutation.target`` into a
deterministic tuple of ``TicketPlan``s. It performs ZERO I/O and reads NO clock:
it is a pure function of its frozen inputs, so identical inputs yield equal
observations and plans (AC1). Grouping never adds, drops, or duplicates a
mutation.

Cross-sibling types (``Mutation`` for grouping keys, plus ``Observation`` /
``TicketPlan`` / ``PlanDisposition``) are loaded by file path via the package's
shared ``lazy_load`` idiom (``_loader.py``), matching every other reconciler
sibling and resolving both under the real package and when exec'd standalone.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rebar._store.canonical import canonical_str

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

_observation_mod = lazy_load("rebar_reconciler.observation", "observation.py")
_ticket_plan_mod = lazy_load("rebar_reconciler.ticket_plan", "ticket_plan.py")

build_observation = _observation_mod.build_observation
TicketPlan = _ticket_plan_mod.TicketPlan
PlanDisposition = _ticket_plan_mod.PlanDisposition
IntentKind = _ticket_plan_mod.IntentKind
DeferReason = _ticket_plan_mod.DeferReason
LifecycleIntent = _ticket_plan_mod.LifecycleIntent

# Selection kinds that scope the pass to an explicit id set (kind ``all``/``None`` do not).
_SCOPE_KINDS: frozenset[str] = frozenset({"ids", "subset", "local_ids"})
# Skip causes that turn a plan into a ``skipped`` no-op (granular cause kept in diagnostics).
_SKIP_CAUSES: frozenset[str] = frozenset(
    {"tombstone", "index_lag", "moved_key", "impossible_link", "partial_snapshot"}
)
# Default action → lifecycle intent kind mapping (probe/conflict map to no kind).
_ACTION_TO_KIND: Mapping[str, str] = {
    "create": "bind",
    "update": "confirm",
    "delete": "retire",
    "repair_property": "baseline",
    "clean_label": "comment_identity",
}
_INTENT_KIND_VALUES: frozenset[str] = frozenset(k.value for k in IntentKind)


def _collect_dependencies(muts: Sequence[Any]) -> tuple[str, ...]:
    """Structural prerequisite identities declared by a target's mutation payloads via
    ``requires_create`` / ``requires_parent`` (each a single id or a list), sorted+deduped."""
    deps: set[str] = set()
    for m in muts:
        for key in ("requires_create", "requires_parent"):
            value = m.payload.get(key)
            if isinstance(value, str):
                deps.add(value)
            elif isinstance(value, (list, tuple)):
                deps.update(v for v in value if isinstance(v, str))
    return tuple(sorted(deps))


def _skip_causes(muts: Sequence[Any]) -> tuple[str, ...]:
    """The granular skip cause of every mutation whose ``payload['skip']`` is recognized,
    in ``muts`` order (empty when none)."""
    return tuple(m.payload.get("skip") for m in muts if m.payload.get("skip") in _SKIP_CAUSES)


def _scope_excluded(target: str, selection: Mapping[str, Any]) -> bool:
    """True when ``selection`` scopes to an explicit id set and ``target`` is not in it."""
    if selection.get("kind") in _SCOPE_KINDS:
        return target not in set(selection.get("ids") or [])
    return False


def _mode_capped(muts: Sequence[Any], mode: str) -> bool:
    """True when a non-``live`` mode must abort a target carrying any outbound mutation."""
    return mode != "live" and any(m.direction.value == "outbound" for m in muts)


def _classify_early(
    target: str, muts: Sequence[Any], selection: Mapping[str, Any], mode: str
) -> tuple[Any, Any, tuple[str, ...]] | None:
    """Rules 1–3 (scope / skip / mode-cap) in precedence order. Returns
    ``(disposition, defer_reason, extra_diagnostics)`` or ``None`` when the target is
    eligible for the later limit/dependency stages."""
    if _scope_excluded(target, selection):
        return (PlanDisposition.defer, DeferReason.scope_deferred, ())
    causes = _skip_causes(muts)
    if causes:
        return (PlanDisposition.noop, DeferReason.skipped, tuple(f"skip:{c}" for c in causes))
    if _mode_capped(muts, mode):
        return (PlanDisposition.defer, DeferReason.safety_aborted, ())
    return None


def _limit_excluded(eligible: Sequence[str], max_changes: Any) -> set[str]:
    """Rule 4: eligible targets (ascending) at position ``>= max_changes`` are limit-capped;
    an absent/``None`` limit excludes nothing."""
    if max_changes is None:
        return set()
    return {t for i, t in enumerate(sorted(eligible)) if i >= max_changes}


def _is_satisfied(
    prereq: str,
    create_targets: set[str],
    binding_view: Mapping[str, Any],
    local_snapshot: Mapping[str, Any],
    remote_snapshot: Mapping[str, Any],
) -> bool:
    return (
        prereq in create_targets
        or prereq in binding_view
        or prereq in local_snapshot
        or prereq in remote_snapshot
    )


def _dependency_blocked(
    deps: Sequence[str],
    create_targets: set[str],
    binding_view: Mapping[str, Any],
    local_snapshot: Mapping[str, Any],
    remote_snapshot: Mapping[str, Any],
) -> bool:
    return bool(deps) and any(
        not _is_satisfied(r, create_targets, binding_view, local_snapshot, remote_snapshot)
        for r in deps
    )


def _intent_kind(m: Any) -> Any:
    """The lifecycle intent kind for a mutation: an explicit ``payload['lifecycle']``
    override (one of the 6 IntentKind values) wins, else the action mapping (probe /
    conflict → ``None``)."""
    override = m.payload.get("lifecycle")
    if isinstance(override, str) and override in _INTENT_KIND_VALUES:
        return IntentKind(override)
    mapped = _ACTION_TO_KIND.get(m.action.value)
    return IntentKind(mapped) if mapped is not None else None


def _derive_intents(muts: Sequence[Any], target: str, version: Any) -> tuple[Any, ...]:
    """One LifecycleIntent per mutation that maps to a kind, in ``muts`` order."""
    intents = []
    for m in muts:
        kind = _intent_kind(m)
        if kind is None:
            continue
        intents.append(
            LifecycleIntent(kind=kind, target=target, version=version, payload=m.payload)
        )
    return tuple(intents)


def _mutation_sort_key(m: Any) -> tuple[str, str, str]:
    """Total, deterministic ordering key for a Mutation within its target group.

    ``(direction.value, action.value)`` alone leaves two mutations that share that
    pair (distinguished only by ``payload``/``provenance``, which are excluded from
    Mutation identity) ordered by input happenstance. A canonical serialization of
    the full mutation is appended as a stable tie-breaker so plan-internal ordering
    is a pure function of the mutation set, not of the differ's emission order (AC1).
    """
    return (
        m.direction.value,
        m.action.value,
        canonical_str(
            {
                "direction": m.direction.value,
                "action": m.action.value,
                "target": m.target,
                "payload": dict(m.payload),
                "provenance": dict(m.provenance),
            },
            ascii_only=True,
        ),
    )


def plan_pass(
    *,
    pass_id: str,
    local_snapshot: Mapping[str, Any],
    remote_snapshot: Mapping[str, Any],
    binding_view: Mapping[str, Any],
    mode: str,
    selection: Mapping[str, Any],
    limits: Mapping[str, Any],
    mutations: Sequence[Any],
    diagnostics_by_target: Mapping[str, Sequence[str]] | None = None,
    plan_payload_by_target: Mapping[str, Mapping[str, Any]] | None = None,
    observation_payload: Mapping[str, Any] | None = None,
) -> tuple[Any, tuple[Any, ...]]:
    """Purely build a pass ``Observation`` and one ``TicketPlan`` per mutation target.

    Mutations are grouped by ``Mutation.target``; targets are emitted in sorted
    order and each plan's mutations are sorted by ``_mutation_sort_key`` (a total,
    canonical ordering) so output is deterministic regardless of input order. Every
    plan carries ``disposition=mutate`` and the pass observation version. No I/O,
    no clock.
    """
    diagnostics_by_target = diagnostics_by_target or {}
    plan_payload_by_target = plan_payload_by_target or {}

    observation = build_observation(
        pass_id=pass_id,
        local_snapshot=local_snapshot,
        remote_snapshot=remote_snapshot,
        binding_view=binding_view,
        mode=mode,
        selection=selection,
        limits=limits,
        payload=observation_payload,
    )

    grouped: dict[str, list[Any]] = defaultdict(list)
    for mutation in mutations:
        grouped[mutation.target].append(mutation)

    version = observation.version
    sorted_targets = sorted(grouped)
    muts_by_target = {
        target: tuple(sorted(grouped[target], key=_mutation_sort_key)) for target in sorted_targets
    }
    deps_by_target = {
        target: _collect_dependencies(muts_by_target[target]) for target in sorted_targets
    }
    create_targets = {
        target
        for target, muts in muts_by_target.items()
        if any(m.action.value == "create" for m in muts)
    }

    # Stage 1 (rules 1–3): scope / skip / mode-cap exclusions; the rest are "eligible".
    early = {
        target: _classify_early(target, muts_by_target[target], selection, mode)
        for target in sorted_targets
    }
    eligible = [target for target in sorted_targets if early[target] is None]
    # Stage 2 (rule 4): global change limit over eligible targets in ascending order.
    limited = _limit_excluded(eligible, limits.get("max_changes"))

    plans: list[Any] = []
    for target in sorted_targets:
        muts = muts_by_target[target]
        deps = deps_by_target[target]
        disposition, defer_reason, extra, final_muts, intents = _decide_plan(
            target,
            muts,
            deps,
            early[target],
            limited,
            version,
            create_targets,
            binding_view,
            local_snapshot,
            remote_snapshot,
        )
        plans.append(
            TicketPlan(
                identity=target,
                mutations=final_muts,
                diagnostics=tuple(diagnostics_by_target.get(target, ())) + extra,
                disposition=disposition,
                observation_version=version,
                payload=plan_payload_by_target.get(target, {}),
                intents=intents,
                dependencies=deps,
                defer_reason=defer_reason,
            )
        )

    return observation, tuple(plans)


def _decide_plan(
    target: str,
    muts: tuple[Any, ...],
    deps: tuple[str, ...],
    early: tuple[Any, Any, tuple[str, ...]] | None,
    limited: set[str],
    version: Any,
    create_targets: set[str],
    binding_view: Mapping[str, Any],
    local_snapshot: Mapping[str, Any],
    remote_snapshot: Mapping[str, Any],
) -> tuple[Any, Any, tuple[str, ...], tuple[Any, ...], tuple[Any, ...]]:
    """Resolve one target's final ``(disposition, defer_reason, extra_diagnostics,
    mutations, intents)`` following the rule precedence. Only rule 5 empties mutations;
    only rule 6 (mutate) derives intents."""
    if early is not None:
        disposition, defer_reason, extra = early
        return disposition, defer_reason, extra, muts, ()
    if target in limited:
        return PlanDisposition.defer, DeferReason.safety_aborted, (), muts, ()
    if _dependency_blocked(deps, create_targets, binding_view, local_snapshot, remote_snapshot):
        return PlanDisposition.defer, DeferReason.dependency_deferred, (), (), ()
    return PlanDisposition.mutate, None, (), muts, _derive_intents(muts, target, version)
