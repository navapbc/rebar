"""Per-plan COST MODEL, budget cap, and the container bin-packer (extracted from
:mod:`.sizing` along the call-graph seam it already formed).

:func:`shed_to_budget` is the cluster's root: it reads the cost constants, asks
:func:`plan_budget_cap` (which scales the configured base by the plan's
:func:`centrality`) for the cap, and computes its never-shed container floor with the
same :func:`pack_container_bins` / :func:`container_budget` pair the container fan-out
uses. Nothing here calls back into :mod:`.sizing`; the names are re-exported there so
the historical ``sizing.<name>`` call sites are unchanged.
"""

from __future__ import annotations

from typing import Any

from . import det_floor, registry
from .det_floor import PlanContext


def _child_tokens(child: dict[str, Any]) -> int:
    """Estimated tokens for one WHOLE child (title + description) — the unit the
    container bin-packer sums (a child is NEVER chunked; ca03)."""
    return det_floor.est_tokens(f"{child.get('title', '')}\n{child.get('description', '')}")


def container_budget(largest_window_tokens: int) -> int:
    """The per-call token budget the container bin-packer fits (parent + all packed
    children) under — the SAME P8 window budget used elsewhere (model window × headroom
    − output reserve)."""
    return int(largest_window_tokens * det_floor.P8_HEADROOM) - det_floor.P8_OUTPUT_RESERVE_TOKENS


def pack_container_bins(
    children: list[dict[str, Any]], parent_tokens: int, budget: int
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Greedily bin-pack WHOLE children for the container fan-out (story 1762).

    Each bin is ONE merged container call: ``parent_tokens + Σ(child tokens in the bin)``
    must stay ≤ ``budget`` (so the parent + every packed child fit the window together,
    each WHOLE — never chunked, ca03). Returns ``(bins, oversized)`` where ``bins`` is a
    list of child-lists (small children packed together; a large parent+child pairing
    keeps its own single-child bin), and ``oversized`` is the children whose
    ``parent + that child ALONE`` already exceeds ``budget`` — the single-child fallback
    that becomes the existing 'too big → reduce the ticket' failure finding."""
    bins: list[list[dict[str, Any]]] = []
    oversized: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []
    cur_tokens = parent_tokens
    for child in children:
        ct = _child_tokens(child)
        if parent_tokens + ct > budget:
            oversized.append(child)  # too big even alone → single-child too-big finding
            continue
        if cur and cur_tokens + ct > budget:
            bins.append(cur)
            cur, cur_tokens = [], parent_tokens
        cur.append(child)
        cur_tokens += ct
    if cur:
        bins.append(cur)
    return bins, oversized


# Per-plan BUDGET CAP tiers (experiment-grounded; config-overridable via
# REBAR_PLAN_REVIEW_BUDGET). DET ~free, single-turn ~$0.006 cached, AGENT ~$0.12 (≈85×).
COST_SINGLE_TURN_USD = 0.006
COST_AGENT_USD = 0.12
DEFAULT_BUDGET_CAP_USD = 2.0


def centrality(state: dict[str, Any], children: list[dict[str, Any]]) -> float:
    """Blast-radius signal ∈ [0,1] computed at plan time from the ticket graph: how
    many tickets DEPEND ON this one (incoming blocks / depends_on) + how many children
    it has. A central, high-fan-in plan earns more scrutiny + budget. Saturating
    (≈1.0 by ~10 dependents)."""
    deps = state.get("deps", []) or []
    dependents = sum(1 for d in deps if d.get("relation") in ("blocks", "depends_on"))
    blast = dependents + len(children)
    return round(min(1.0, blast / 10.0), 3)


def plan_budget_cap(ctx: PlanContext) -> float:
    """The per-plan budget cap in USD: a base cap scaled by centrality (a central plan
    earns up to 2× scrutiny), overridable by ``REBAR_PLAN_REVIEW_BUDGET`` (the base,
    before centrality scaling)."""
    from rebar import config as _config

    base = _config.resolve_plan_review_budget(DEFAULT_BUDGET_CAP_USD)
    return round(base * (1.0 + ctx.centrality), 4)


def shed_to_budget(
    ctx: PlanContext,
    chunks: list,
    agent: list[dict],
    container: list[dict],
    coverage: dict[str, Any],
    cap_override: float | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Shed the lowest-priority AGENT/overlay criteria first when projected spend
    exceeds the per-plan budget cap. Returns (kept_agent, kept_container, shed).

    CONTAINER CRITERIA (G3/G4) ARE NEVER SHED (story ba7e). Shedding a container
    criterion marks it INDETERMINATE — i.e. it DROPS child-coverage/consistency on
    exactly the large, central epics where those cross-child audits matter most, the
    fidelity regression the epic AC forbids. So the budget cap bounds ONLY the
    sheddable single-turn + AGENT/overlay spend; the container fan-out is a FIXED cost
    floor recorded for observability but never traded away. (This is the correct fix —
    correcting the COST MODEL — rather than inverting the centrality scaling of the cap,
    which would have shed G3/G4 first.) Within the sheddable set we still shed overlays
    (T*) before the core code-grounding set.

    ``cap_override`` is the caller's explicit per-plan cap in USD, used VERBATIM — it is NOT
    centrality-scaled. That is the whole point of the seam: ``REBAR_PLAN_REVIEW_BUDGET`` is read
    by :func:`plan_budget_cap` as the BASE and then scaled by centrality, so it can never express
    "this run costs at most $X". Selection is on ``is None``, not falsiness, so an explicit
    ``0.0`` is a real cap ("shed everything sheddable") rather than a request for the computed
    one."""
    cap = plan_budget_cap(ctx) if cap_override is None else float(cap_override)

    def project_sheddable(ag: list[dict]) -> float:
        """Projected SHEDDABLE spend (the single-turn chunks + AGENT/overlay criteria)
        — the only spend the cap governs. The container fan-out is excluded: it is never
        shed, so including it would only force over-aggressive shedding of agent criteria
        for a cost we always pay regardless."""
        return round(len(chunks) * COST_SINGLE_TURN_USD + len(ag) * COST_AGENT_USD, 4)

    projected_initial = project_sheddable(agent)
    agent = list(agent)
    container = list(container)  # never shed — returned unchanged
    shed: list[dict] = []
    overlay_agent = [c for c in agent if registry.is_overlay(c["id"])]
    core_agent = [c for c in agent if not registry.is_overlay(c["id"])]
    shed_queue = [("agent", c) for c in overlay_agent] + [("agent", c) for c in core_agent]
    while project_sheddable(agent) > cap and shed_queue:
        _kind, c = shed_queue.pop(0)
        c = {**c, "_tier": "AGENT"}
        shed.append(c)
        agent = [x for x in agent if x["id"] != c["id"]]
    # The container fan-out is a fixed floor, never shed — recorded so the cap's "bounds
    # only overlay/agent spend" posture, and the unavoidable container cost, are both
    # observable. Story 98c6 MERGED all container criteria into ONE call per child (not 2N);
    # story 1762 then BIN-PACKS small children, so the real floor is the number of PACKED
    # BINS (< N when children pack), computed with the same packer the fan-out uses.
    if container and ctx.children:
        bins, _oversized = pack_container_bins(
            ctx.children,
            det_floor.est_tokens(ctx.plan_text),
            container_budget(ctx.largest_window_tokens),
        )
        container_calls = len(bins)
    else:
        container_calls = 0
    container_floor_usd = round(container_calls * COST_AGENT_USD, 4)
    coverage["budget"] = {
        "cap_usd": cap,
        # Which cap governed this run: an explicit caller override, or the centrality-scaled
        # computed one. `centrality` below is still recorded either way, but it did NOT scale
        # an overridden cap — so without this the two are indistinguishable in the journal.
        "cap_source": "computed" if cap_override is None else "override",
        "centrality": ctx.centrality,
        "projected_usd_initial": projected_initial,
        "projected_usd_final": project_sheddable(agent),
        "container_floor_usd": container_floor_usd,
        "container_never_shed": True,
        "shed": [c["id"] for c in shed],
    }
    return agent, container, shed
