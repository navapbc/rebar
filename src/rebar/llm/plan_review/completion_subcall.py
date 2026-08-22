"""The completion-aware container seam: the Pass-2 completion sub-call + its Pass-3 floor.

Epic ``66ac`` (children ``94fd`` / ``6533``) made container plan-review completion-aware: a
SEPARATE Pass-2 sub-call classifies each finding on three atomic axes (attribution / containment
/ layer), and a deterministic Pass-3 floor drops the findings that merely re-litigate
already-DELIVERED child work.

Extracted from :mod:`rebar.llm.plan_review.passes` (task 8705) as a pure move along a seam that
module already carved — its own section banner named this "the completion-aware container seam".
The cluster is self-contained in the call graph: ``_delivered_manifest_block``,
``_completion_finding_listing``, ``_coerce_completion_enum`` and ``_coerce_attribution`` each had
exactly ONE caller anywhere in the tree (``pass2_completion``); ``pass2_completion`` and
``completion_floor_drop`` were leaves *within* ``passes`` (nothing else in that module called
them); and ``_pass2_completion_model``'s only caller is ``passes.register_contracts()``. No body
is rewritten.

``passes`` re-exports every name here, so every existing call site keeps reaching them as
``passes.<name>`` — ``plan_review/__init__.py``, ``scripts/calibrate_completion_floor.py`` and the
test suite are untouched, and ``tests/unit/test_completion_floor.py``'s
``from rebar.llm.plan_review.passes import completion_floor_drop`` still resolves. Following the
contract ``tests/unit/test_gate_context_seam.py`` pins for the ``config``/``gate_context`` split,
NO consumer under ``src/`` is repointed at this module: they all keep resolving through
``passes``.

WHY ``passes`` IS IMPORTED INSIDE THE FUNCTION BODY, NOT AT MODULE LEVEL
    ``passes`` imports THIS module at module level — ``register_contracts()`` runs at import time
    and needs ``_pass2_completion_model`` — so a module-level ``from .passes import ...`` here
    would be a hard import cycle: when ``passes`` reaches its relative-import block,
    ``PASS_COMPLETION``, ``_max_output_cfg`` and ``_resolve_system`` are not yet defined and the
    import raises.  A **function-local** import defers the lookup to call time, when ``passes``
    is fully initialised.

    It also preserves resolution semantics exactly. Before the move these three names resolved
    through ``passes``'s module globals on EVERY call; a module-level ``from`` binds them once at
    import, so a ``monkeypatch.setattr(passes, ...)`` of any of them would be silently ignored.
    That failure mode is not theoretical — task ``c6c9`` demonstrated it on the sibling
    ``attest``/``remediation_mode`` split, where the naive form turned four tests RED. The
    function-local import re-reads the attribute per call, so the patch seam survives.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner, RunRequest

logger = logging.getLogger(__name__)


# ── Pass-2 COMPLETION sub-call contract (epic 66ac / child 94fd) — completion-aware container
#    plan-review. Its shape is plan-review-SPECIFIC (about a container's DELIVERED children — not
#    a generic kernel axis like novelty/verification), so it is defined here as a LOCAL factory
#    (like `_pass1_model` / `_pass4_model`), NOT aliased from the kernel. The three atomic
#    sub-answers are a CLOSED vocabulary; following the novelty/verification precedent they are
#    `str` fields (permissive contract) + these constants, with the closed set ENFORCED by
#    coercion in `pass2_completion` — so ONE bad value coerces to the fail-safe default rather
#    than failing the whole structured batch (the per-finding fail-safe the gate mandates). ─────
COMPLETION_ATTRIBUTION_NONE = "none"  # attribution when a finding is about no closed child
# The two DROP-ELIGIBLE enum values named once, so the sub-call vocabulary (the tuples below) and
# the Pass-3 completion floor (`completion_floor_drop`) consume the SAME literal — no value drift
# between the two ends of the contract (story 6533 AC).
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
    """The Pass-3 COMPLETION-floor drop predicate (story 6533), deterministic — no LLM. Mirrors
    :func:`rebar.llm.review_kernel.decide.rising_floor_drop`, but keyed on the completion
    sub-answers instead of novelty.

    A finding is dropped IFF **all** hold:

    - its ``attribution`` is a child id that is provably **delivered-now** — i.e. in
      ``delivered_ids`` (the manifest's delivered set). This is stronger than "not ``none``": a
      structural ``_container_child`` attribution can name a **force-closed** (unverified) child,
      which must NOT be dropped — "delivery is proven, not assumed" (ADR 0024). A hallucinated /
      non-delivered id also fails here;
    - its ``containment`` is exactly :data:`COMPLETION_CONTAINMENT_CLOSED` (limited to closed work);
    - its ``layer`` is exactly :data:`COMPLETION_LAYER_PLAN` (plan-semantics, not delivered
      functionality);
    - its ``priority`` (validity × impact) is ``< floor``;
    - **none** of its ``criteria`` is in the always-preserve set (e.g. security / contract).

    Every OTHER combination KEEPS the finding — and because every ambiguous/fail-safe sub-answer
    (``attribution="none"``, ``containment`` anything but limited-to-closed, ``layer`` anything but
    plan-semantics) is a non-drop value, an unsure classification always fails toward KEEP. The
    preserve-set veto is checked FIRST, so a security/contract finding is never dropped regardless
    of the other axes. Pure; the caller supplies the per-finding answers + priority + criteria and
    the configured floor + preserve set + the delivered-now id set."""
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
