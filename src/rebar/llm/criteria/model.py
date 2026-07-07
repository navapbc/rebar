"""Shared criteria model — the descriptor + threshold machinery both review gates
delegate to (story 5065, the capstone of epic 3156).

Plan-review and code-review historically each carried their OWN copy of two pieces of
machinery: the per-criterion :func:`threshold_for` posture resolver, and (plan-review
only) the exec-tier-polymorphic descriptor builder. This module HOSTS the one shared
implementation of both; the two gates keep their public functions, which now DELEGATE
here. It is a pure refactor — behaviour is unchanged, so both gates' full suites stay
green.

Two deliberate divergences are preserved SIDE-BY-SIDE (not collapsed):

* :func:`threshold_for` dispatches its BLOCKING derivation on the ``gate`` argument —
  ``plan_review`` blocks on ``default_posture == "blocking"`` (the criterion's intended
  posture IS its runtime posture); ``code_review`` blocks on an explicit
  ``blocking_enabled`` flag (the detector keys ship ``default_posture: "blocking"`` yet
  must run ADVISORY until WS5 flips the separate enable flag). See ADR 0017.
* :func:`build_descriptor` is exec-tier polymorphic: an ``exec == "DET"`` criterion is a
  pattern-rule detector, not an LLM rubric, so it builds a PROMPT-LESS descriptor (the
  "scenario" is the detector's rule message); every other tier resolves its rubric via
  the injected ``prompt_getter`` (plan-review passes its ``get_prompt``). Generalizes
  plan-review's ``_descriptor_from_prompt`` (story 7f0d's DET branch included).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

# The default per-criterion block threshold when a criterion has no routing entry — the
# high-threshold, mostly-advisory v1 stance, kept in sync with
# ``review_kernel.decide.DEFAULT_BLOCK_THRESHOLD = 0.95`` (both gates historically used
# this same default in their own resolvers).
DEFAULT_BLOCK_THRESHOLD = 0.95


class CriteriaError(Exception):
    """The shared criteria layer could not build/validate a descriptor or overlay.

    Both gates re-export this as their own error alias (plan-review's ``RegistryError``)
    so every existing ``except``/``pytest.raises`` against the gate error keeps working."""


# ── the reconciled threshold resolver (hosts BOTH blocking conventions) ─────────────
def threshold_for(
    criteria: Sequence[str],
    routing_map: dict[str, Any],
    *,
    gate: str,
) -> tuple[float, bool]:
    """Resolve ``(block_threshold, blocking)`` for a finding's ``criteria`` from a gate's
    ``{criterion_id: routing_entry}`` map — the ``ThresholdResolver`` the review kernel's
    ``pass3_over_findings(..., threshold_for=...)`` consumes.

    ``block_threshold`` = the MIN over the criteria's thresholds (most aggressive; default
    :data:`DEFAULT_BLOCK_THRESHOLD`). ``blocking`` is gate-dispatched — this is the
    deliberate divergence the unification PRESERVES (do NOT collapse it):

    * ``gate == "plan_review"`` → True iff ANY criterion has ``default_posture ==
      "blocking"`` (the plan-review convention: the criterion's intended posture is its
      runtime posture).
    * ``gate == "code_review"`` → True iff ANY criterion has ``blocking_enabled: true``
      (the code-review convention: an EXPLICIT enable flag, separate from the staged
      ``default_posture`` — WS5 flips exactly the two detector keys).

    An unknown criterion contributes the default threshold and is NOT blocking (a
    base-reviewer dimension with no routing entry stays advisory at the default)."""
    thresholds = [
        float((routing_map.get(c) or {}).get("block_threshold", DEFAULT_BLOCK_THRESHOLD))
        for c in criteria
    ]
    bt = min(thresholds) if thresholds else DEFAULT_BLOCK_THRESHOLD
    if gate == "plan_review":
        blocking = any(
            str((routing_map.get(c) or {}).get("default_posture", "advisory")).lower() == "blocking"
            for c in criteria
        )
    elif gate == "code_review":
        blocking = any(
            bool((routing_map.get(c) or {}).get("blocking_enabled", False)) for c in criteria
        )
    else:  # pragma: no cover — a mis-wired gate is a programming error, not a data error
        raise CriteriaError(
            f"threshold_for: unknown gate {gate!r} (expected 'plan_review' or 'code_review')"
        )
    return bt, blocking


# ── exec-tier-polymorphic descriptor builder ────────────────────────────────────────
def detector_matches(detector_id: str, selector: dict[str, Any] | None) -> bool:
    """True iff ``detector_id`` matches a routing ``detector`` selector — an exact ``id`` or
    an ``id_prefix`` class (the selector grammar both gates' DET consumers read).

    PUBLIC criteria-model API (SC2): the plan-review DET-invariant consumer
    (``plan_review.det_invariants``) imports this directly, so it is a documented
    cross-package entry point rather than a leading-underscore symbol re-exported
    through the registry."""
    if not selector:
        return False
    exact = selector.get("id")
    if exact is not None and detector_id == exact:
        return True
    pref = selector.get("id_prefix")
    return pref is not None and detector_id.startswith(pref)


def _det_scenario(routing: dict[str, Any], repo_root: str | None) -> str | None:
    """The human-readable "scenario" for an exec:DET criterion = the message of the first
    detector its ``detector`` selector resolves to (from the on-disk detector registry).
    Returns ``None`` when no selector / no matching detector / no message (the caller falls
    back to name / id). Fail-open: any registry-load error yields ``None`` (a DET descriptor
    never depends on the detector suite being installed)."""
    selector = routing.get("detector")
    if not selector:
        return None
    try:
        from rebar.grounding.detectors import load_registry

        reg = load_registry(repo_root)
    except Exception:  # noqa: BLE001 — the detector suite is optional; a missing registry ⇒ fallback
        return None
    for det in reg:
        if detector_matches(det.id, selector):
            msg = (det.rule or {}).get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
            return None
    return None


# A prompt object exposes .text / .dimension / .title (the da27 prompt machinery's shape);
# the getter resolves a criterion id → its rubric prompt (project override > packaged).
PromptGetter = Callable[[str, str | None], Any]


def build_descriptor(
    cid: str,
    routing_entry: dict[str, Any] | None,
    *,
    repo_root: str | None = None,
    prompt_getter: PromptGetter | None = None,
) -> dict[str, Any]:
    """Build a criterion descriptor from its routing entry, exec-tier polymorphically.

    * ``exec == "DET"`` → a PROMPT-LESS descriptor (a pattern-rule detector, not an LLM
      rubric): the ``scenario`` is the detector's rule message (resolved from the detector
      registry via the routing ``detector`` selector), never a prompt body — so an
      activated project DET criterion that ships no ``.rebar/prompts/…`` file still loads.
    * any other tier → resolve the RUBRIC via the injected ``prompt_getter`` (the caller's
      ``get_prompt`` wrapper) and merge its front-matter (facet/title/body) with the
      routing entry.

    ``routing_entry`` must be non-``None`` (the caller raises its own located error when a
    criterion has no routing). A non-DET criterion with no ``prompt_getter`` is a
    programming error (an LLM criterion always needs a rubric resolver)."""
    if routing_entry is None:  # pragma: no cover — callers pre-check + raise a located error
        raise CriteriaError(f"criterion {cid!r} has no routing entry to build a descriptor from")
    if str(routing_entry.get("exec", "")).upper() == "DET":
        return {
            "id": cid,
            "exec": "DET",
            "facet": routing_entry.get("facet", cid),
            "name": routing_entry.get("name", cid),
            "scenario": _det_scenario(routing_entry, repo_root) or routing_entry.get("name") or cid,
            "applies_at": routing_entry.get("applies_at", {}),
            "checklist": [],
            "block_threshold": routing_entry.get("block_threshold", DEFAULT_BLOCK_THRESHOLD),
            "default_posture": routing_entry.get("default_posture", "advisory"),
            "fail_mode": routing_entry.get("fail_mode", "open"),
            "detector": routing_entry.get("detector"),
            "routing": routing_entry.get("routing"),
            "trigger": None,
            "overlay_routing": None,
        }
    if prompt_getter is None:
        raise CriteriaError(
            f"criterion {cid!r} is an LLM-tier criterion but no prompt_getter was supplied"
        )
    prompt = prompt_getter(cid, repo_root)
    return {
        "id": cid,
        "exec": routing_entry.get("exec", "1-TURN"),
        "facet": prompt.dimension or routing_entry.get("facet", "misc"),
        "name": prompt.title or cid,
        "scenario": prompt.text.strip(),
        "applies_at": routing_entry.get("applies_at", {}),
        "checklist": routing_entry.get("checklist", []),
        "block_threshold": routing_entry.get("block_threshold", DEFAULT_BLOCK_THRESHOLD),
        "default_posture": routing_entry.get("default_posture", "advisory"),
        "routing": routing_entry.get("routing"),
        "trigger": routing_entry.get("trigger"),
        "overlay_routing": routing_entry.get("overlay_routing"),
    }
