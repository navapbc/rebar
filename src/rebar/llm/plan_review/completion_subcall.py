"""Completion-aware container plan-review seam (epic 66ac).

A separate Pass-2 sub-call classifies finding attribution, containment, and
layer; the deterministic Pass-3 floor can then drop low-priority findings
about delivered child plan semantics. ``passes`` re-exports this module's
names for existing callers. Import ``passes`` inside functions, after module
initialization, to avoid its import cycle and preserve call-time monkeypatch
resolution of ``PASS_COMPLETION`` and helper globals."""

from __future__ import annotations

import logging
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner, RunRequest

logger = logging.getLogger(__name__)


# Pass-2's local completion contract classifies delivered-child attribution,
# containment, and layer. Permissive str fields are coerced per finding to
# closed enum values; one bad answer takes the fail-safe non-drop default
# without rejecting the entire structured batch (epic 66ac).
COMPLETION_ATTRIBUTION_NONE = "none"  # attribution when a finding is about no closed child
# Share the 2 drop-eligible enum literals between the sub-call vocabulary
# and Pass-3 completion floor (story 6533).
COMPLETION_CONTAINMENT_CLOSED = "limited-to-closed"  # containment value the floor drops on
COMPLETION_LAYER_PLAN = "plan-semantics"  # layer value the floor drops on
COMPLETION_CONTAINMENT = (COMPLETION_CONTAINMENT_CLOSED, "spans-open-or-system", "n-a")
COMPLETION_LAYER = (COMPLETION_LAYER_PLAN, "delivered-functionality", "n-a")
# Fail-safe defaults — each independently steers the (later) Pass-3 floor AWAY from a drop, so an
# unsure / missing / invalid answer keeps the finding (drop-nothing is the safe direction).
_COMPLETION_CONTAINMENT_DEFAULT = "spans-open-or-system"
_COMPLETION_LAYER_DEFAULT = "delivered-functionality"


def _pass2_completion_model() -> type:
    """The Pass-2 ``completion`` structured-output model: one ``CompletionSubAnswers`` per finding
    (by ``index``) carrying the three atomic completion-awareness sub-answers.

    Mirrors the novelty/verification per-finding shape (a flat list wrapper keyed by ``index``).
    The sub-answers are ``str`` (not pydantic ``Literal``) on purpose — matching the
    novelty/verification precedent — so a divergent value validates through and is COERCED to the
    closed vocabulary by :func:`pass2_completion` (one bad value never fails the whole batch)."""
    from pydantic import BaseModel, Field

    class CompletionSubAnswers(BaseModel):
        index: int = Field(description="The 0-based index of the finding being classified.")
        attribution: str = Field(
            default=COMPLETION_ATTRIBUTION_NONE,
            description="A CLOSED child ticket-id this finding is about, or 'none' (not about any "
            "closed child).",
        )
        containment: str = Field(
            default=_COMPLETION_CONTAINMENT_DEFAULT,
            description="limited-to-closed | spans-open-or-system | n-a",
        )
        layer: str = Field(
            default=_COMPLETION_LAYER_DEFAULT,
            description="plan-semantics | delivered-functionality | n-a",
        )

    class CompletionOutput(BaseModel):
        completions: list[CompletionSubAnswers] = Field(default_factory=list)

    return CompletionOutput


# ── Pass 2: completion sub-call (epic 66ac / child 94fd) — the completion-aware container seam ──
# A SEPARATE Pass-2 sub-call that classifies each finding on three atomic axes so the (later)
# Pass-3 completion FLOOR can decide whether the finding merely re-litigates already-DELIVERED
# child work. Structurally mirrors the novelty sub-call: a distinct contract + single-turn call
# that receives ONLY the plan + the delivered-children manifest (Pass-1 independence — it is NOT
# fed the prior findings). It DOES NOT itself drop anything; it emits the classification the floor
# consumes.
def _delivered_manifest_block(manifest: list[dict[str, Any]]) -> str:
    """Render the delivered-children manifest as the sub-call's context: each already-delivered
    child's id + its OWN Acceptance Criteria text (so the model can judge attribution/containment
    against what that child actually delivered)."""
    return "\n\n".join(
        f"### delivered child {m.get('ticket_id', '?')}\n"
        f"acceptance criteria:\n{(m.get('ac_text') or '(none recorded)')}"
        for m in manifest
    )


def _completion_finding_listing(findings: list[dict[str, Any]]) -> str:
    """The per-finding listing the completion sub-call classifies (by 0-based index). A STRUCTURAL
    (G3/G4 container) finding already carries ``_container_child`` — its attribution is
    DETERMINISTIC, so the listing PRE-STATES it and tells the model to answer only containment +
    layer for that finding; a non-structural finding asks for all three."""
    blocks: list[str] = []
    for i, f in enumerate(findings):
        child = f.get("_container_child")
        attr_line = (
            f"attribution: {child} (PRE-ATTRIBUTED, structural — do NOT re-derive; answer only "
            "containment + layer)"
            if child
            else "attribution: (answer the delivered child id it is about, or 'none')"
        )
        blocks.append(
            f"### finding index {i}\n"
            f"claim: {f.get('finding', '')}\n"
            f"criteria: {', '.join(f.get('criteria', []) or [])}\n"
            f"location: {f.get('location', '')}\n"
            f"{attr_line}"
        )
    return "\n\n".join(blocks)


def _coerce_completion_enum(value: Any, allowed: tuple[str, ...], default: str) -> str:
    """Coerce a sub-answer to the CLOSED vocabulary: pass a value that is exactly one of ``allowed``
    through; anything missing/invalid becomes the fail-safe ``default`` (drop-nothing direction)."""
    return value if isinstance(value, str) and value in allowed else default


def _coerce_attribution(value: Any) -> str:
    """Attribution is an OPEN vocabulary (a child ticket-id) — accept any non-empty string; a
    missing/blank value becomes ``"none"`` (the fail-safe: not about any closed child)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return COMPLETION_ATTRIBUTION_NONE


def pass2_completion(
    runner: Runner,
    cfg: LLMConfig,
    *,
    plan: str,
    findings: list[dict[str, Any]],
    delivered_manifest: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Classify each finding for the completion floor. Returns
    ``{finding_index: {"attribution", "containment", "layer"}}``.

    A single-turn structured sub-call (``output_schema="plan_review_completion"``) over the plan +
    the delivered-children ``delivered_manifest`` (built by
    :func:`rebar.llm.plan_review.orchestrator.delivered_children_manifest`) + the finding listing.
    It is NOT given the prior findings (Pass-1 independence, mirroring the novelty sub-call).

    DETERMINISM: a finding that already carries ``_container_child`` (G3/G4 structural attribution)
    has its ``attribution`` set to that child id DETERMINISTICALLY — the model is asked only for
    containment + layer on it (never to re-derive the attribution). Non-structural findings get all
    three from the model. Every enum sub-answer is coerced to its closed vocabulary.

    DEGRADE (fail toward keep): with no findings or an EMPTY manifest there is nothing to classify,
    so it returns ``{}``; likewise any sub-call error returns ``{}``. An empty map means the
    downstream floor drops NOTHING."""
    # Function-local by necessity AND by design — see this module's docstring: `passes` imports
    # this module at import time (import cycle), and per-call resolution preserves the
    # `monkeypatch.setattr(passes, ...)` seam these names had as `passes` module globals.
    from .passes import PASS_COMPLETION, _max_output_cfg, _resolve_system

    if not findings or not delivered_manifest:
        return {}
    req = RunRequest.for_structured(
        system_prompt=_resolve_system(PASS_COMPLETION, plan, cfg),
        instructions=(
            "## Delivered-children manifest (each already-delivered child + its own AC)\n"
            f"{_delivered_manifest_block(delivered_manifest)}\n\n"
            "## Findings to classify (by index)\n"
            f"{_completion_finding_listing(findings)}\n\n"
            "For EACH finding, by its index, answer the three atomic questions "
            "(attribution / containment / layer). Answer the fail-safe value when unsure."
        ),
        config=_max_output_cfg(cfg),  # model-max output budget (bug 30a2)
        reviewers=["plan-completion"],
        output_schema="plan_review_completion",
        bounds=RunRequest.INHERIT_POLICY,
    )
    try:
        raw = runner.run(req).get("completions", []) or []
    except Exception:
        logger.warning(
            "completion sub-call failed; classifying nothing (the floor drops nothing)",
            exc_info=True,
        )
        return {}

    # Reshape the flat list into {index: answers}, tolerantly (mirrors reshape_novelties): a
    # non-int / out-of-range index is dropped; a later item wins on a duplicate.
    by_index: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(findings):
            by_index[idx] = item

    out: dict[int, dict[str, Any]] = {}
    for i, f in enumerate(findings):
        ans = by_index.get(i, {})
        struct_child = f.get("_container_child")
        attribution = (
            str(struct_child) if struct_child else _coerce_attribution(ans.get("attribution"))
        )
        out[i] = {
            "attribution": attribution,
            "containment": _coerce_completion_enum(
                ans.get("containment"), COMPLETION_CONTAINMENT, _COMPLETION_CONTAINMENT_DEFAULT
            ),
            "layer": _coerce_completion_enum(
                ans.get("layer"), COMPLETION_LAYER, _COMPLETION_LAYER_DEFAULT
            ),
        }
    return out


def completion_floor_drop(
    completion: dict[str, Any],
    priority: float,
    criteria: list[str] | None,
    *,
    floor: float,
    preserve: frozenset[str],
    delivered_ids: frozenset[str],
) -> bool:
    """Return whether Pass-3 may drop a completion finding.

    Never drop preserve-set criteria. Otherwise require a child attribution in
    the proven delivered-now ID set, containment limited to closed work, a
    plan-semantics layer, and priority below the floor. A force-closed or
    hallucinated child ID, ambiguous sub-answer, delivered functionality, or
    high-priority finding remains (story 6533)."""
    if any(c in preserve for c in (criteria or [])):
        return False  # preserve-set veto (security/contract) — never dropped
    attribution = completion.get("attribution", COMPLETION_ATTRIBUTION_NONE)
    if attribution not in delivered_ids:
        return False  # "none", a force-closed/undelivered child, or a hallucinated id — keep
    if completion.get("containment") != COMPLETION_CONTAINMENT_CLOSED:
        return False  # spans open/system work (or n-a) — still live
    if completion.get("layer") != COMPLETION_LAYER_PLAN:
        return False  # about delivered functionality (or n-a), not throw-away plan text
    return priority < floor
