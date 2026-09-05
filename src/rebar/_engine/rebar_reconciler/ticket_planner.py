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
_mode_mod = lazy_load("rebar_reconciler.mode", "mode.py")

build_observation = _observation_mod.build_observation
TicketPlan = _ticket_plan_mod.TicketPlan
PlanDisposition = _ticket_plan_mod.PlanDisposition
IntentKind = _ticket_plan_mod.IntentKind
DeferReason = _ticket_plan_mod.DeferReason
LifecycleIntent = _ticket_plan_mod.LifecycleIntent
ParityDelta = _ticket_plan_mod.ParityDelta
ParityReport = _ticket_plan_mod.ParityReport
# Modes that perform NO writes (canonical ``mode.MODE_CAPS == 0``:
# ``dry-run``). A mutation planned under one of these is mode-capped (``safety_aborted``)
# pre-effect. The WRITE-ENABLED modes — ``bootstrap-strict``, ``bootstrap-throttle`` (finite
# blast-radius caps) and ``live`` (uncapped) — are NOT mode-capped here; their numeric caps
# are a separate apply-layer concern. Sourced from ``mode.MODE_CAPS`` so this stays aligned
# with the canonical Mode vocabulary instead of hard-coding a string set.
_NO_WRITE_MODES: frozenset[str] = frozenset(
    m.value for m, cap in _mode_mod.MODE_CAPS.items() if cap == 0
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
    """The granular skip cause of every mutation carrying a truthy ``payload['skip']``, in
    ``muts`` order (empty when none). Any non-empty value classifies the plan as ``skipped``
    — an unrecognized/mistyped cause is recorded verbatim rather than silently ignored."""
    return tuple(str(m.payload["skip"]) for m in muts if m.payload.get("skip"))


def scoped_selection_ids(selection_ids: set[str], typed_mutations: list) -> list[str]:
    """The selection id set ``plan_pass`` scopes against, expanded to include the BOUND
    JIRA KEYS of the selected local ids — mirroring the legacy
    ``reconcile_helpers._build_filter_target_set`` (LOCAL IDS ∪ bound JIRA KEYS).

    A bound issue's outbound update/delete carries ``target = jira_key`` (run_differs'
    OM→typed conversion), while ``--only`` / ``--except`` select by LOCAL id. Without this
    expansion ``_scope_excluded`` compares a jira-key target against a local-id-only set and
    wrongly ``scope_deferred``s the in-scope mutation, so the live coordinator+fuse reroute
    drops the write (bug af1b). The bound keys are read purely from the mutations' own
    provenance — no ``binding_store`` I/O — so the shadow plan stays side-effect-free. For
    ``--except`` the same expansion is correct: an excepted ticket's update targets its jira
    key, which must therefore also land in the excluded set. Keys are derived only from
    mutations present in this pass — the sole targets ``_scope_excluded`` ever checks — so a
    selected id with no mutation contributes nothing (it has no target to scope).
    """
    selected = set(selection_ids)
    scoped = set(selected)
    if not selected:
        return sorted(scoped)
    for m in typed_mutations:
        prov = getattr(m, "provenance", None) or {}
        local_id = prov.get("local_id") if isinstance(prov, Mapping) else None
        if local_id not in selected:
            continue
        jira_key = prov.get("jira_key") if isinstance(prov, Mapping) else None
        if jira_key:
            scoped.add(jira_key)
        target = getattr(m, "target", None)
        if target:
            scoped.add(target)
    return sorted(scoped)


def _scope_excluded(target: str, selection: Mapping[str, Any]) -> bool:
    """True when ``selection`` scopes the pass OUT of ``target``. The canonical selection
    vocabulary (request.py / reconcile_helpers.narrow_selection_inputs) is:
      - ``"only"``  → keep only ids IN the set, so a target NOT in the set is excluded;
      - ``"except"`` → keep ids NOT in the set (inverted), so a target IN the set is excluded;
      - ``None`` / any other kind → no selection narrowing (nothing scope-excluded)."""
    kind = selection.get("kind")
    ids = set(selection.get("ids") or [])
    if kind == "only":
        return target not in ids
    if kind == "except":
        return target in ids
    return False


def _mode_capped(muts: Sequence[Any], mode: str) -> bool:
    """True when the pass runs in a NO-WRITE mode (``dry-run``/preview) that
    aborts any planned mutation pre-effect. Write-enabled modes (``bootstrap-strict``,
    ``bootstrap-throttle``, ``live``) are NOT mode-capped — bootstrap warm-up modes permit
    outbound writes, so treating them as capping would wrongly defer real work."""
    return mode in _NO_WRITE_MODES and bool(muts)


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


def _existing_identities(
    binding_view: Mapping[str, Any],
    local_snapshot: Mapping[str, Any],
    remote_snapshot: Mapping[str, Any],
) -> set[str]:
    """Identities that already exist (so can satisfy a prerequisite without a fresh create)."""
    return set(binding_view) | set(local_snapshot) | set(remote_snapshot)


def _effective_create_providers(
    create_targets: set[str],
    base_excluded: set[str],
    deps_by_target: Mapping[str, Sequence[str]],
    existing: set[str],
) -> set[str]:
    """The create targets that will ACTUALLY be created this pass — i.e. can satisfy a
    dependent's ``requires_create`` / ``requires_parent``. A create target excluded before
    the dependency stage (scope / skip / mode-cap / limit) cannot provide its identity, and
    a create target that is itself dependency-blocked cannot either; the latter cascades, so
    providers are shrunk to a fixpoint (monotonically, hence terminating)."""
    providers = {t for t in create_targets if t not in base_excluded}
    changed = True
    while changed:
        changed = False
        available = existing | providers
        for candidate in tuple(providers):
            deps = deps_by_target.get(candidate, ())
            if deps and any(r not in available for r in deps):
                providers.discard(candidate)
                changed = True
    return providers


def _dependency_blocked(deps: Sequence[str], available: set[str]) -> bool:
    """True when the target declares prerequisites and at least one is not ``available``
    (neither already-existing nor an effective create provider this pass)."""
    return bool(deps) and any(r not in available for r in deps)


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
    # Stage 3 pre-work: a prerequisite is satisfiable only by an already-existing identity or
    # by a create target that will ACTUALLY be created — one excluded by scope/skip/mode-cap
    # (``early``) or the limit cannot provide its identity, so it must not satisfy dependents.
    base_excluded = {t for t in sorted_targets if early[t] is not None} | limited
    existing = _existing_identities(binding_view, local_snapshot, remote_snapshot)
    providers = _effective_create_providers(create_targets, base_excluded, deps_by_target, existing)
    available = existing | providers

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
            available,
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
    available: set[str],
) -> tuple[Any, Any, tuple[str, ...], tuple[Any, ...], tuple[Any, ...]]:
    """Resolve one target's final ``(disposition, defer_reason, extra_diagnostics,
    mutations, intents)`` following the rule precedence. Only rule 5 empties mutations;
    only rule 6 (mutate) derives intents."""
    if early is not None:
        disposition, defer_reason, extra = early
        return disposition, defer_reason, extra, muts, ()
    if target in limited:
        return PlanDisposition.defer, DeferReason.safety_aborted, (), muts, ()
    if _dependency_blocked(deps, available):
        return PlanDisposition.defer, DeferReason.dependency_deferred, (), (), ()
    return PlanDisposition.mutate, None, (), muts, _derive_intents(muts, target, version)


def _mutation_triples(mutations: Sequence[Any]) -> set[tuple[str, str, str]]:
    """The ``(target, direction, action)`` identity triples of ``mutations`` that carry a
    ``.target`` attribute (legacy dict mutations without one are skipped)."""
    return {
        (m.target, m.direction.value, m.action.value)
        for m in mutations
        if getattr(m, "target", None) is not None
    }


def _disposition_deltas(ticket_plans: Sequence[Any]) -> list[Any]:
    """One ParityDelta per non-``mutate`` plan, recording its disposition and defer reason
    as bounded, structured markers (never a raw payload — AC7)."""
    deltas: list[Any] = []
    for plan in ticket_plans:
        if plan.disposition.value == "mutate":
            continue
        defer = plan.defer_reason.value if plan.defer_reason else "none"
        deltas.append(
            ParityDelta(
                identity=plan.identity,
                observation_version=plan.observation_version,
                kind="disposition_" + plan.disposition.value,
                fields=(
                    f"disposition={plan.disposition.value}",
                    f"defer_reason={defer}",
                ),
            )
        )
    return deltas


def _triple_deltas(triples: set[tuple[str, str, str]], kind: str, version: Any) -> list[Any]:
    """One ParityDelta per triple (sorted for deterministic ordering) with the given
    ``kind`` and a single bounded ``direction/action`` field."""
    return [
        ParityDelta(
            identity=target,
            observation_version=version,
            kind=kind,
            fields=(f"{direction}/{action}",),
        )
        for target, direction, action in sorted(triples)
    ]


def compare_parity(
    legacy_mutations: Sequence[Any],
    ticket_plans: Sequence[Any],
    *,
    approved_identities: frozenset[str] = frozenset(),
) -> Any:
    """Purely compare the shadow ``ticket_plans`` against the legacy mutation list.

    Emits a ``ParityReport`` whose deltas record (a) every plan that did NOT resolve to
    ``mutate``, (b) legacy mutations dropped by the shadow layer, and (c) mutations the
    shadow layer added. Delta ``fields`` are bounded, structured strings only — never a
    raw mutation payload or provenance (AC7). PURE: no I/O, no clock.
    """
    version = ticket_plans[0].observation_version if ticket_plans else None
    legacy_set = _mutation_triples(legacy_mutations)
    shadow_set = {
        (m.target, m.direction.value, m.action.value)
        for plan in ticket_plans
        for m in plan.mutations
    }
    deltas = _disposition_deltas(ticket_plans)
    deltas += _triple_deltas(legacy_set - shadow_set, "mutation_dropped", version)
    deltas += _triple_deltas(shadow_set - legacy_set, "mutation_added", version)
    deltas_t = tuple(deltas)
    unexpected = tuple(d for d in deltas_t if d.identity not in approved_identities)
    return ParityReport(deltas=deltas_t, unexpected=unexpected, matched=not unexpected)
