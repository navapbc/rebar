"""Plan-review's deterministic Pass-4 move catalog + loader and the R6 advisory-triage stage.

This is the plan-review gate's move-catalog CONTENT — the consumer SEAM the shared review
kernel deliberately leaves per-gate (:mod:`rebar.llm.review_kernel` owns the coach MECHANISM;
the catalog of moves is the domain-specific part) — plus the deterministic R6 advisory-triage
stage. Both are pure/deterministic (no model call), so the same finding set yields
byte-identical output.

Extracted from :mod:`.passes` along the Pass-4 boundary that module already carves (a
module-size seam). :mod:`.passes` re-imports and re-exports every name here, so the historical
``passes.<name>`` and ``orchestrator.<name>`` call sites keep resolving unchanged.

Leaf module: it depends only on the shared review kernel — never back on ``passes`` — so the
extraction introduces no import cycle. (:func:`rebar.llm.plan_review.passes.pass4_coach` STAYS
in ``passes`` because it builds a ``RunRequest`` through that module's ``_resolve_system`` /
``PASS_COACH`` internals; relocating it here would create a circular import.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rebar.llm.review_kernel import DEFAULT_BLOCK_THRESHOLD, validate_move_registry

# ── Pass 4: move registry + coach (rendered deterministically from a locked template) ──
# Pass-4 move registry (moves 1-9,11,12 with LOCKED templates; project-extensible
# via .rebar later — child 75a9). The LLM picks the move + names a {subject}; the
# prose is rendered deterministically from these templates (it never authors prose).
MOVE_REGISTRY: dict[str, dict[str, Any]] = {
    "1": {
        "name": "spike",
        "template": "Consider a short spike to de-risk {subject} before committing the plan.",
    },
    "2": {
        "name": "prior-art research",
        "template": "Research prior art / OSS for {subject} before building it custom.",
    },
    "3": {
        "name": "pre-mortem",
        "template": "Run a quick pre-mortem on {subject}: how could this plan fail?",
    },
    "4": {
        "name": "riskiest-assumption test",
        "template": "Test the riskiest assumption behind {subject} first.",
    },
    "5": {
        "name": "weigh alternatives",
        "template": "Weigh at least one structural alternative for {subject}.",
    },
    "6": {
        "name": "specification by example",
        "template": "Pin down {subject} with a concrete worked example.",
    },
    "7": {
        "name": "thin vertical slice",
        "template": "Prove {subject} end-to-end with a thin vertical slice first.",
    },
    "8": {
        "name": "ADR / one-way-door",
        "template": "Record an ADR for {subject} — it reads like a one-way door.",
    },
    "9": {
        "name": "plan the verification",
        "template": (
            "Plan how {subject} will be verified in-session — restate any deferred or "
            "unobservable success target as an observable proxy."
        ),
    },
    # Operator-attested evidence (epic a8e5, ADR 0043): when an AC's "done" evidence lives OUTSIDE
    # the codebase (a deploy, a live drill), tag it [operator-attested] and record the concrete
    # attestation on the ticket — the completion verifier accepts that over an in-session proof.
    "14": {
        "name": "state attestation evidence",
        "template": (
            "State the concrete attestation evidence the [operator-attested] {subject} will "
            "require (a change id / vote outcome / timestamp), recorded on the ticket."
        ),
    },
    # Foundation/enhancement split (epic cite-stone-sea / WS8): the removal of DEFERRED_MEASUREMENT
    # (counter-architectural — a blocking AC must be in-session-closable). Instead of deferring the
    # measurement inside the current AC, ship the functional goal with existing machinery now and
    # route the ideal version to a DEPENDENT FOLLOW-ON ticket. Scoped (applies_when) to the
    # sizing/complexity/risk criteria where "split by fidelity, not scope" is the productive move —
    # complements move 7 (thin vertical slice). applies_when values are REAL criterion ids (the
    # active triggers are the surviving findings' criteria[]).
    "10": {
        "name": "foundation/enhancement split",
        "template": (
            "Deliver {subject} with existing machinery first; make the ideal version a "
            "dependent follow-on ticket."
        ),
        "applies_when": ["G5", "A1", "T2"],
    },
    "11": {
        "name": "propagate to children",
        "template": "Propagate the revision for {subject} to the child tickets.",
    },
    "12": {
        "name": "generalize the finding",
        "template": "Generalize {subject} across the rest of the work.",
    },
    "13": {
        "name": "realign to parent plan",
        "template": (
            "Realign {subject} to the parent's plan — the parent wins on conflict; if the "
            "parent is genuinely wrong, update the PARENT first (which forces its re-review), "
            "never silently diverge the leaf."
        ),
        "applies_when": ["G7"],
    },
}


def load_move_registry(repo_root=None) -> dict[str, dict[str, Any]]:
    """The Pass-4 move registry INSTANCE plan-review supplies to the shared coach mechanism:
    the built-in :data:`MOVE_REGISTRY` PLUS project extensions from
    ``.rebar/plan_review_moves.json`` (a ``{move_id: {name, template, applies_when?}}`` map; a
    project entry adds a new move or overrides a built-in by id). Validated through the kernel
    move-registry schema (:func:`rebar.llm.review_kernel.validate_move_registry`): the built-ins
    strictly, the project file best-effort (``strict=False`` — a malformed entry is DROPPED, the
    review never crashes). Existing moves declare no ``applies_when`` ⇒ always-applicable."""
    moves = validate_move_registry({mid: dict(m) for mid, m in MOVE_REGISTRY.items()})
    if not repo_root:
        return moves
    try:
        path = Path(repo_root) / ".rebar" / "plan_review_moves.json"
        if path.is_file():
            extra = json.loads(path.read_text(encoding="utf-8"))
            moves.update(validate_move_registry(extra or {}, strict=False))
    except Exception:  # noqa: BLE001 — project move file is best-effort
        pass
    return moves


# ── R6 (epic 6982): deterministic advisory triage ──────────────────────────────────────────
# Report §5.2 found the dominant plan-review leak is advisory LATENCY (4/8 tickets applied a
# surfaced advisory only AFTER claim), not blindness. This deterministic stage triages the
# round's surviving ADVISORY findings into `apply-now` vs `defer` buckets from recorded finding
# fields alone (no LLM, no free prose), so the author knows which advisories are worth applying
# now. It attaches to the returned verdict as `verdict["triage"]`; it is NOT an LLM-picked
# MOVE_REGISTRY entry (a per-finding {subject} template cannot express a ranked bucket split).
# See docs/plan-review-gate.md "Advisory triage" for the ranking rule + the R6 dogfood loop.
APPLY_NOW_MARGIN = 0.10
"""Priority margin below a finding's ``block_threshold`` within which a surviving advisory is
bucketed ``apply-now`` (it came within ``APPLY_NOW_MARGIN`` of blocking). See
``triage_advisories``."""


def triage_advisories(surviving: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministically triage the round's surviving ADVISORY findings into ``apply-now`` vs
    ``defer`` buckets using only recorded finding fields — ``priority`` (= ``validity × impact``,
    always present) and ``block_threshold`` (present on LLM-decided findings; falls back to
    ``DEFAULT_BLOCK_THRESHOLD`` for DET-tier advisories that don't carry it). No model call, no
    free prose ⇒ the same finding set yields byte-identical output (R6, epic 6982).

    Returns one entry per surviving advisory:
    ``{"id", "criteria", "priority", "block_threshold", "bucket", "reason"}``. ``bucket`` is
    ``apply-now`` iff ``priority >= block_threshold - APPLY_NOW_MARGIN`` (else ``defer`` with a
    numeric ``reason``; ``reason`` is ``""`` for ``apply-now``). The list is sorted by ``priority``
    DESC, then ``criteria[0]`` ASC (empty ``criteria`` sorts last via the sentinel ``"~"``), then
    ``id`` ASC. Only findings with ``decision == "advisory"`` are included; blocking findings are
    excluded (they must be remediated regardless, so R6 is advisory-only)."""
    entries: list[dict[str, Any]] = []
    for f in surviving:
        if f.get("decision") != "advisory":
            continue
        priority = float(f.get("priority", 0.0))
        block_threshold = float(f.get("block_threshold", DEFAULT_BLOCK_THRESHOLD))
        criteria = list(f.get("criteria") or [])
        if priority >= block_threshold - APPLY_NOW_MARGIN:
            bucket, reason = "apply-now", ""
        else:
            bucket = "defer"
            reason = (
                f"deferred: priority {priority:.2f} is {block_threshold - priority:.2f} "
                f"below its {block_threshold:.2f} block line"
            )
        entries.append(
            {
                "id": f.get("id"),
                "criteria": criteria,
                "priority": priority,
                "block_threshold": block_threshold,
                "bucket": bucket,
                "reason": reason,
            }
        )
    entries.sort(
        key=lambda e: (-e["priority"], (e["criteria"][0] if e["criteria"] else "~"), str(e["id"]))
    )
    return entries
