"""Carry unresolved code-review findings across at most one later patchset.

Only findings surfaced in the prior decision and carrying grounding evidence are eligible.
An unchanged or unresolvable region is ``still-present`` and is carried. A changed region is
``addressed`` and is dropped. An item that already has standing provenance is ``withdrawn`` and
cannot be carried again. This three-state rule prevents a permanent finding ratchet while
keeping ambiguous evidence in front of the verifier.

Fresh finders never receive standing items. Pass 2 re-grounds them, and the posture clamp can
only lower a decision. Sidecar errors produce no standing items and never abort the review.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Cap on the carried-candidate set, bounding the added Pass-2 verification cost. Matched to
#: plan-review's ``RECALL_CAP`` so the two backstops stay comparable.
STANDING_CAP = 12

STATE_STILL_PRESENT = "still-present"
STATE_ADDRESSED = "addressed"
STATE_WITHDRAWN = "withdrawn"

#: The states whose items are injected into the fresh review. Everything else is recorded and
#: dropped — that is what terminates the carry chain.
CARRIABLE_STATES = frozenset({STATE_STILL_PRESENT})

#: The decision a carried item is clamped to when the prior payload recorded none.
_DEFAULT_ORIGIN_DECISION = "advisory"


def classify_state(finding: dict[str, Any], prior_deps: dict[str, str] | None, *, repo_root=None):
    """The ``standing.state`` for one prior finding — see the module docstring for the rule behind
    each value. Pure dispatch over :func:`region_gate.region_for_finding` plus the already-carried
    check; never raises (the region detector is itself fail-safe)."""
    from rebar.llm.code_review import region_gate

    if isinstance(finding.get("standing"), dict):
        return STATE_WITHDRAWN
    region = region_gate.region_for_finding(finding, prior_deps, repo_root=repo_root)
    if region == region_gate.REGION_CHANGED:
        return STATE_ADDRESSED
    return STATE_STILL_PRESENT


def _priority(finding: dict[str, Any]) -> float:
    try:
        return float(finding.get("priority") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _carried(finding: dict[str, Any], *, origin_revision: str, state: str) -> dict[str, Any]:
    """The slim, re-groundable candidate injected into the fresh review: the prior finding's own
    text + grounding (so Pass-2 re-grounds quotes against the CURRENT diff rather than judging a
    bare restatement), plus the ``standing`` provenance the clamp and the Gerrit renderer read."""
    from rebar.llm.plan_review.sidecar import norm_id

    # The bucket the prior review surfaced this under, stamped by the reader (the surfaced union
    # erases the bucket boundary). A payload predating that stamp falls back to the finding's own
    # recorded decision, then to advisory — the clamp must never invent a blocking posture.
    decision = finding.get("origin_decision") or finding.get("decision")
    return {
        "finding": finding.get("finding", ""),
        "suggested_fix": finding.get("suggested_fix", ""),
        "criteria": list(finding.get("criteria") or []),
        "location": finding.get("location", ""),
        "evidence": list(finding.get("evidence") or []),
        "impact": finding["impact"] if isinstance(finding.get("impact"), str) else "",
        "norm_id": finding.get("norm_id") or norm_id(finding),
        "standing": {
            "origin_revision": origin_revision,
            "origin_decision": (
                decision if isinstance(decision, str) and decision else _DEFAULT_ORIGIN_DECISION
            ),
            "state": state,
        },
    }


def standing_items(key: str, *, repo_root=None, coverage: dict[str, Any] | None = None):
    """Return eligible prior findings by descending priority, capped at ``STANDING_CAP``.

    The typed key preserves separate local-session and Gerrit-change histories. The sidecar
    reader supplies only findings surfaced as blocking or advisory, and this function further
    requires grounding evidence. Optional ``coverage`` records state counts and ungrounded
    suppression. Any read or classification failure returns an empty list.
    """
    try:
        from rebar.llm.code_review import sidecar

        prior = sidecar.latest_code_review_result(key, repo_root=repo_root) if key else None
        if not prior:
            return []
        prior_findings = list(prior.get("findings") or [])
        prior_deps = prior.get("deps") or {}
        origin_revision = str(prior.get("revision") or "")
        eligible = [f for f in prior_findings if isinstance(f, dict) and f.get("evidence")]
        if coverage is not None and len(eligible) < len(prior_findings):
            coverage["standing_suppressed"] = "ungrounded-prior"
        eligible.sort(key=_priority, reverse=True)
        counts: dict[str, int] = {}
        carried: list[dict[str, Any]] = []
        for f in eligible:
            state = classify_state(f, prior_deps, repo_root=repo_root)
            counts[state] = counts.get(state, 0) + 1
            if state not in CARRIABLE_STATES or len(carried) >= STANDING_CAP:
                continue
            carried.append(_carried(f, origin_revision=origin_revision, state=state))
        if coverage is not None:
            coverage["standing"] = {"carried": len(carried), "states": counts}
        return carried
    except Exception:
        logger.warning("code-review carry-forward read failed; carrying nothing", exc_info=True)
        return []


def inject_standing(findings: list[dict[str, Any]], standing: list[dict[str, Any]]):
    """Append the standing items the FRESH finders did not already produce (matched on
    ``norm_id``), returning a new list. An item the fresh run re-emitted needs no carry — the fresh
    finding is the live one, and re-adding it would double-verify the same defect."""
    from rebar.llm.plan_review.sidecar import norm_id

    out = list(findings)
    seen = {norm_id(f) for f in out if isinstance(f, dict)}
    for item in standing or []:
        if not isinstance(item, dict):
            continue
        nid = item.get("norm_id") or norm_id(item)
        if nid in seen:
            continue
        seen.add(nid)
        out.append(dict(item))
    return out


def verify_standing_note(findings: list[dict[str, Any]]) -> str:
    """The code-review-local addendum naming the standing items for the Pass-2 verifier, or ``""``
    when none are present.

    Deliberately NOT folded into ``review_kernel.verify_instructions``: plan-review calls that same
    builder, so wording added there would change plan-review's verifier prompt too."""
    marked = [
        (i, f)
        for i, f in enumerate(findings or [])
        if isinstance(f, dict) and isinstance(f.get("standing"), dict)
    ]
    if not marked:
        return ""
    lines = [
        "## Standing findings carried from an earlier patchset",
        "",
        "These findings were raised on an earlier revision and were NOT re-raised by this run's "
        "finders. Verify each ONE exactly as you verify a fresh finding — re-ground its evidence "
        "against the diff under review. A standing finding that the current change resolved must "
        "fail verification; carried memory alone is never grounds to keep it.",
        "",
    ]
    for i, f in marked:
        rev = str((f.get("standing") or {}).get("origin_revision") or "an earlier patchset")
        lines.append(f"- Finding {i}: standing since patchset {rev}.")
    return "\n".join(lines)


def clamp_standing_decisions(decided: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clamp every decided finding that carries ``standing`` to AT MOST its origin decision,
    mutating in place and returning the list.

    Needed because ``pass3_decide`` RECOMPUTES the decision on every run from the criterion's
    current ``blocking_enabled`` / ``block_threshold`` routing, and a finding's criterion can
    change between patchsets — so without this, carrying a finding forward could turn a prior
    ADVISORY into a fresh BLOCK purely by memory. Lowering is untouched: only a raise is clamped."""
    for f in decided or []:
        standing = f.get("standing") if isinstance(f, dict) else None
        if not isinstance(standing, dict):
            continue
        origin = standing.get("origin_decision") or _DEFAULT_ORIGIN_DECISION
        if f.get("decision") == "block" and origin != "block":
            f["decision"] = origin
            f["reason"] = "standing-clamp"
    return decided
