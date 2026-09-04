"""Size-handling and model escalation for the plan-review gate (child ca03).

Extracted from the orchestrator (call-graph seam: the "fit the review into a budget +
context window" cluster). Owns:

* the model-by-window escalation ladder + :func:`largest_window_tokens` (P8 budget) and
  :func:`models_at_or_above` / :func:`escalation_rungs`;
* the prompt-budget PACKERS that resolve their window through this module's
  :func:`largest_window_tokens` — :func:`verify_request_chunks`,
  :func:`pack_prerequisite_bins`, :func:`pack_prerequisite_verifier_bins`;
* :func:`usage_record` (one per COMPLETED call) and :func:`is_context_limit_error`;
* :func:`pass1_with_ladder` — the runtime size ladder (batch → one-criterion-per-call
  → escalate model → too-big failure finding; content never chunked).

Two clusters that already formed their own call-graph seams live in siblings and are
RE-EXPORTED here so every historical ``sizing.<name>`` call site is unchanged:

* :mod:`.budget` — the cost model, :func:`centrality` / :func:`plan_budget_cap`, the
  container bin-packer, and :func:`shed_to_budget`;
* :mod:`.checkpoints` — :func:`checkpoint_identity` / :func:`load_checkpoint` /
  :func:`save_checkpoint`, the envelope-based chunk-atomic checkpointing that lets an
  interrupted/restarted review RESUME completed Pass-1 chunks.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from rebar.llm import failure
from rebar.llm.config import LLMConfig, infer_provider
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.model_classes import MODEL_WINDOW_LADDER as MODEL_LADDER
from rebar.llm.runner import Runner

from . import det_floor, passes
from .budget import (
    COST_AGENT_USD,
    COST_SINGLE_TURN_USD,
    DEFAULT_BUDGET_CAP_USD,
    centrality,
    container_budget,
    pack_container_bins,
    plan_budget_cap,
    shed_to_budget,
)
from .checkpoints import (  # noqa: F401 — private re-exports: `pass1` and the tests reach them as `sizing.<name>`
    _checkpoint_dir,
    _discovery_unit_plan,
    _unit_id,
    checkpoint_identity,
    load_checkpoint,
    save_checkpoint,
)


@dataclass(frozen=True)
class PrerequisiteBlock:
    """One authoritative whole prerequisite plan block; never split by packing."""

    canonical_id: str
    rendered_text: str
    relation_kind: str = "depends_on"


@dataclass(frozen=True)
class PrerequisiteVerificationBlock:
    """One focused finding plus its authoritative whole prerequisite context."""

    canonical_id: str
    findings: tuple[dict[str, Any], ...]
    rendered_text: str


# Model-by-window escalation ladder (estimated tokens → the smallest model whose
# window fits; escalate up on a context-limit signal). The table now lives in
# model_classes.py (bug 8eb3) and is re-exported above under its historical name.


def largest_window_tokens(model: str | None) -> int:
    """The largest context window the gate can escalate to for P8's budget. A model on
    the ladder uses the max window at-or-above it; a model SMALLER than the ladder top
    caps P8 there so P8 doesn't under-block.

    ABSENT model → the ladder maximum: with nothing configured the gate runs its own default,
    which IS a ladder model. But a model the ladder cannot LOCATE → the ladder MINIMUM (bug
    48b3). The rung lookup is a substring match against MODEL_LADDER's bare Anthropic family
    names, so any other family matched nothing and inherited the ladder maximum — overstating
    the window, which makes P8 UNDER-block: material that will not fit is judged to fit and the
    review truncates instead of telling the author to decompose. The two error directions are
    not symmetric — under-blocking is silent, over-blocking is loud and actionable — so with no
    window entry for the operator's model the honest default is the conservative one. It is also
    safe for the other consumers (:func:`verify_request_chunks`, :func:`pack_prerequisite_bins`,
    :func:`pack_prerequisite_verifier_bins` all DIVIDE by this number, so a smaller value yields
    more, smaller chunks — never an overflow)."""
    if model:
        for name, _window in MODEL_LADDER:
            if name in model:
                idx = [n for n, _ in MODEL_LADDER].index(name)
                return max(w for _, w in MODEL_LADDER[idx:])
        return min(w for _, w in MODEL_LADDER)
    return MODEL_LADDER[-1][1]


# ── Pass-2 verify token-budget chunking + the model-max output-budget rule ─────────
# The model-max output-budget rule (bug 30a2) is SHARED across every review workflow, so
# its single source is the review kernel (`rebar.llm.review_kernel.verify`, beside the
# analogous cross-gate `resolve_verifier_model` rule); it is re-exported here for the
# historical `sizing.<name>` call sites, exactly like the chunking constants below.
# The chunking ALGORITHM + constants are owned by the shared review kernel
# (`rebar.llm.review_kernel.verify`) as the single source (epic vivid-gang-day WS2). The
# constants are re-exported here for the historical `sizing.<name>` call sites;
# `verify_request_chunks` is a thin plan-review wrapper that supplies the model WINDOW
# (`largest_window_tokens(model)` — model escalation baked in) + the token ESTIMATOR
# (`det_floor.est_tokens`), the two infra inputs the kernel chunker injects.
from rebar.llm.review_kernel.verify import (  # noqa: E402,F401
    DEFAULT_VERIFY_WINDOW_HEADROOM,
    MODEL_MAX_OUTPUT_TOKENS,
    PER_FINDING_VERIFY_TOKENS,
    VERIFY_SYSTEM_RESERVE_TOKENS,
    max_output_cfg,
    model_max_output_tokens,
)
from rebar.llm.review_kernel.verify import (  # noqa: E402
    verify_request_chunks as _kernel_verify_request_chunks,
)


def verify_request_chunks(
    findings: list[dict[str, Any]],
    *,
    model: str | None,
    headroom: float = DEFAULT_VERIFY_WINDOW_HEADROOM,
    per_finding_out_tokens: int = PER_FINDING_VERIFY_TOKENS,
) -> tuple[list[list[tuple[int, dict[str, Any]]]], list[int]]:
    """Split ``findings`` into token-budgeted Pass-2 verify chunks (the plan-review wrapper
    over :func:`rebar.llm.review_kernel.verify.verify_request_chunks`). Supplies the model
    WINDOW (``largest_window_tokens(model)`` — model escalation baked in: the largest window
    at-or-above ``model``) + the token ESTIMATOR (``det_floor.est_tokens``). The common case
    returns ONE chunk == the whole enumerated list (no behavior change); a finding too big to
    verify even alone is returned in ``omitted_indices`` → ``pass3_decide(None)`` →
    INDETERMINATE. See the kernel for the fit-test math."""
    return _kernel_verify_request_chunks(
        findings,
        window_tokens=largest_window_tokens(model),
        est_tokens=det_floor.est_tokens,
        headroom=headroom,
        per_finding_out_tokens=per_finding_out_tokens,
    )


def pack_prerequisite_bins(
    blocks: list[PrerequisiteBlock] | tuple[PrerequisiteBlock, ...],
    *,
    subject_plan: str,
    system_prompt: str,
    model: str | None,
    per_block_output_tokens: int = PER_FINDING_VERIFY_TOKENS,
    headroom: float = DEFAULT_VERIFY_WINDOW_HEADROOM,
) -> tuple[list[list[PrerequisiteBlock]], list[PrerequisiteBlock]]:
    """Greedily pack stable, whole prerequisite blocks within the actual prompt budget."""
    limit = int(largest_window_tokens(model) * headroom)
    fixed = (
        det_floor.est_tokens(system_prompt)
        + det_floor.est_tokens(subject_plan)
        + VERIFY_SYSTEM_RESERVE_TOKENS
    )
    bins: list[list[PrerequisiteBlock]] = []
    oversized: list[PrerequisiteBlock] = []
    current: list[PrerequisiteBlock] = []
    used = fixed
    for block in sorted(blocks, key=lambda item: item.canonical_id):
        charge = det_floor.est_tokens(block.rendered_text) + per_block_output_tokens
        if fixed + charge > limit:
            oversized.append(block)
            continue
        if current and used + charge > limit:
            bins.append(current)
            current, used = [], fixed
        current.append(block)
        used += charge
    if current:
        bins.append(current)
    return bins, oversized


def pack_prerequisite_verifier_bins(
    records: list[PrerequisiteVerificationBlock] | tuple[PrerequisiteVerificationBlock, ...],
    *,
    subject_plan: str,
    system_prompt: str,
    model: str | None,
    per_finding_output_tokens: int = PER_FINDING_VERIFY_TOKENS,
    headroom: float = DEFAULT_VERIFY_WINDOW_HEADROOM,
) -> tuple[list[list[PrerequisiteVerificationBlock]], list[PrerequisiteVerificationBlock]]:
    """Pack whole focused verification records; plan text and records are indivisible."""
    limit = int(largest_window_tokens(model) * headroom)
    fixed = (
        det_floor.est_tokens(system_prompt)
        + det_floor.est_tokens(subject_plan)
        + VERIFY_SYSTEM_RESERVE_TOKENS
    )
    bins: list[list[PrerequisiteVerificationBlock]] = []
    oversized: list[PrerequisiteVerificationBlock] = []
    current: list[PrerequisiteVerificationBlock] = []
    used = fixed
    for record in sorted(records, key=lambda item: item.canonical_id):
        charge = det_floor.est_tokens(record.rendered_text) + (
            per_finding_output_tokens * len(record.findings)
        )
        if fixed + charge > limit:
            oversized.append(record)
            continue
        if current and used + charge > limit:
            bins.append(current)
            current, used = [], fixed
        current.append(record)
        used += charge
    if current:
        bins.append(current)
    return bins, oversized


# ── per-call usage records (story d52a) ────────────────────────────────────────────
# The token fields a per-call usage record carries (mirrors runner._extract_usage minus
# `requests`, which is recorded separately).
USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def usage_record(criteria: list[str], usage: dict[str, Any] | None) -> dict[str, Any]:
    """Build one per-CALL usage record from a runner result's ``_usage`` dict:
    ``{criteria, requests, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens}``. One record IS one completed call — there is deliberately
    NO ``llm_calls`` field on a record; the per-criterion ``llm_calls`` value is
    DERIVED downstream as the count of records covering that criterion (see
    :func:`rebar.llm.plan_review.pass1.aggregate_usage`).

    A missing/empty ``usage`` (a runner that attaches no ``_usage``, e.g. the
    ``FakeRunner``) degrades to all-zero fields — a zero contribution, never an
    error. An attempt that RAISES returns no result dict at all, so its usage is
    unrecoverable from the runner API — raising attempts get NO record (callers
    simply never mint one)."""
    u = usage or {}
    record = {
        "criteria": list(criteria),
        "requests": int(u.get("requests", 0) or 0),
        **{f: int(u.get(f, 0) or 0) for f in USAGE_TOKEN_FIELDS},
    }
    # Ticket 81ca: carry the run-shape's ``distinct_fetches`` (the repository paths the agentic
    # pass actually opened) through to the aggregate, where it becomes the signed read-set.
    # Additive — absent from the record when the runner attached none, so every existing
    # consumer (which reads named fields) is unchanged.
    fetches = u.get("distinct_fetches")
    if isinstance(fetches, list) and fetches:
        record["distinct_fetches"] = [f for f in fetches if isinstance(f, dict)]
    return record


def is_context_limit_error(exc: Exception) -> bool:
    """Heuristic: does ``exc`` look like a provider context-window/too-many-tokens
    error (vs an unrelated failure)? Matches common phrasings across providers.

    The phrasings are OWNED by :mod:`rebar.llm.failure` and read from it at call time
    (story fcb7): this predicate and ``failure``'s CHANGE_INPUT classifier judge the SAME
    wire error, so a second copy here would let a fix to one silently leave the other
    stale. The dependency runs one way only — ``failure`` still imports nothing from
    ``plan_review``, which is the boundary its own comment records.
    """
    msg = str(exc).lower()
    return any(s in msg for s in failure._CONTEXT_LEN_HINTS)


# Ladder rung FAMILY substring → the model class that rung stands for (task 7761). Keyed on the
# family, not the full pinned id, so a ladder version bump (`claude-sonnet-4-6` → `-4-7`) keeps
# resolving instead of silently falling off the mapping. MODEL_LADDER itself must keep its BARE
# names: `largest_window_tokens` matches with `if name in model`, and no class name is a
# substring of `bedrock:us.anthropic.claude-sonnet-4-6`, so class names in the table would make
# every window lookup miss and silently return the ladder maximum for every model.
_RUNG_CLASSES: tuple[tuple[str, str], ...] = (
    ("haiku", "trivial"),
    ("sonnet", "standard"),
    ("opus", "frontier"),
)


def _rung_target(bare: str, primary: str | None = None, *, own_rung: bool = False) -> str | None:
    """The escalation target for ladder rung ``bare``, resolved through its model class.

    ``primary`` is the model the RUN is actually on, and ``own_rung`` marks the rung the primary
    itself sits at. Returning ``None`` means the rung has no honest target and must be DROPPED.

    THE BACK-COMPAT RULE: when the rung's class has NOT been retargeted, return TODAY'S BARE
    NAME byte-for-byte. ``resolve_class`` runs every name through ``_resolve_target``, which
    PREFIXES the inferred provider, so a naive always-resolve would hand existing callers
    ``anthropic:claude-sonnet-4-6`` where they have always seen ``claude-sonnet-4-6`` —
    ``plan_review/orchestrator.py``'s module-level ``_models_at_or_above`` alias (asserted
    against literal bare ids at ``tests/unit/test_plan_review.py:1145-1150``) and
    ``plan_review/prerequisites.py``'s escalation log both depend on the bare form. So the
    rung's OWN provider-inferred qualified form is computed and compared: equal ⇒ nothing is
    configured ⇒ keep the bare name; different ⇒ a config table or ``REBAR_LLM_<CLASS>_MODEL``
    env override retargeted the class, and THAT retarget is what escalation must follow.

    THE PROVIDER-AGREEMENT RULE: the back-compat rule above is decided entirely from the LADDER
    CONSTANT — both sides of the comparison derive from ``bare`` — so the provider the run is on
    is not an input, and an unretargeted class table degrades every rung to a bare, direct-
    Anthropic-inferring name. Once the target is resolved, the RUN's provider and the TARGET's
    provider are therefore compared:

    * they agree, or either is undeterminable ⇒ today's value, byte-for-byte;
    * they differ on the primary's OWN rung ⇒ the primary verbatim, so the same-model
      one-criterion retry (the step that most often succeeds, one criterion being a far smaller
      prompt than the batch) still runs on the operator's model;
    * they differ on a HIGHER rung ⇒ ``None``. The class vocabulary is the only thing that can
      name that rung in the run's provider and it has not been configured, so there is no honest
      target: escalation stops and the too-big failure finding reports it, rather than silently
      relocating the call to another provider.

    A rung with no class mapping, or any failure resolving a class, degrades to the bare name —
    escalation is a recovery path and must never become a new failure mode."""
    class_name = next((cls for family, cls in _RUNG_CLASSES if family in bare), None)
    if class_name is None:
        return bare
    try:
        from rebar.llm.model_classes import resolve_model_string

        resolved = resolve_model_string(class_name)
        provider = infer_provider(bare)
        own = f"{provider}:{bare}" if provider else bare
        target = bare if resolved == own else resolved
    except Exception:  # noqa: BLE001 — best-effort: an unresolvable class must not break escalation
        return bare
    if primary is None:
        return target
    run_provider = infer_provider(primary)
    target_provider = infer_provider(target)
    if not run_provider or not target_provider or run_provider == target_provider:
        return target
    return primary if own_rung else None


def models_at_or_above(model: str | None) -> list[str]:
    """The model ladder from ``model`` upward (by window), for runtime escalation.
    ABSENT model → the whole ladder; a model the ladder cannot LOCATE → just that model.

    The START rung is still located by substring match against MODEL_LADDER's bare names, so an
    already-resolved ``provider:model`` primary positions correctly; each at-or-above rung is
    then mapped through its MODEL CLASS by :func:`_rung_target`, so a Bedrock-configured run
    escalates to Bedrock rather than to direct Anthropic (task 7761). ORDER is unchanged:
    cheapest rung first, frontier last.

    Once a start rung IS located, ``_rung_target`` also applies the provider-agreement rule
    against ``model`` and a rung it cannot name in the run's provider is dropped.

    A TRUTHY primary the ladder cannot locate returns ``[model]`` — the operator's own model and
    nothing else (bug 48b3). Falling through to the whole ladder made the first "escalation" for
    e.g. a Nova primary ``claude-haiku-4-5``: a SMALLER window and a different provider than the
    operator configured, so escalation DOWNGRADED. Returning ``[]`` instead would be wrong the
    other way: :func:`pass1_with_ladder` has two independent recovery steps — batch →
    one-criterion-per-call, and only THEN rung climbing — and an empty list makes the escalation
    loop body never execute, jumping straight to the too-big failure finding and discarding the
    single-criterion retry, the step that most often succeeds because one criterion is a far
    smaller prompt than the batch. ``[model]`` preserves that retry and stops before the first
    cross-family rung, so the failure finding fires only when the criterion genuinely does not
    fit. ``prerequisites.run_focused_finder``'s escalation inherits the same repair without being
    edited: its ``ladder.index(model) + 1`` runs off the end of a one-element ladder and its
    existing ``IndexError`` handler already yields the explicit input-too-large indeterminate."""
    names = [n for n, _w in MODEL_LADDER]
    if model:
        for i, n in enumerate(names):
            if n in model:
                targets = [_rung_target(name, model, own_rung=(name == n)) for name in names[i:]]
                return [t for t in targets if t is not None]
        return [model]
    return [t for name in names if (t := _rung_target(name)) is not None]


def _rung_window(target: str) -> int | None:
    """The MODEL_LADDER-declared window for an escalation TARGET, or ``None`` if no rung matches.

    Uses the same bare-name substring match as :func:`largest_window_tokens`, so a target the
    model-class vocabulary retargeted (e.g. ``bedrock:us.anthropic.claude-sonnet-4-6``) still
    resolves to the window of the rung it stands for."""
    for name, window in MODEL_LADDER:
        if name in target:
            return window
    return None


def escalation_rungs(model: str | None) -> list[str]:
    """The rungs a CONTEXT-LIMIT retry should climb: :func:`models_at_or_above` with every rung
    that buys no additional context removed.

    A rung above the start is only ever reached after :func:`is_context_limit_error`, so a retry
    whose declared window is not strictly LARGER than the one that just failed cannot succeed —
    it re-hits the identical limit a full round-trip later, at the higher rung's price, and the
    trace reads as "even the bigger model could not fit it" when in fact nothing got bigger.
    MODEL_LADDER's sonnet and opus rungs both declare 1_000_000 — both figures are CORRECT, the
    two models genuinely share a window — so sonnet -> opus is exactly such a no-op (bug 1157).

    The START rung is always kept: it is the operator's own model rather than an escalation, and
    the single-criterion retry there is the step that most often succeeds. Above it a rung is kept
    only when its window is strictly greater than the last KEPT rung's; comparing against the last
    kept rung rather than the immediate predecessor stays correct for a ladder carrying several
    equal-window rungs in a row. A target whose rung window cannot be determined is KEPT, never
    silently dropped — escalation is a recovery path and must not become a new failure mode.

    :func:`models_at_or_above` deliberately keeps its own contract (every rung at or above the
    start, class-resolved, cheapest first): it answers "which rungs sit above this one", which is
    still a truthful question and is what task 7761's provider-stickiness tests observe."""
    rungs = models_at_or_above(model)
    if not rungs:
        return rungs
    kept = [rungs[0]]
    best = _rung_window(rungs[0])
    for target in rungs[1:]:
        window = _rung_window(target)
        if window is None:
            kept.append(target)  # unknown window ⇒ keep; never drop a rung we cannot size
            continue
        if best is None or window > best:
            kept.append(target)
            best = window
    return kept


def pass1_with_ladder(
    runner: Runner,
    cfg: LLMConfig,
    plan: str,
    chunk: list[dict],
    agentic: bool,
    events: list[str],
    extra_context: str = "",
    tf_provider: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run a Pass-1 finder call with the SIZE-HANDLING LADDER (ca03 AC4/AC6):

    1. run the criteria BATCH (chunk) at the configured model;
    2. on a context-limit signal, fall back to ONE CRITERION PER CALL (full content,
       minimal rubric — content is never chunked);
    3. on a context-limit signal for a single criterion, ESCALATE up the model ladder;
    4. if a single criterion still won't fit at the largest window, emit a FAILURE
       FINDING (P8: the ticket is too big to review in full — reduce/decompose it).

    Non-context errors drop the unit's findings (never abort the review). ``events``
    accumulates a human-readable ladder trace for the coverage record.

    ``extra_context`` is authoritative store-derived context (e.g. the G5 DECOMPOSITION
    STATE block) threaded verbatim to :func:`passes.pass1_chunk` at every ladder rung so
    the injected fact survives batch→single-criterion fallback and model escalation.

    Returns ``(findings, call_records)`` — every COMPLETED ladder attempt appends its own
    :func:`usage_record` (all carrying the attempt's criteria list); an attempt that
    RAISES (e.g. the context-limit error that triggers escalation) contributes nothing
    (see :func:`usage_record`)."""
    calls: list[dict[str, Any]] = []
    ids = [c["id"] for c in chunk]
    try:
        findings, usage = passes.pass1_chunk(
            runner,
            cfg,
            plan=plan,
            chunk=chunk,
            agentic=agentic,
            extra_context=extra_context,
            tf_provider=tf_provider,
        )
        calls.append(usage_record(ids, usage))
        return findings, calls
    except LLMUnavailableError:
        raise  # SYSTEMIC failure (deps/key/auth/connection) — surface, never drop (fuel-posse-ball)
    except Exception as exc:  # noqa: BLE001 — broad to inspect is_context_limit_error(exc); a non-context failure drops findings, a context error falls through to the size-ladder
        if not is_context_limit_error(exc):
            return [], calls  # unrelated failure → drop this unit's findings (never abort)

    if len(chunk) > 1:
        events.append(f"batch of {len(chunk)} hit the context limit → one-criterion-per-call")
    out: list[dict[str, Any]] = []
    for crit in chunk:
        produced = False
        for model in escalation_rungs(cfg.model):
            try:
                crit_findings, usage = passes.pass1_chunk(
                    runner,
                    replace(cfg, model=model),
                    plan=plan,
                    chunk=[crit],
                    agentic=agentic,
                    extra_context=extra_context,
                    tf_provider=tf_provider,
                )
                out.extend(crit_findings)
                calls.append(usage_record([crit["id"]], usage))
                if model != cfg.model:
                    events.append(f"{crit['id']}: escalated to {model}")
                produced = True
                break
            except LLMUnavailableError:
                raise  # SYSTEMIC failure — surface, never drop (fuel-posse-ball)
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
                    # COHORT (WS9): a size-ladder failure is a single-criterion finding — stamp its
                    # singleton cohort so it isn't excluded from contamination analysis.
                    "cohort": [crit["id"]],
                }
            )
    return out, calls


__all__ = [
    "COST_AGENT_USD",
    "COST_SINGLE_TURN_USD",
    "DEFAULT_BUDGET_CAP_USD",
    "MODEL_LADDER",
    "USAGE_TOKEN_FIELDS",
    "centrality",
    "checkpoint_identity",
    "container_budget",
    "escalation_rungs",
    "is_context_limit_error",
    "largest_window_tokens",
    "load_checkpoint",
    "models_at_or_above",
    "pack_container_bins",
    "pass1_with_ladder",
    "plan_budget_cap",
    "save_checkpoint",
    "shed_to_budget",
    "usage_record",
]
