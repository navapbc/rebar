"""Pass-3 of the four-pass review framework: the DETERMINISTIC decision (no model).

This is the framework's single decision core, extracted from the plan-review gate
(epic ``vivid-gang-day`` WS1) so every review surface shares ONE interpretation of
the binary sub-answers + severity attributes the verifier (Pass-2) produces. The
model emits NO holistic severity/confidence anywhere in this path — it is pure
arithmetic, fully unit-testable.

For each finding the decision computes:

* **validity** — the graded fraction of the binary sub-answers
  (``yes`` = 1, ``insufficient`` = 0.5, ``no`` = 0) over the answerable graded set;
* **impact** ∈ [0,1] — the mean of the ordinal-mapped severity attributes;
* **priority** — ``validity × impact``;
* the ``block | advisory | dropped | indeterminate`` **decision** against a
  per-criterion ``block_threshold`` (parameterized — a consuming gate passes its
  own posture; the math does not change).

The per-criterion threshold/posture LOOKUP is a consumer concern (it differs by
gate — plan-review reads it from its criteria registry); :func:`pass3_over_findings`
takes that lookup as a callable so the kernel never depends on a gate's registry.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

# ── the binary sub-question vocabulary (the GRADED set; the cited-reference veto is
#    handled separately and is NOT in this set) ─────────────────────────────────────
GRADED_BINARY = (
    "is_verifiable",
    "evidence_entails_finding",
    "path_reachable",
    "impact_follows_necessarily",
    "no_viable_alternative_explanation",
    "no_existing_mitigation",
    "severity_claim_justified",
    # DSO-adopted sub-answers (epic cite-stone-sea / WS1, ADR 0032). Generic GRADED_BINARY
    # entries — they participate in validity() through the SAME uniform loop, no per-criterion
    # branching. Their Binary-model default is "na" (see verify._BINARY_NA_DEFAULT), so a
    # verifier that does not address them abstains (excluded from the mean) rather than
    # dragging validity, and old sidecars that predate them stay comparable.
    "committed_work_relies_on_unbacked_claim",
    "respects_artifact_altitude",
)
_GRADE = {"yes": 1.0, "insufficient": 0.5, "no": 0.0}
_SEV01: dict[str | None, float] = {"none": 0.0, "low": 0.33, "medium": 0.67, "high": 1.0}
_BLAST01: dict[str | None, float] = {"local": 0.33, "module": 0.67, "system": 1.0}
_LIKE01: dict[str | None, float] = {"low": 0.33, "medium": 0.67, "high": 1.0}
_REV01: dict[str | None, float] = {"easy": 0.33, "moderate": 0.67, "hard": 1.0}

DEFAULT_BLOCK_THRESHOLD = 0.95  # near-certain AND high-impact ⇒ v1 is almost all advisory


def validity(binary: dict[str, Any]) -> float:
    """The graded fraction of the binary sub-answers (yes=1, insufficient=.5,
    no=0) over the answerable graded set (excluding any 'na'). The cited-reference
    veto is handled separately. Empty ⇒ 0.0."""
    scores = [
        _GRADE[binary[q]] for q in GRADED_BINARY if binary.get(q) in ("yes", "no", "insufficient")
    ]
    return round(sum(scores) / len(scores), 4) if scores else 0.0


# ── novelty (the remediation re-review carryover axis — child 150b) ──────────────────────────
# The matches-prior sub-answer field set, the analogue of GRADED_BINARY for the validity axis:
# the SINGLE vocabulary the `novelty` contract names AND `novelty()` scores, so the two can
# never drift. Each is a factual yes/insufficient/no question on the same ordinal `_GRADE` map.
NOVELTY_SUBANSWERS: tuple[str, ...] = (
    "restates_prior_defect",  # Q1: same underlying defect as a specific prior finding?
    "cites_prior_location",  # Q2: same plan location/section as that prior finding?
    "matches_prior_fix",  # Q3: substantively the same suggested remediation?
)


def novelty(matches_prior: dict[str, Any]) -> float:
    """NOVELTY ∈ [0,1] = 1 − the graded fraction of the matches-prior sub-answers
    (``carryover_match``). High novelty (≈1.0) = no prior match (genuinely new); low novelty
    (≈0.0) = carryover. A sub-answer is "answerable" only when it is one of yes/insufficient/no
    (the ``_GRADE`` map); a missing/blank/garbage one is skipped from the mean. With NO answerable
    sub-answer, novelty defaults to **0.0** (carryover → never dropped — the safe direction the
    fail-safe mandates)."""
    scores = [
        _GRADE[matches_prior[q]]
        for q in NOVELTY_SUBANSWERS
        if matches_prior.get(q) in ("yes", "no", "insufficient")
    ]
    if not scores:
        return 0.0
    return round(1.0 - sum(scores) / len(scores), 4)


def rising_floor_drop(priority: float, novelty: float, *, t_novel: float, floor: float) -> bool:
    """The Pass-3 RISING-FLOOR drop predicate (child cc5b), deterministic — no LLM holistic
    severity. A finding is dropped IFF it is both NOVEL (``novelty >= t_novel``) AND LOW-PRIORITY
    (``priority < floor``, where ``priority = validity × impact``). The four quadrants:

    - novel + low-priority  → DROP (the only drop case — a fresh, low-stakes finding the edit
      surfaced that would otherwise restart the remediation loop);
    - novel + high-priority → KEEP (a real defect the edit introduced; may block);
    - carryover (low novelty) → KEEP at the normal threshold (it was flagged before and must still
      be resolved — never dropped, regardless of priority);
    - carryover + high-priority → KEEP.

    Pure; the caller supplies the per-finding ``priority``/``novelty`` and the configured
    ``t_novel``/``floor``. The activation guard + eligibility live in the caller."""
    return novelty >= t_novel and priority < floor


def impact(attrs: dict[str, Any]) -> float:
    """IMPACT ∈ [0,1] = mean of the ordinal-mapped severity attributes:
    max(prod_impact, debt_impact), blast_radius, likelihood, reversibility."""
    sev = max(_SEV01.get(attrs.get("prod_impact"), 0.0), _SEV01.get(attrs.get("debt_impact"), 0.0))
    blast = _BLAST01.get(attrs.get("blast_radius"), 0.33)
    like = _LIKE01.get(attrs.get("likelihood"), 0.33)
    rev = _REV01.get(attrs.get("reversibility"), 0.33)
    return round((sev + blast + like + rev) / 4.0, 4)


# ── plan-review impact model (story fishable-apivorous-redhead) ───────────────────────────
# The plan-review gate dispatches `impact_plan` via `impact_fn` (see pass3_decide) INSTEAD of
# the mean `impact`. Rationale: the mean dilutes a genuinely high-severity plan finding below
# the bar (a critical axis averaged with low axes lands ~0.60-0.69). Severity-first MAX + a
# hard-override floor + a detection amplifier fixes that. The seven axes are emitted by
# verify.plan_review_verification_model; a missing axis maps to 0.0 (an older/absent verifier
# ABSTAINS — it never inflates impact). Code-review dispatches its own model (child albite).
_PLAN_SEVERITY_AXES = (
    "ac_unverifiable",
    "dod_uncertifiable",
    "undecomposed",
    "divergent_implementation",
    "internal_conflict",
    "vague_directive",
    "irreversible_without_rationale",
)
# The four axes that mean "the plan will build the wrong thing": ANY of them present makes the
# finding auto-high via a hard floor, regardless of the other axes.
_PLAN_HARD_OVERRIDE_AXES = (
    "ac_unverifiable",
    "dod_uncertifiable",
    "undecomposed",
    "divergent_implementation",
)
_PLAN_HARD_OVERRIDE_FLOOR = 0.85


def impact_plan(attrs: dict[str, Any]) -> float:
    """Plan-review IMPACT ∈ [0,1]: severity-first MAX + hard override + detection amplifier
    (story fishable-apivorous-redhead), dispatched into :func:`pass3_decide` via ``impact_fn``.

    1. ``impact_sev`` = MAX over the seven ordinal-mapped plan-severity axes (no averaging);
    2. DETECTION AMPLIFIER: ``mult`` = 0.8 for a ``self_revealing`` finding, else 1.0; a present
       ``dod_uncertifiable`` forces 1.0 (a DoD you cannot certify is never "self-revealing").
       ``amplified = min(1.0, impact_sev * mult)``;
    3. HARD OVERRIDE (applied LAST, as a floor): if ANY of {ac_unverifiable, dod_uncertifiable,
       undecomposed, divergent_implementation} is present (non-none), the result is floored at
       0.85.

    The override is floored AFTER the amplifier on purpose. The ticket's stated compose
    (``impact_sev = max(impact_sev, 0.85)`` THEN ``× mult``) lets a self-revealing override
    finding land at 0.85 × 0.8 = 0.68 — BELOW the 0.70 bar — silently defeating the "auto-high"
    intent (flagged by this ticket's own plan-review, findings COH/E1/G6). Flooring last
    guarantees an override finding is always ≥ 0.85, mirroring impact_code's reversibility
    floor. All three mechanisms (MAX, override, amplifier) are present, per AC2."""
    contribs = [_SEV01.get(attrs.get(a), 0.0) for a in _PLAN_SEVERITY_AXES]
    impact_sev = max(contribs) if contribs else 0.0
    mult = 0.8 if attrs.get("silent_vs_self_revealing") == "self_revealing" else 1.0
    if _SEV01.get(attrs.get("dod_uncertifiable"), 0.0) > 0.0:
        mult = 1.0  # a DoD you cannot certify forces full detection weight
    amplified = min(1.0, impact_sev * mult)
    has_override = any(_SEV01.get(attrs.get(a), 0.0) > 0.0 for a in _PLAN_HARD_OVERRIDE_AXES)
    result = max(amplified, _PLAN_HARD_OVERRIDE_FLOOR) if has_override else amplified
    return round(result, 4)


def severity_label(imp: float) -> str:
    if imp >= 0.75:
        return "critical"
    if imp >= 0.5:
        return "major"
    if imp >= 0.25:
        return "minor"
    return "none"


def pass3_decide(
    verification: dict[str, Any] | None,
    *,
    block_threshold: float = DEFAULT_BLOCK_THRESHOLD,
    blocking_enabled: bool = False,
    impact_fn: Callable[[dict[str, Any]], float] | None = None,
) -> dict[str, Any]:
    """The deterministic decision. Returns
    ``{decision, reason, validity, impact, priority, severity}``.

    Rules (the v1 authoritative shape):
      * no verification → INDETERMINATE (verifier produced nothing for this finding);
      * cited_reference_accurate == "no" → DROPPED (the only veto, fires only when a
        code citation is present);
      * validity < 0.5 → DROPPED (low validity);
      * else BLOCK iff (not vetoed) AND blocking_enabled AND priority ≥ block_threshold;
      * else ADVISORY.

    ``impact_fn`` is the PER-GATE impact model (story fishable-apivorous-redhead). It defaults
    to the mean :func:`impact` — so any caller that does not pass it (e.g. the code-review path
    today) is byte-unchanged — while the plan-review gate threads ``impact_fn=impact_plan`` and
    code-review later threads its own. The signed-verdict shape is identical either way; only
    the ``impact`` scalar's provenance differs."""
    if not verification:
        return {
            "decision": "indeterminate",
            "reason": "no-verification",
            "validity": 0.0,
            "impact": 0.0,
            "priority": 0.0,
            "severity": "none",
        }
    binary = verification.get("binary", {}) or {}
    attrs = verification.get("severity_attributes", {}) or {}
    val = validity(binary)
    imp = (impact_fn or impact)(attrs)
    priority = round(val * imp, 4)
    sev = severity_label(imp)
    if binary.get("cited_reference_accurate") == "no":
        return {
            "decision": "dropped",
            "reason": "veto:cited-reference-inaccurate",
            "validity": val,
            "impact": imp,
            "priority": priority,
            "severity": sev,
        }
    if val < 0.5:
        decision, reason = "dropped", "low-validity"
    elif blocking_enabled and priority >= block_threshold:
        decision, reason = "block", "high-priority+criterion-opted-in"
    else:
        decision, reason = "advisory", "default-advisory"
    return {
        "decision": decision,
        "reason": reason,
        "validity": val,
        "impact": imp,
        "priority": priority,
        "severity": sev,
    }


# A per-finding threshold resolver: given a finding's criteria id list, return
# ``(block_threshold, blocking_enabled)``. The LOOKUP is a consumer concern (it reads
# the gate's own criteria registry/posture) — the kernel takes it as a callable so the
# decision math is shared while the per-criterion posture stays parameterized per gate.
ThresholdResolver = Callable[[Sequence[str]], tuple[float, bool]]


def pass3_over_findings(
    findings: list[dict[str, Any]],
    verifs: dict[int, dict[str, Any]],
    *,
    threshold_for: ThresholdResolver,
    impact_fn: Callable[[dict[str, Any]], float] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic Pass-3 over the verifiable findings: per-criterion thresholds
    (resolved by the consumer-supplied ``threshold_for``) + :func:`pass3_decide`
    keyed by each finding's index into ``findings`` (matching the
    ``{index: verification}`` map Pass-2 produced). The shared decision core every
    gate calls — the too_big/shed routing is the caller's (it differs by
    index-domain).

    ``impact_fn`` (story fishable-apivorous-redhead) is threaded verbatim to
    :func:`pass3_decide` — the plan-review wrapper passes ``impact_plan``; a caller that
    omits it gets the mean :func:`impact` unchanged."""
    decided: list[dict[str, Any]] = []
    for i, f in enumerate(findings):
        block_threshold, blocking_enabled = threshold_for(f.get("criteria", []))
        d = pass3_decide(
            verifs.get(i),
            block_threshold=block_threshold,
            blocking_enabled=blocking_enabled,
            impact_fn=impact_fn,
        )
        decided.append({**f, **d, "verification": verifs.get(i), "tier": "LLM"})
    return decided
