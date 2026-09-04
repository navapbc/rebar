"""The four-pass review engine for the plan-review gate.

Implements the evidence → binary-verify → deterministic-gate model (adopted from
epic ``9da1``), plus the Pass-4 affirmative coach:

* **Pass 1 — find** (children ``1913``): the finder surfaces grounded FINDINGS
  ``{finding, criteria[], evidence[], scenarios[], impact}`` — NO severity, NO
  confidence. Single-turn over facet-chunks of the rubric; agentic (tool-using)
  for the code-grounding criteria.
* **Pass 2 — verify** (child ``acc1``): a SEPARATE verifier re-grounds each finding
  and emits coarse severity ATTRIBUTES + a typed BINARY sub-answer set
  ``{yes|no|insufficient}`` — one aggregate pass over all findings.
* **Pass 3 — decide** (child ``487d``): DETERMINISTIC. Computes validity (graded
  fraction of the binary answers), impact (mean of the ordinal-mapped severity
  attributes), the unified priority score (validity × impact), and the
  ``block | advisory | dropped`` decision. The model emits NO holistic
  severity/confidence anywhere in the decision path.
* **Pass 4 — coach** (child ``75a9``): a single-turn structured call over the
  SURVIVING (advisory) findings that maps each to a move from a locked registry —
  rendered deterministically (the LLM never authors free prose).

The model-driven passes (1, 2, 4) go through the shared :class:`~rebar.llm.runner.Runner`
seam, so they are fully exercisable offline with a ``FakeRunner``. Pass 3 is pure
arithmetic — no model, fully unit-testable.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from rebar.llm import contracts
from rebar.llm.config import LLMConfig
from rebar.llm.prompting import prompts
from rebar.llm.review_kernel import verify as _verify
from rebar.llm.runner import Runner, RunRequest

from . import completion_subcall as _completion_subcall
from .prerequisites import prerequisite_coverage_model as _prerequisite_coverage_model

logger = logging.getLogger(__name__)


# ── structured-output contracts (registered once on import) ────────────────────
def _pass1_model() -> type:
    from pydantic import BaseModel, Field

    class P1Finding(BaseModel):
        finding: str = Field(description="The defect/gap, stated as a claim to verify.")
        criteria: list[str] = Field(default_factory=list, description="Rubric criterion id(s).")
        location: str = Field(
            default="",
            description="WHERE: the plan section / file path / AC line the finding is about.",
        )
        evidence: list[str] = Field(
            default_factory=list,
            description="Flexible grounding: a plan quote, section name, or ABSENCE rationale.",
        )
        scenarios: list[str] = Field(default_factory=list, description="Where this bites.")
        impact: str = Field(default="", description="Consequence if unaddressed.")
        checklist_item: str = Field(
            default="",
            description="The finding expressed as ONE actionable `- [ ]` checklist line.",
        )
        suggested_fix: str = Field(
            default="",
            description="A concrete fix — ONLY when you are confident; leave empty otherwise.",
        )

    class P1Output(BaseModel):
        analysis: str = Field(default="", description="Scratchpad — reason before emitting.")
        affirmations: list[str] = Field(
            default_factory=list,
            description="Criteria this chunk PASSES — affirm what already holds (not findings).",
        )
        findings: list[P1Finding] = Field(default_factory=list)

    return P1Output


# Pass-2's `verification` contract (the binary sub-question vocabulary + the severity-attribute
# enums) is owned by the shared review KERNEL (epic vivid-gang-day WS2). Plan-review registers
# its OWN model factory under `plan_review_verification`: the kernel's Verification shape EXTENDED
# with the 7 plan-severity axes + a detection axis (story fishable-apivorous-redhead), which
# decide.impact_plan aggregates. It reuses the kernel's exact Binary vocabulary (built from the
# shared helper), so only the severity_attributes differ; the kernel `verification` contract used
# by code-review + the kernel default stays byte-identical.
_pass2_model = _verify.plan_review_verification_model
# Pass-2's SEPARATE novelty sub-call contract (child 150b) — the same kernel `novelty` model
# factory, aliased under the plan-review name `plan_review_novelty` (mirroring the verification
# pairing). The kernel registers it under the canonical name `novelty`.
_pass2_novelty_model = _verify.novelty_model


# The Pass-2 COMPLETION sub-call contract + its closed-vocabulary constants
# (`COMPLETION_*` / `_pass2_completion_model`) live in the sibling `completion_subcall.py`
# alongside the sub-call itself (module-size seam, task 8705). Re-exported at the top of this
# module, so `register_contracts` below and the `passes.<name>` call sites resolve unchanged.


def _pass4_model() -> type:
    from pydantic import BaseModel, Field

    class CoachNote(BaseModel):
        move_id: str = Field(description="A move id from the locked move registry.")
        subject: str = Field(
            description="A short noun-phrase subject (≤8 words; no code, no imperative)."
        )
        finding_refs: list[str] = Field(
            default_factory=list, description="The finding id(s) this move addresses."
        )

    class P4Output(BaseModel):
        notes: list[CoachNote] = Field(default_factory=list)

    return P4Output


def register_contracts() -> None:
    """Register the per-pass structured-output contracts (idempotent)."""
    contracts.register_contract("plan_review_findings", _pass1_model)
    contracts.register_contract("plan_review_verification", _pass2_model)
    contracts.register_contract("plan_review_novelty", _pass2_novelty_model)
    contracts.register_contract(
        "plan_review_completion", _completion_subcall._pass2_completion_model
    )
    contracts.register_contract("plan_review_coach", _pass4_model)
    contracts.register_contract("plan_review_prerequisite_coverage", _prerequisite_coverage_model)


register_contracts()


# ── prompts (loaded from the workflow-engine prompt library, NOT inline) ─────────
# The pass system prompts are contract-bearing prompt FILES in the prompt library
# (src/rebar/llm/reviewers/plan_review_*.md), resolved via the da27 prompt machinery
# (prompts.get_prompt / resolve_prompt) with `.rebar/prompts/<id>.md` project
# overrides — never inline string constants. Prompt ids:
PASS_FINDER = "plan-review-finder"  # Pass-1
PASS_PREREQUISITE_FINDER = "plan-review-prerequisite-finder"
PASS_PREREQUISITE_VERIFIER = "plan-review-prerequisite-verifier"
# Pass-2 verify runs via the workflow gate's `plan-review-verifier` prompt step (the bespoke
# pass2_verify that once resolved it here was retired in epic solid-timer-unison, WS1). The id
# constant is retained as the canonical reference to that prompt (used by the prompt-cache split).
PASS_VERIFIER = "plan-review-verifier"  # Pass-2
PASS_NOVELTY = "plan-review-novelty"  # Pass-2 SEPARATE novelty sub-call (child 150b)
PASS_COMPLETION = "plan-review-completion"  # Pass-2 SEPARATE completion sub-call (child 94fd)
PASS_COACH = "plan-review-coach"  # Pass-4
PASS_ISF = "plan-review-isf-finder"  # ISF finder
PASS_CONTAINER = "plan-review-container"  # G3/G4 container finder
PASS_CONTRADICTION = "plan-review-contradiction"  # validation: intra-verdict contradiction (5e40)
PASS_COMMENT_TRAIL = "plan-review-comment-trail"  # validation: comment-trail consultation (5e40)


# ── helpers ─────────────────────────────────────────────────────────────────────
def _max_output_cfg(cfg: LLMConfig) -> LLMConfig:
    """Model-max output budget for every plan-review request (bug 30a2)."""
    return _verify.max_output_cfg(cfg)


def _criterion_block(c: dict[str, Any]) -> str:
    checks = c.get("checklist") or []
    bullets = "\n".join(f"    - {ck.get('check', ck)}" for ck in checks) if checks else ""
    body = f"[{c['id']}] {c.get('name', '')}\n  {c.get('scenario', '')}"
    return body + (f"\n  Checklist:\n{bullets}" if bullets else "")


# The shared reviewing-stance preamble is SINGLE-SOURCED in the prompt registry now
# (``prompts.SHARED_STANCE_PREAMBLE`` + ``prompts.shared_plan_prefix``, story 9374): the
# same bytes lead every pass system prompt here AND the verifier templates' stable
# segment (via their `{{shared_prefix}}` variable), so the plan-bearing leading prefix is
# byte-identical across Pass-1 and Pass-2 by construction.
def _resolve_system(prompt_id: str, plan: str, cfg: LLMConfig) -> str:
    """Resolve a plan-review pass prompt from the prompt library to its compiled system
    prompt, led by the single-sourced reviewing stance. The FINDER is plan-first: its
    template is stance-only, and the returned prompt is ``shared_plan_prefix(plan)`` (the
    preamble + the whole plan) followed by the rendered stance — the byte-identical
    leading prefix the Pass-2 verifier stable segments also start with. Every other pass
    keeps its historical shape: ``SHARED_STANCE_PREAMBLE`` + the rendered template (with
    the plan still rendered at the template's own ``{{plan}}`` site). Both variables are
    supplied so any template may reference either; strict rendering only requires the
    ones a template actually uses. A project `.rebar/prompts/<id>.md` override wins over
    the packaged prompt. Reuses the da27 prompt machinery — no inline prompt strings."""
    variables = {"plan": plan, "shared_prefix": prompts.shared_plan_prefix(plan)}
    prompt = prompts.get_prompt(prompt_id, repo_root=cfg.repo_path)
    system, _meta = prompts.resolve_prompt(prompt, variables, repo_root=cfg.repo_path)
    # This path sends the WHOLE prompt as the system prompt; a prompt that carries the S2
    # `<!--volatile-->` cache-split marker (for the workflow RunnerAgentStep path) must
    # read here as if the marker were absent — strip it, keeping all content in place.
    body = prompts.strip_volatile_marker(system)
    if prompt_id == PASS_FINDER:
        return prompts.shared_plan_prefix(plan) + body
    return prompts.SHARED_STANCE_PREAMBLE + body


# ── Pass 1: find ─────────────────────────────────────────────────────────────────
def pass1_chunk(
    runner: Runner,
    cfg: LLMConfig,
    *,
    plan: str,
    chunk: list[dict[str, Any]],
    agentic: bool = False,
    extra_context: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one Pass-1 finder call over a chunk of criteria. Returns
    ``(findings, usage)`` — the findings (each tagged with the criteria it maps
    to) plus the call's ``_usage`` dict (``{}`` when the runner attaches none,
    e.g. the ``FakeRunner`` — a zero contribution, never an error). Single-turn
    unless ``agentic`` (the code-grounding tier).

    ``extra_context`` is authoritative, store-derived context prepended to the rubric
    instructions (currently the G5 DECOMPOSITION STATE block — see
    :func:`rebar.llm.plan_review.det_floor.decomposition_state_block`). The caller
    populates it ONLY for chunks whose criteria need it, so co-chunked criteria that
    don't are unaffected; empty by default (byte-identical to the prior instructions)."""
    ids = [c["id"] for c in chunk]
    rubric = "\n\n".join(_criterion_block(c) for c in chunk)
    context_block = f"{extra_context}\n\n" if extra_context else ""
    req = RunRequest(
        system_prompt=_resolve_system(PASS_FINDER, plan, cfg),
        # Bug 1dbe: put the cache breakpoint at the END of the byte-identical
        # ``shared_plan_prefix`` (the leading segment the Pass-2 verifier prompts also start
        # with) so the finder's cache WRITE is READ by the verifier within one run.
        cache_prefix=prompts.shared_plan_prefix(plan),
        instructions=(
            f"{context_block}"
            f"## Rubric criteria for this pass (ids: {', '.join(ids)})\n{rubric}\n\n"
            "Surface every grounded finding for these criteria. Return ONLY findings whose "
            "`criteria` are in this id set; an empty list for a clean chunk is correct."
        ),
        config=_max_output_cfg(cfg),  # model-max output budget (bug 30a2)
        reviewers=["plan-reviewer"],
        mode="structured",
        output_schema="plan_review_findings",
        execution_mode="agentic" if agentic else "single_turn",
        # ff64: routing `"web": true` rides AGENT chunks only (anthropic-gated in runner).
        web=agentic and any(bool(c.get("web")) for c in chunk),
    )
    result = runner.run(req)
    out: list[dict[str, Any]] = []
    for f in result.get("findings", []) or []:
        # Keep ONLY criteria in this chunk's rubric. A finding that maps to no
        # in-chunk criterion is the model violating the instruction ("return ONLY
        # findings whose criteria are in this id set") — DROP it rather than
        # fabricate an attribution (the old `or ids[:1]` silently mis-attributed it
        # to the chunk's first criterion, corrupting the finding→criterion mapping
        # the coach + sidecar depend on). A genuine finding for another criterion is
        # surfaced when ITS chunk runs.
        crit = [c for c in (f.get("criteria") or []) if c in ids]
        if not crit:
            continue
        out.append(
            {
                "finding": f.get("finding", ""),
                "criteria": crit,
                "location": f.get("location", ""),
                "evidence": f.get("evidence", []) or [],
                "scenarios": f.get("scenarios", []) or [],
                "impact": f.get("impact", ""),
                "checklist_item": f.get("checklist_item", ""),
                "suggested_fix": f.get("suggested_fix", ""),
                "_agentic": agentic,
                # COHORT (epic cite-stone-sea / WS9): the sorted set of criterion ids that were
                # CO-RESIDENT in this finder call — the contamination-analysis key for R-1.
                "cohort": sorted(ids),
            }
        )
    return out, dict(result.get("_usage") or {})


def pass1_container(
    runner: Runner,
    cfg: LLMConfig,
    *,
    parent_plan: str,
    children: list[dict[str, Any]],
    criteria: list[dict[str, Any]],
    sibling_roster: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run ALL container criteria (G3/G4/decomp-shape — the ``CONTAINER_CRITERIA`` set) for a
    parent + a BIN of one-or-more WHOLE children in a SINGLE agentic call (stories 98c6 merge +
    1762 bin-packing). The container prompt describes the audits over the shared
    (parent, children, roster) context; presenting the rubrics + every child in one turn halves
    calls (merge) and
    packs small children together (bin-pack) while keeping per-criterion AND per-child
    attribution. The complete sibling roster lets an absence finding be cross-checked
    against ALL siblings before it stands.

    Criterion attribution is MODEL-SELF-REPORTED then VALIDATED against the container id
    set (the ``criteria`` passed in — G3/G4/decomp-shape) — out-of-set tags DROPPED, a finding
    mapping to no in-set criterion
    dropped (mirrors ``pass1_chunk``; never fabricate an attribution). CHILD attribution
    is parsed from the model's ``location`` ('child <id>') and validated against the bin's
    children: a single-child bin falls back to its sole child; a multi-child finding the
    model left unattributed is kept as bin-level (``_container_child=None``) rather than
    mis-assigned. Per-child sections + the required per-child output preserve per-child
    attention so packing does not dilute it.

    Returns ``(findings, usage)`` — the call's ``_usage`` dict rides along (``{}`` when
    the runner attaches none; zero contribution)."""
    valid_ids = [c["id"] for c in criteria]
    bin_ids = [c.get("ticket_id", "?") for c in children]
    multi = len(children) > 1
    children_block = "\n\n".join(
        f"### child {c.get('ticket_id', '?')}: {c.get('title', '')}\n{c.get('description', '')}"
        for c in children
    )
    rubric = "\n\n".join(_criterion_block(c) for c in criteria)
    if multi:
        attribution = (
            f"The {len(children)} children are EACH in their own '### child <id>' section. "
            "Evaluate EVERY child against ALL of these criteria — do not skip any child. For "
            "EACH finding, set `location` to 'child <id>' naming the SPECIFIC child it "
            "concerns, and tag `criteria` with the container id(s) it addresses."
        )
    else:
        attribution = (
            f"Set `location` to 'child {bin_ids[0]}' and tag `criteria` with the container "
            "id(s) the finding addresses."
        )
    req = RunRequest(
        # The roster is BYTE-IDENTICAL across a review's pairings, so it rides the cached
        # prefix; in `instructions` (after the per-pairing `children_block`) it would be
        # re-sent per pairing — quadratic in child count once it carries each child's AC.
        system_prompt=(
            _resolve_system(PASS_CONTAINER, parent_plan, cfg)
            + f"\n\n## Complete sibling roster (for absence cross-check)\n{sibling_roster}\n"
        ),
        instructions=(
            f"## Container criteria for this pass (ids: {', '.join(valid_ids)})\n{rubric}\n\n"
            f"## Child/children under review (whole)\n{children_block}\n\n"
            f"{attribution} An absence is a finding only if NO sibling in the roster covers "
            "it. A clean pairing returns an empty findings list."
        ),
        config=_max_output_cfg(cfg),  # model-max output budget (bug 30a2)
        reviewers=["plan-container"],
        mode="structured",
        output_schema="plan_review_findings",
        execution_mode="agentic",
    )
    result = runner.run(req)
    out: list[dict[str, Any]] = []
    for f in result.get("findings", []) or []:
        crit = [c for c in (f.get("criteria") or []) if c in valid_ids]
        if not crit:
            continue
        loc = f.get("location", "") or ""
        # Attribute to the SPECIFIC bin child the model named in `location` as 'child <id>'.
        # Match the id as a WHOLE token after 'child ' (word-boundary anchored) — NOT a bare
        # substring — so a child id that is a prefix of another (e.g. 'c1' vs 'c12') is never
        # mis-attributed to the shorter id. A single-child bin falls back to its sole child;
        # a multi-child finding left unattributed stays bin-level (None), not mis-assigned.
        child_id = next(
            (cid for cid in bin_ids if cid and re.search(rf"child\s+{re.escape(cid)}\b", loc)),
            None,
        )
        if child_id is None and not multi:
            child_id = bin_ids[0]
        out.append(
            {
                "finding": f.get("finding", ""),
                "criteria": crit,
                "location": loc or (f"child {child_id}" if child_id else "container bin"),
                "evidence": [
                    *(f.get("evidence", []) or []),
                    f"container pairing: parent + {'/'.join(bin_ids)}",
                ],
                "scenarios": f.get("scenarios", []) or [],
                "impact": f.get("impact", ""),
                "checklist_item": f.get("checklist_item", ""),
                "suggested_fix": f.get("suggested_fix", ""),
                "_agentic": True,
                "_container_child": child_id,
                "_container_bin": list(bin_ids),
                # COHORT (WS9): the container criteria co-resident in this pairing call.
                "cohort": sorted(valid_ids),
            }
        )
    return out, dict(result.get("_usage") or {})


def pass1_isf(
    runner: Runner,
    cfg: LLMConfig,
    *,
    plan: str,
    session_log_text: str,
    ticket_graph: str = "",
    summarized: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The Intent-Source-Fidelity finder (child 681b). Fed the plan + the linked
    SESSION LOG + the PRE-RESOLVED TICKET GRAPH as context (single-turn, NOT agentic —
    the design forbids the tool loop here; the graph is resolved by the orchestrator
    and injected, not fetched by the model). When the log was summarized to fit the
    window, each finding is tagged ``_reduced_confidence``. Returns
    ``(findings, usage)`` — the call's ``_usage`` dict (``{}`` when absent)."""
    graph_block = (
        f"## Ticket graph (pre-resolved — parent / children / dependency links)\n{ticket_graph}\n\n"
        if ticket_graph
        else ""
    )
    req = RunRequest.for_structured(
        system_prompt=_resolve_system(PASS_ISF, plan, cfg),
        instructions=(
            "## Linked session log (the external intent of record)\n"
            f"{session_log_text}\n\n"
            f"{graph_block}"
            "Extract the expressed requirements/decisions/constraints, then check the plan AND "
            "its ticket graph (children may cover an expressed requirement) before flagging any "
            "the plan silently dropped, narrowed without rationale, or contradicted. A clean "
            "comparison returns an empty findings list."
        ),
        config=_max_output_cfg(cfg),  # model-max output budget (bug 30a2)
        reviewers=["plan-isf"],
        output_schema="plan_review_findings",
        bounds=RunRequest.INHERIT_POLICY,
    )
    result = runner.run(req)
    out: list[dict[str, Any]] = []
    for f in result.get("findings", []) or []:
        evidence = f.get("evidence", []) or []
        if summarized:
            evidence = [*evidence, "(ISF ran against a SUMMARY of an oversized session log)"]
        out.append(
            {
                "finding": f.get("finding", ""),
                "criteria": ["ISF"],
                "evidence": evidence,
                "scenarios": f.get("scenarios", []) or [],
                "impact": f.get("impact", ""),
                "_agentic": False,
                "_reduced_confidence": summarized,
                # COHORT (WS9): ISF runs a SINGLE fixed call, never co-resident with other
                # criteria, so its cohort is the singleton ["ISF"] (contamination cohort = itself).
                "cohort": ["ISF"],
            }
        )
    return out, dict(result.get("_usage") or {})


def summarize_for_isf(
    runner: Runner, cfg: LLMConfig, *, log_text: str
) -> tuple[str, dict[str, Any]]:
    """Compress an oversized session log to fit the ISF context window (a single
    text call). Used only when the log exceeds the budget — the PLAN is never
    summarized, only this supporting context. Returns ``(text, usage)`` — the
    summary plus the call's ``_usage`` dict (``{}`` when absent); the sole caller
    (the ISF oversized-log path in ``pass1``) records the usage as an ISF-attributed
    call record."""
    prompt = prompts.get_prompt("plan-review-isf-summarizer", repo_root=cfg.repo_path)
    system, _meta = prompts.resolve_prompt(prompt, {}, repo_root=cfg.repo_path)
    req = RunRequest(
        system_prompt=system,
        instructions=log_text,
        config=_max_output_cfg(cfg),  # model-max output budget (bug 30a2)
        reviewers=["plan-isf-summarizer"],
        mode="text",
        execution_mode="single_turn",
    )
    result = runner.run(req)
    return str(result.get("text", "")), dict(result.get("_usage") or {})


# ── Pass 2: verify ───────────────────────────────────────────────────────────────
# The Pass-2 verifier mechanism — the `verification` contract, the per-finding listing
# builders (`verify_instructions` / `verify_finding_listing`), the token-budget chunking, the
# merge-by-global-index, and `verify_findings` — is owned by the shared review kernel
# (`rebar.llm.review_kernel.verify`) as the single source (epic vivid-gang-day WS2). The
# listing builders are re-exported at the top of this module; the chunker lives in `.sizing`
# (a thin wrapper over the kernel chunker). The Pass-2 verify itself runs through the workflow
# gate's `plan-review-verifier` prompt step (the bespoke `pass2_verify` was retired in epic
# solid-timer-unison WS1).


# ── Pass 2: completion sub-call (epic 66ac / child 94fd) — the completion-aware container seam ──
