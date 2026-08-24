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

    plans: list[Any] = []
    for target in sorted(grouped):
        target_mutations = tuple(sorted(grouped[target], key=_mutation_sort_key))
        plans.append(
            TicketPlan(
                identity=target,
                mutations=target_mutations,
                diagnostics=tuple(diagnostics_by_target.get(target, ())),
                disposition=PlanDisposition.mutate,
                observation_version=observation.version,
                payload=plan_payload_by_target.get(target, {}),
            )
        )

    return observation, tuple(plans)
