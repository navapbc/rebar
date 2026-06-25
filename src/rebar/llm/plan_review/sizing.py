"""Size-handling, budget cap, model escalation, and chunk checkpointing (child ca03).

Extracted from the orchestrator (call-graph seam: the "fit the review into a budget +
context window" cluster). Owns:

* the model-by-window escalation ladder + :func:`largest_window_tokens` (P8 budget);
* :func:`centrality` (blast-radius at plan time) + :func:`plan_budget_cap`;
* :func:`pass1_with_ladder` — the runtime size ladder (batch → one-criterion-per-call
  → escalate model → too-big failure finding; content never chunked);
* :func:`shed_to_budget` — cap-hit shedding of the lowest-priority AGENT/overlay
  criteria first (→ INDETERMINATE);
* :func:`load_checkpoint` / :func:`save_checkpoint` — chunk-atomic checkpointing so an
  interrupted/restarted review RESUMES completed Pass-1 chunks instead of re-paying
  for them (keyed by the ticket's MATERIAL fingerprint, so a material edit invalidates
  the cache).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner

from . import passes, registry
from .det_floor import PlanContext

# Model-by-window escalation ladder (estimated tokens → the smallest model whose
# window fits; escalate up on a context-limit signal).
MODEL_LADDER = (
    ("claude-haiku-4-5", 200_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
)

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
    base = DEFAULT_BUDGET_CAP_USD
    raw = os.environ.get("REBAR_PLAN_REVIEW_BUDGET", "").strip()
    if raw:
        try:
            base = float(raw)
        except ValueError:
            pass
    return round(base * (1.0 + ctx.centrality), 4)


def largest_window_tokens(model: str | None) -> int:
    """The largest context window the gate can escalate to for P8's budget. A model on
    the ladder uses the max window at-or-above it; a model SMALLER than the ladder top
    caps P8 there so P8 doesn't under-block. Unknown/absent → the ladder maximum."""
    if model:
        for name, _window in MODEL_LADDER:
            if name in model:
                idx = [n for n, _ in MODEL_LADDER].index(name)
                return max(w for _, w in MODEL_LADDER[idx:])
    return MODEL_LADDER[-1][1]


def is_context_limit_error(exc: Exception) -> bool:
    """Heuristic: does ``exc`` look like a provider context-window/too-many-tokens
    error (vs an unrelated failure)? Matches common phrasings across providers."""
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "context",
            "too many tokens",
            "maximum context",
            "context_length",
            "prompt is too long",
            "input length",
            "exceeds the maximum",
            "token limit",
        )
    )


def models_at_or_above(model: str | None) -> list[str]:
    """The model ladder from ``model`` upward (by window), for runtime escalation.
    Unknown/absent model → the whole ladder."""
    names = [n for n, _w in MODEL_LADDER]
    if model:
        for i, n in enumerate(names):
            if n in model:
                return names[i:]
    return list(names)


def pass1_with_ladder(
    runner: Runner,
    cfg: LLMConfig,
    plan: str,
    chunk: list[dict],
    agentic: bool,
    events: list[str],
) -> list[dict[str, Any]]:
    """Run a Pass-1 finder call with the SIZE-HANDLING LADDER (ca03 AC4/AC6):

    1. run the criteria BATCH (chunk) at the configured model;
    2. on a context-limit signal, fall back to ONE CRITERION PER CALL (full content,
       minimal rubric — content is never chunked);
    3. on a context-limit signal for a single criterion, ESCALATE up the model ladder;
    4. if a single criterion still won't fit at the largest window, emit a FAILURE
       FINDING (P8: the ticket is too big to review in full — reduce/decompose it).

    Non-context errors drop the unit's findings (never abort the review). ``events``
    accumulates a human-readable ladder trace for the coverage record."""
    try:
        return passes.pass1_chunk(runner, cfg, plan=plan, chunk=chunk, agentic=agentic)
    except Exception as exc:  # noqa: BLE001 — broad to inspect is_context_limit_error(exc); a non-context failure drops findings, a context error falls through to the size-ladder
        if not is_context_limit_error(exc):
            return []  # unrelated failure → drop this unit's findings (never abort)

    if len(chunk) > 1:
        events.append(f"batch of {len(chunk)} hit the context limit → one-criterion-per-call")
    out: list[dict[str, Any]] = []
    for crit in chunk:
        produced = False
        for model in models_at_or_above(cfg.model):
            try:
                out.extend(
                    passes.pass1_chunk(
                        runner, replace(cfg, model=model), plan=plan, chunk=[crit], agentic=agentic
                    )
                )
                if model != cfg.model:
                    events.append(f"{crit['id']}: escalated to {model}")
                produced = True
                break
            except Exception as exc:  # noqa: BLE001 — broad to inspect is_context_limit_error(exc); a non-size failure drops, a context error escalates to the next model
                if not is_context_limit_error(exc):
                    produced = True  # non-size failure → drop, don't escalate
                    break
                continue  # context limit at this model → escalate to the next
        if not produced:
            events.append(f"{crit['id']}: too big even at the largest model → failure finding")
            out.append(
                {
                    "finding": (
                        "The ticket is too large to review in full even for a single criterion "
                        f"({crit['id']}) at the largest context window."
                    ),
                    "criteria": [crit["id"]],
                    "location": "(whole plan)",
                    "evidence": ["content exceeds the largest model window even one-at-a-time"],
                    "scenarios": [],
                    "impact": "The plan cannot be reviewed whole; reduce/decompose it (P8/G5).",
                    "checklist_item": "- [ ] Reduce/decompose the ticket so it fits a review pass.",
                    "suggested_fix": "Split the ticket into smaller children.",
                    "tier": "DET",
                    "_too_big": True,
                }
            )
    return out


def shed_to_budget(
    ctx: PlanContext,
    chunks: list,
    agent: list[dict],
    container: list[dict],
    coverage: dict[str, Any],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Shed the lowest-priority AGENT/overlay criteria first when projected spend
    exceeds the per-plan budget cap. Returns (kept_agent, kept_container, shed). The
    cheap single-turn chunks + the DET floor always run; we shed only the 85× AGENT
    criteria — overlays (T*) before the core code-grounding set, then container."""
    cap = plan_budget_cap(ctx)
    n_children = max(1, len(ctx.children))

    def project(ag: list[dict], cont: list[dict]) -> float:
        return round(
            len(chunks) * COST_SINGLE_TURN_USD
            + len(ag) * COST_AGENT_USD
            + len(cont) * n_children * COST_AGENT_USD,
            4,
        )

    projected_initial = project(agent, container)
    agent = list(agent)
    container = list(container)
    shed: list[dict] = []
    overlay_agent = [c for c in agent if registry.is_overlay(c["id"])]
    core_agent = [c for c in agent if not registry.is_overlay(c["id"])]
    shed_queue = (
        [("agent", c) for c in overlay_agent]
        + [("container", c) for c in container]
        + [("agent", c) for c in core_agent]
    )
    while project(agent, container) > cap and shed_queue:
        kind, c = shed_queue.pop(0)
        c = {**c, "_tier": "AGENT"}
        shed.append(c)
        if kind == "container":
            container = [x for x in container if x["id"] != c["id"]]
        else:
            agent = [x for x in agent if x["id"] != c["id"]]
    coverage["budget"] = {
        "cap_usd": cap,
        "centrality": ctx.centrality,
        "projected_usd_initial": projected_initial,
        "projected_usd_final": project(agent, container),
        "shed": [c["id"] for c in shed],
    }
    return agent, container, shed


# ── chunk-atomic checkpointing (resume completed Pass-1 chunks) ──────────────────
def _checkpoint_dir(ctx: PlanContext) -> Path | None:
    """The git-ignored per-ticket checkpoint cache dir (``.rebar/cache/plan-review/``),
    or None when there is no repo root to anchor it."""
    if not ctx.repo_root:
        return None
    return Path(ctx.repo_root) / ".rebar" / "cache" / "plan-review" / ctx.ticket_id


def _checkpoint_key(material: str, chunk: list[dict], model: str | None, agentic: bool) -> str:
    """A content key for a chunk's checkpoint: the ticket MATERIAL fingerprint (so a
    material edit invalidates it) + the chunk's criterion ids + model + tier."""
    ids = ",".join(sorted(c["id"] for c in chunk))
    basis = f"{material}|{ids}|{model}|{int(agentic)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def load_checkpoint(
    ctx: PlanContext, material: str, chunk: list[dict], model: str | None, agentic: bool
) -> list[dict[str, Any]] | None:
    """Return a completed chunk's checkpointed findings if present + matching (resume),
    else None. Best-effort: any read/parse error → None (re-run the chunk)."""
    d = _checkpoint_dir(ctx)
    if d is None:
        return None
    try:
        path = d / f"{_checkpoint_key(material, chunk, model, agentic)}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — checkpoint read is a best-effort resume optimization; any failure ⇒ no cached result (recompute)
        return None
    return None


def save_checkpoint(
    ctx: PlanContext,
    material: str,
    chunk: list[dict],
    model: str | None,
    agentic: bool,
    findings: list[dict[str, Any]],
) -> bool:
    """Persist a chunk's findings ATOMICALLY (tmp + rename) so a restarted review
    resumes it. Best-effort: any write error → False (the review still proceeds)."""
    d = _checkpoint_dir(ctx)
    if d is None:
        return False
    try:
        d.mkdir(parents=True, exist_ok=True)
        key = _checkpoint_key(material, chunk, model, agentic)
        path = d / f"{key}.json"
        tmp = d / f".tmp-{key}.json"
        tmp.write_text(json.dumps(findings, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:  # noqa: BLE001 — checkpoint write is a best-effort resume optimization; any failure ⇒ not cached (the review still proceeds)
        return False


__all__ = [
    "MODEL_LADDER",
    "COST_SINGLE_TURN_USD",
    "COST_AGENT_USD",
    "DEFAULT_BUDGET_CAP_USD",
    "centrality",
    "plan_budget_cap",
    "largest_window_tokens",
    "is_context_limit_error",
    "models_at_or_above",
    "pass1_with_ladder",
    "shed_to_budget",
    "load_checkpoint",
    "save_checkpoint",
]
