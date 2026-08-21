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
    # R5 (story empty-microbial-antlion, epic pastoral-aquatic-viper). The code-review
    # counterpart to R1's plan-review asserted-capability probe: R1 flags "the plan claims a
    # capability an existing module lacks"; this VERIFIES, at code-review time, a finding that
    # asserts a claimed capability is NOT delivered — same yes=finding-holds polarity as the rest
    # of the graded set (yes = the gap is confirmed, finding stands; no = the capability IS
    # delivered, so the false non-delivery claim is refuted and drops). CONSERVATIVE scope — its
    # Binary default is "na" (see verify_models._BINARY_NA_DEFAULT) AND it is answered non-na ONLY
    # for findings in the asserted-capability cohort (G6/E4/T3); everywhere else it stays "na" and
    # abstains from validity() (excluded from the mean), so adding it is byte-identical for every
    # finding outside the cohort and every pre-R5 sidecar (proven by E5's non-regression replay).
    "asserted_capability_confirmed",
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


def drift_floor_drop(
    novelty: float,
    *,
    cited_paths: Any,
    drifted_files: Any,
    t_novel: float,
) -> bool:
    """The Pass-3 DRIFT-FLOOR drop predicate (bug 5e40), the code-drift-axis analogue of
    :func:`rising_floor_drop`. It converges a re-review that fired ONLY because HEAD/code drifted
    under an already-signed, plan-UNCHANGED attestation (the whole-HEAD-invalidation "stale-head"
    re-review). A finding is dropped IFF it is NOVEL (``novelty >= t_novel``) AND its citations do
    NOT intersect the ``drifted_files`` set. The quadrants:

    - novel + cites NO drifted file → DROP (a non-determinism artifact: the plan text is
      byte-identical to what was already signed PASS and this finding does not touch the code that
      changed, so it cannot be a genuine NEW defect — dropping it converges the re-review to its
      prior PASS);
    - novel + cites a drifted file → KEEP (a genuine code-drift signal — this per-finding drift
      guard is exactly what preserves the code-drift detection the whole-HEAD invalidation exists
      for, which the abandoned "stop invalidating on drift" fix destroyed);
    - carryover (``novelty < t_novel``) → KEEP (it was flagged before and must still be resolved).

    Unlike :func:`rising_floor_drop` there is deliberately NO priority floor on this axis: because
    the plan is byte-identical to the signed-PASS baseline, the SOUND per-finding guard is drift
    intersection, not priority — a novel HIGH-priority finding that does not touch drifted code is
    precisely the false-positive block 5e40 is about. Pure; the caller supplies the per-finding
    ``novelty`` + ``cited_paths``, the ``drifted_files`` set, and the configured ``t_novel``."""
    if novelty < t_novel:
        return False
    return not (set(cited_paths) & set(drifted_files))


def contradiction_drop_index(pair: Any, priorities: list[float]) -> int | None:
    """The intra-verdict CONTRADICTION drop predicate (bug 5e40, validation assessment),
    deterministic — no LLM holistic re-judgement. Given ONE pairwise contradiction judgment
    ``{a, b, contradiction, drop}`` (produced by the detection sub-call) and the verdict's
    per-finding ``priorities`` (validity × impact, combined-index-aligned), return the combined
    index to DROP, or ``None`` when nothing should be dropped.

    A drop happens ONLY when ``contradiction is True`` and ``a``/``b`` are two distinct in-range
    indices (any other shape → ``None``, the fail-safe KEEP). WHICH member is dropped is:

    - the model-identified contradicted member ``drop`` when it names one of the pair — this is the
      5e40 A1 case: a FALSE blocking finding (higher priority) contradicted by a TRUE advisory
      (lower priority); dropping "the weaker/contradicted one" must follow the contradiction, not
      the priority, so the false BLOCK is the one removed;
    - otherwise a deterministic TIEBREAK: drop the LOWER-priority member (the "weaker" one); on a
      priority tie, the higher index.

    Pure; the caller supplies the (injected) judgment and priorities. Never raises."""
    if not isinstance(pair, dict) or pair.get("contradiction") is not True:
        return None
    a, b = pair.get("a"), pair.get("b")
    n = len(priorities)
    if not (isinstance(a, int) and isinstance(b, int) and 0 <= a < n and 0 <= b < n and a != b):
        return None
    drop = pair.get("drop")
    if isinstance(drop, int) and drop in (a, b):
        return drop
    if priorities[a] < priorities[b]:
        return a
    if priorities[b] < priorities[a]:
        return b
    return max(a, b)  # priority tie → deterministic: the higher index


def comment_trail_drop(answer: Any) -> bool:
    """The COMMENT-TRAIL consultation drop predicate (bug 5e40, validation assessment),
    deterministic. Drop a finding IFF its per-finding sub-answer says the point it raises is
    ALREADY RESOLVED in the ticket's recorded comment trail (``resolved_in_trail == "yes"`` — the
    5e40 B3 case where a review round already CONCEDED the point). Every other value —
    ``"no"``/``"insufficient"``, a missing key, a malformed answer — KEEPS the finding (fail-safe:
    a broken signal can only make the gate stricter, never suppress). Pure; the sub-answer is
    injected. Never raises."""
    return isinstance(answer, dict) and answer.get("resolved_in_trail") == "yes"


# ── Discovery-stage deterministic narrowing (bug 5e40 B2/B4) ──────────────────────────
# Two PURE, LLM-free filters applied when the discovered findings are first bucketed
# (``orchestrator.partition_findings``): suppress findings that carry no actionable content
# (B4) and collapse exact duplicates (B2). Fully deterministic — an empty body / missing
# checklist_item and an identical (criterion + location + body) pair are decidable without a
# model — so they run by default (dropping only non-actionable / redundant findings is a
# strict improvement), unlike the LLM-detected floors above.

_BODY_KEYS = ("finding", "detail", "body", "description")


def _norm_text(value: Any) -> str:
    """Whitespace-collapsed, case-folded text — the normal form both filters compare on.
    Non-strings normalize to ``""``. Pure; never raises."""
    return " ".join(str(value or "").split()).strip().lower()


def finding_body(f: dict[str, Any]) -> str:
    """The finding's human-readable body, tolerant of the two live finding shapes: plan-review
    findings key it ``finding``; the ``ReviewResult`` finding schema keys it ``detail`` (with
    ``body``/``description`` accepted as further fallbacks). Returns the first non-blank value,
    else ``""``. Pure; never raises."""
    for k in _BODY_KEYS:
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _checklist_text(f: dict[str, Any]) -> str:
    """The actionable text of a finding's ``checklist_item``, with a leading markdown
    checkbox/list marker (``- [ ]``, ``* [x]``, ``- ``) stripped — a bare unchecked box carries
    no action. Returns ``""`` when nothing actionable remains. Pure; never raises."""
    raw = f.get("checklist_item")
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    # Drop a leading bullet marker then an optional checkbox, whatever remains is the action.
    if text[:1] in "-*+":
        text = text[1:].strip()
    if text[:3].lower() in ("[ ]", "[x]"):
        text = text[3:].strip()
    return text


def is_contentless_finding(f: dict[str, Any]) -> bool:
    """B4: True iff the finding carries NO actionable content — an empty/whitespace-only body
    AND no actionable ``checklist_item``. A finding with a real body OR a real checklist item is
    kept (either is actionable), so this drops only genuinely empty findings — a strict
    improvement. Pure; never raises."""
    return not _norm_text(finding_body(f)) and not _norm_text(_checklist_text(f))


def dedup_key(f: dict[str, Any]) -> tuple[tuple[str, ...], str, str]:
    """B2: the equivalence key two findings are duplicates under — same criterion set, same
    location, equivalent (normalized) body. Criteria are order-insensitive; location and body are
    whitespace/case normalized. Pure; never raises."""
    criteria = tuple(sorted(_norm_text(c) for c in (f.get("criteria") or []) if _norm_text(c)))
    return (criteria, _norm_text(f.get("location")), _norm_text(finding_body(f)))


def suppress_and_dedup(
    findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Discovery-stage narrowing (bug 5e40 B2/B4): partition ``findings`` into ``(kept, dropped)``
    in stable input order. A finding is dropped when it is contentless (B4, stamped
    ``drop_reason="contentless"``) or a later duplicate of an already-kept finding (B2, stamped
    ``drop_reason="duplicate"``); the FIRST occurrence of a duplicate group is kept. Contentless
    is checked first, so an empty duplicate is recorded as contentless. Findings already carrying
    a ``drop_reason`` are left untouched (a prior stage owns them). Pure and deterministic — no
    LLM, no I/O; a findings list with no contentless/duplicate members returns ``(findings, [])``
    unchanged. Never raises."""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], str, str]] = set()
    for f in findings:
        if is_contentless_finding(f):
            dropped.append({**f, "drop_reason": "contentless"})
            continue
        key = dedup_key(f)
        if key in seen:
            dropped.append({**f, "drop_reason": "duplicate"})
            continue
        seen.add(key)
        kept.append(f)
    return kept, dropped


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
# ac_unverifiable is graded by ORACLE KIND, not the ordinal severity ladder (story
# large-sleepful-needlefish, calibration-3 evidence: 56% of its floor-driven blocks demanded
# only a more specific command/file/value). broken/missing keep the hard floor;
# underspecified contributes below every blocking threshold and never floors.
# INVARIANT: UNDERSPECIFIED_ORACLE_CONTRIB stays strictly below the lowest blocking
# block_threshold in plan_review/criteria_routing.json (0.60 after calibration 3) — pinned by
# test_impact_plan.py so a future recalibration below it fails loudly.
UNDERSPECIFIED_ORACLE_CONTRIB = 0.55
ORACLE_GRADE01: dict[str | None, float] = {
    "none": 0.0,
    "underspecified_oracle": UNDERSPECIFIED_ORACLE_CONTRIB,
    "broken_oracle": 1.0,
    "missing_oracle": 1.0,
}
_ORACLE_FLOOR_GRADES = ("broken_oracle", "missing_oracle")
# divergent_implementation is likewise graded by DIVERGENCE KIND, not the ordinal ladder (story
# doggish-nonorganic-tsetsefly, plan-v4). Field evidence from the plan-v3 corpus: the axis fired on
# only 7.72% of findings, and in the "omitted scope site / unenumerated consumer" class it exists to
# describe it was graded `none` ~90% of the time (1173/1307) — so a plan that provably under-scopes
# reality scored impact 0.0 and could not block. Grading the axis, rather than merely widening it,
# follows the ac_unverifiable precedent: a blunt widening would have routed 113 corpus findings into
# the 0.85 floor at once (4.3% of runs flipping PASS→BLOCK), the same over-fire calibration 3 had to
# walk back. contradicts_reality/omits_required_site keep the floor; incomplete_enumeration (the
# omitted site is optional/cosmetic — docs, comments, a redundant mention — and the goal still
# holds) is coached, never auto-blocked.
# INVARIANT: DIVERGENCE_INCOMPLETE_CONTRIB stays strictly below the lowest blocking block_threshold
# in plan_review/criteria_routing.json (0.60) — pinned by test_impact_plan.py, same as
# UNDERSPECIFIED_ORACLE_CONTRIB, so a future recalibration below it fails loudly.
DIVERGENCE_INCOMPLETE_CONTRIB = 0.55
DIVERGENCE_GRADE01: dict[str | None, float] = {
    "none": 0.0,
    "incomplete_enumeration": DIVERGENCE_INCOMPLETE_CONTRIB,
    "contradicts_reality": 1.0,
    "omits_required_site": 1.0,
}
_DIVERGENCE_FLOOR_GRADES = ("contradicts_reality", "omits_required_site")
# undecomposed is graded by DECOMPOSITION KIND, not the ordinal ladder (story
# fixable-angular-caribou, plan-v5). Ordinal labels are an LLM anti-pattern: models do not apply
# none|low|medium|high reliably enough for deterministic gate behavior, so the ladder is replaced
# by narrow semantic kinds that map to floor-vs-advisory in code. Field evidence from the recorded
# corpus: all 30 gradings were read, and 23 say "this plan bundles N independently-releasable
# outcomes" — a right-sizing observation on a plan that is executable as written — yet EVERY
# non-none grade floored to 0.85 and 4 of those blocked. missing_required_child (the plan or its
# parent commits to work that has no corresponding child/sibling) and no_executable_breakdown (no
# executable step sequence for the unit's own scope, or an all-or-nothing build whose riskiest
# unknown is never de-risked first) are genuine gaps and keep the floor; bundles_separable_slices
# is coached, never auto-blocked.
# INVARIANT: UNDECOMPOSED_BUNDLED_CONTRIB stays strictly below the lowest blocking block_threshold
# in plan_review/criteria_routing.json (0.60) — pinned by test_impact_plan.py, same as
# UNDERSPECIFIED_ORACLE_CONTRIB, so a future recalibration below it fails loudly.
UNDECOMPOSED_BUNDLED_CONTRIB = 0.55
UNDECOMPOSED_GRADE01: dict[str | None, float] = {
    "none": 0.0,
    "bundles_separable_slices": UNDECOMPOSED_BUNDLED_CONTRIB,
    "missing_required_child": 1.0,
    "no_executable_breakdown": 1.0,
}
_UNDECOMPOSED_FLOOR_GRADES = ("missing_required_child", "no_executable_breakdown")
# dod_uncertifiable is graded by CERTIFICATION KIND, not the ordinal ladder (same story, plan-v5).
# Its 1,413 recorded gradings mix three semantically distinct defects under one grade-blind floor:
# no oracle exists at all (uncertifiable_outcome), a stated oracle is factually broken or
# vacuously satisfiable (certification_cannot_prove), and an oracle exists but its exact
# command/path/assertion is not spelled out (underspecified_certification). The first two are
# genuine gaps and keep the floor; the third is a specificity demand and is coached, exactly as
# underspecified_oracle is on the ac_unverifiable axis.
# INVARIANT: DOD_UNDERSPECIFIED_CONTRIB stays strictly below the lowest blocking block_threshold
# in plan_review/criteria_routing.json (0.60) — pinned by test_impact_plan.py.
DOD_UNDERSPECIFIED_CONTRIB = 0.55
DOD_GRADE01: dict[str | None, float] = {
    "none": 0.0,
    "underspecified_certification": DOD_UNDERSPECIFIED_CONTRIB,
    "uncertifiable_outcome": 1.0,
    "certification_cannot_prove": 1.0,
}
_DOD_FLOOR_GRADES = ("uncertifiable_outcome", "certification_cannot_prove")
# The axes graded by a CLOSED KIND SET rather than the ordinal none|low|medium|high ladder.
# All are hard-override axes whose floor is decided by grade, so all are special-cased out of the
# generic _SEV01 loops in impact_plan.
_PLAN_GRADED_AXES = (
    "ac_unverifiable",
    "divergent_implementation",
    "undecomposed",
    "dod_uncertifiable",
)
# Every graded axis's kind->contribution map, keyed by axis. `_SEV01` maps ONLY the ordinal
# vocabulary, so it returns 0.0 for any kind name — reading a graded axis through it silently
# scores the axis at zero. Membership in `_PLAN_GRADED_AXES` keeps them out of the generic
# `_SEV01` loops in impact_plan; this table is how they are scored instead.
_PLAN_GRADE_MAPS: dict[str, dict[str | None, float]] = {
    "ac_unverifiable": ORACLE_GRADE01,
    "divergent_implementation": DIVERGENCE_GRADE01,
    "undecomposed": UNDECOMPOSED_GRADE01,
    "dod_uncertifiable": DOD_GRADE01,
}
# Per-axis floor grades, same keying — the kinds that trigger the 0.85 hard override.
_PLAN_FLOOR_GRADES: dict[str, tuple[str, ...]] = {
    "ac_unverifiable": _ORACLE_FLOOR_GRADES,
    "divergent_implementation": _DIVERGENCE_FLOOR_GRADES,
    "undecomposed": _UNDECOMPOSED_FLOOR_GRADES,
    "dod_uncertifiable": _DOD_FLOOR_GRADES,
}


def impact_plan(attrs: dict[str, Any]) -> float:
    """Plan-review IMPACT ∈ [0,1]: severity-first MAX + hard override + detection amplifier
    (story fishable-apivorous-redhead), dispatched into :func:`pass3_decide` via ``impact_fn``.

    1. ``impact_sev`` = MAX over the seven ordinal-mapped plan-severity axes (no averaging);
    2. DETECTION AMPLIFIER: ``mult`` = 0.8 for a ``self_revealing`` finding, else 1.0; a present
       ``dod_uncertifiable`` forces 1.0 (a DoD you cannot certify is never "self-revealing").
       ``amplified = min(1.0, impact_sev * mult)``;
    3. HARD OVERRIDE (applied LAST, as a floor): the result is floored at 0.85 when any
       hard-override axis is graded at a FLOOR kind. As of plan-v5 all four override axes are
       graded by a CLOSED KIND SET rather than the ordinal ladder (``_PLAN_GRADED_AXES``), each
       with one below-threshold kind that is coached instead of auto-blocked:

       * ac_unverifiable by ORACLE KIND (``ORACLE_GRADE01``, plan-v3, story
         large-sleepful-needlefish) — broken/missing floor; underspecified_oracle contributes
         ``UNDERSPECIFIED_ORACLE_CONTRIB``;
       * divergent_implementation by DIVERGENCE KIND (``DIVERGENCE_GRADE01``, plan-v4, story
         doggish-nonorganic-tsetsefly) — contradicts_reality/omits_required_site floor;
         incomplete_enumeration contributes ``DIVERGENCE_INCOMPLETE_CONTRIB``;
       * undecomposed by DECOMPOSITION KIND (``UNDECOMPOSED_GRADE01``, plan-v5, story
         fixable-angular-caribou) — missing_required_child/no_executable_breakdown floor;
         bundles_separable_slices contributes ``UNDECOMPOSED_BUNDLED_CONTRIB``;
       * dod_uncertifiable by CERTIFICATION KIND (``DOD_GRADE01``, plan-v5, same story) —
         uncertifiable_outcome/certification_cannot_prove floor; underspecified_certification
         contributes ``DOD_UNDERSPECIFIED_CONTRIB``.

       Every one of those contributions sits below every blocking threshold, so each axis's
       specificity-only grade is coached rather than auto-blocked. Ordinal labels were retired
       here because models do not apply none|low|medium|high reliably enough for deterministic
       gate behavior; a kind maps to its consequence in code.

    The override is floored AFTER the amplifier on purpose. The ticket's stated compose
    (``impact_sev = max(impact_sev, 0.85)`` THEN ``× mult``) lets a self-revealing override
    finding land at 0.85 × 0.8 = 0.68 — BELOW the 0.70 bar — silently defeating the "auto-high"
    intent (flagged by this ticket's own plan-review, findings COH/E1/G6). Flooring last
    guarantees an override finding is always ≥ 0.85, mirroring impact_code's reversibility
    floor. All three mechanisms (MAX, override, amplifier) are present, per AC2."""
    contribs = [
        _SEV01.get(attrs.get(a), 0.0) for a in _PLAN_SEVERITY_AXES if a not in _PLAN_GRADED_AXES
    ]
    contribs.extend(
        _PLAN_GRADE_MAPS[a].get(attrs.get(a), 0.0)
        for a in _PLAN_SEVERITY_AXES
        if a in _PLAN_GRADED_AXES
    )
    impact_sev = max(contribs) if contribs else 0.0
    mult = 0.8 if attrs.get("silent_vs_self_revealing") == "self_revealing" else 1.0
    # A DoD you cannot certify forces full detection weight. dod_uncertifiable is KIND-graded
    # (plan-v5), so this reads DOD_GRADE01 — `_SEV01` would return 0.0 for every kind name and
    # the force would silently stop firing.
    if DOD_GRADE01.get(attrs.get("dod_uncertifiable"), 0.0) > 0.0:
        mult = 1.0
    amplified = min(1.0, impact_sev * mult)
    has_override = any(
        _SEV01.get(attrs.get(a), 0.0) > 0.0
        if a not in _PLAN_GRADED_AXES
        else attrs.get(a) in _PLAN_FLOOR_GRADES[a]
        for a in _PLAN_HARD_OVERRIDE_AXES
    )
    result = max(amplified, _PLAN_HARD_OVERRIDE_FLOOR) if has_override else amplified
    return round(result, 4)


# ── code-review impact model (story albite-lazy-barb) ─────────────────────────────────────
# The code-review gate dispatches `impact_code` via `impact_fn` (see pass3_decide) INSTEAD of
# the mean `impact`. Rationale: production-severity axes + a mean structurally mis-measure
# code-review's maintainability / latent-regression findings (a feature left silently dead in
# prod — untested wiring — scores ~0.4 because prod_impact is 'none' and reversibility 'easy'
# drag it down; landmines and nits overlap so no scalar threshold separates them). A two-lane,
# tier-tagged, severity-first MAX model fixes that. Each consequence binary (emitted by
# verify.code_review_verification_model) is assigned EXACTLY one lane + one tier; a missing
# binary is False (an older/absent verifier ABSTAINS — it never inflates). `churn90` and
# `hard_to_reverse_surface` are DET-enriched into attrs by code_review_decide (best-effort).
_CODE_TIER_MINOR = 0.3
_CODE_TIER_MODERATE = 0.6
_CODE_TIER_SERIOUS = 0.9
# consequence binary -> tier value, within the PRODUCTION lane (correctness / latent regression).
_CODE_PROD_BINARIES = {
    "data_loss_without_recovery": _CODE_TIER_SERIOUS,
    "security_bypass_not_enforced_elsewhere": _CODE_TIER_SERIOUS,
    "silent_wrong_feeding_a_decision": _CODE_TIER_SERIOUS,
    "capability_degraded": _CODE_TIER_MODERATE,
}
# code-v4 splits the old maintainability lane into three sub-lanes: SERIOUS (undamped, no churn),
# MODERATE (prod_impact-keyed), and DEBT (churn is an amplifier only).
_CODE_SERIOUS_MAINT_BINARIES = {
    "unversioned_published_contract_break": _CODE_TIER_SERIOUS,
    "safety_net_removal_without_replacement": _CODE_TIER_SERIOUS,
    "forbids_contract_allowed_state": _CODE_TIER_SERIOUS,
}
_CODE_MODERATE_MAINT_BINARIES = {
    "contract_drift": _CODE_TIER_MODERATE,
    "hidden_invariant": _CODE_TIER_MODERATE,
    "reachable_path_without_automated_coverage": _CODE_TIER_MODERATE,
}
_CODE_DEBT_BINARIES = {
    "implicit_coupling": _CODE_TIER_MINOR,
    "dead_code": _CODE_TIER_MINOR,
}
# trigger-likelihood multiplier on the PRODUCTION lane. Absent ⇒ "common" (1.0).
_CODE_PROD_TRIGGER_MULT = {"common": 1.0, "sometimes": 0.6, "rare": 0.25}
# trigger-likelihood multiplier on the MODERATE-maint lane (a DIFFERENT map, same source field).
_CODE_MODERATE_TRIGGER_MULT = {"rare": 0.75, "sometimes": 1.0, "common": 1.0}
# prod_impact multiplier on the MODERATE-maint lane (reach of the guarded path). Absent ⇒ none.
_CODE_PROD_IMPACT_MULT = {"high": 1.0, "medium": 1.0, "low": 0.6, "none": 0.5}
_CODE_REVERSIBILITY_FLOOR = 0.6


def _code_truthy(v: Any) -> bool:
    """A consequence binary is TRUE only for boolean-true or the string 'true'/'yes'. Everything
    else (absent, False, '', 'no', 'none') is False so a missing binary ABSTAINS (no inflation)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes"}
    return False


def _code_lane_severity(attrs: dict[str, Any], binaries: dict[str, float]) -> float:
    """MAX tier value over the TRUE binaries in one lane (0.0 if none present) — no dilution and
    no cross-binary compounding (a conservative default: a minor/moderate binary cannot alone
    reach the block zone)."""
    contribs = [tier for name, tier in binaries.items() if _code_truthy(attrs.get(name))]
    return max(contribs) if contribs else 0.0


def _code_churn_amp(attrs: dict[str, Any]) -> float:
    """Debt-lane churn AMPLIFIER ∈ [1.0, 1.5]: ``1.0 + 0.5·min(churn90, 30)/30``. churn=0 ⇒ 1.0
    (never halves impact); a non-int / negative value falls back to 0 defensively."""
    try:
        churn = max(0, int(attrs.get("churn90", 0)))
    except (TypeError, ValueError):
        churn = 0
    return 1.0 + 0.5 * min(churn, 30) / 30.0


def _code_prod_lane(attrs: dict[str, Any]) -> float:
    mult = _CODE_PROD_TRIGGER_MULT.get(attrs.get("trigger_likelihood", "common"), 1.0)
    return _code_lane_severity(attrs, _CODE_PROD_BINARIES) * mult


def _code_serious_maint_lane(attrs: dict[str, Any]) -> float:
    """SERIOUS-maint lane severity (undamped), PLUS the code-v5 removed-public-symbol boost
    (ticket 5452-3077-b34a-4157): a removal of a PUBLICLY EXPORTED symbol
    (``removed_public_symbol``, keyed on the EXPORT by the Pass-2 sub-question) that carries NO
    version/deprecation signal (``version_signal_present`` false) is the same consequence as an
    unversioned published contract break, so it earns the serious tier deterministically even
    when the verifier did not also set that binary. Abstain-safe and AMPLIFY-ONLY: both
    sub-answers default False (an older verifier never trips it), and a MANAGED removal
    (signal present) never boosts — nor does either field ever lower a score another
    binary already earned (this is a ``max``)."""
    sev = _code_lane_severity(attrs, _CODE_SERIOUS_MAINT_BINARIES)
    if _code_truthy(attrs.get("removed_public_symbol")) and not _code_truthy(
        attrs.get("version_signal_present")
    ):
        sev = max(sev, _CODE_TIER_SERIOUS)
    return sev


def _code_moderate_maint_lane(attrs: dict[str, Any]) -> float:
    sev = _code_lane_severity(attrs, _CODE_MODERATE_MAINT_BINARIES)
    prod_mult = _CODE_PROD_IMPACT_MULT.get(attrs.get("prod_impact", "none"), 0.5)
    trig_mult = _CODE_MODERATE_TRIGGER_MULT.get(attrs.get("trigger_likelihood", "common"), 1.0)
    return sev * prod_mult * trig_mult


def impact_code(attrs: dict[str, Any]) -> float:
    """Code-review IMPACT ∈ [0,1]: code-v4 four-lane, tier-tagged, severity-first MAX with a
    detection amplifier and a consequence-lane-gated reversibility floor (bug
    obese-dihedral-ermine). Dispatched into :func:`pass3_decide` via ``impact_fn``. Lanes:
    prod (trigger-keyed), serious-maint (undamped), moderate-maint (prod_impact × trigger keyed),
    and debt (churn amplifier only). ``impact_base`` = MAX over all four; ``consequence_base`` =
    MAX over all but debt. ``amp`` = 1.0 if silent (``silent_failure``/``escapes_automation``)
    else 0.8. The 0.6 floor fires only when ``consequence_base > 0`` AND the change touches a
    hard-to-reverse surface — debt alone NEVER floors."""
    prod_lane = _code_prod_lane(attrs)
    serious_maint_lane = _code_serious_maint_lane(attrs)
    moderate_maint_lane = _code_moderate_maint_lane(attrs)
    debt_lane = _code_lane_severity(attrs, _CODE_DEBT_BINARIES) * _code_churn_amp(attrs)
    impact_base = max(prod_lane, serious_maint_lane, moderate_maint_lane, debt_lane)
    consequence_base = max(prod_lane, serious_maint_lane, moderate_maint_lane)
    silent = _code_truthy(attrs.get("silent_failure")) or _code_truthy(
        attrs.get("escapes_automation")
    )
    amp = 1.0 if silent else 0.8
    rev_floor = (
        _CODE_REVERSIBILITY_FLOOR
        if consequence_base > 0.0 and _code_truthy(attrs.get("hard_to_reverse_surface"))
        else 0.0
    )
    result = max(min(1.0, impact_base * amp), rev_floor)
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
    execution_review: bool = False,
) -> dict[str, Any]:
    """The deterministic decision. Returns
    ``{decision, reason, validity, impact, priority, severity, block_threshold,
    blocking_enabled}`` — the last two echo back the exact decision boundary the
    finding was judged against (persisted losslessly by the sidecar).

    Rules (the v1 authoritative shape):
      * no verification → INDETERMINATE (verifier produced nothing for this finding);
      * cited_reference_accurate == "no" → DROPPED (a veto, fires only when a
        code citation is present);
      * absence-claim refuted → DROPPED (the a8e5 absence veto);
      * ``execution_review`` AND current_state_satisfies_plan_goal == "yes" → DROPPED
        (the on-target veto — an execution-phase re-review found the code already at the
        plan's directed end state, so the finding only re-reports completed work);
      * validity < 0.5 → DROPPED (low validity);
      * else BLOCK iff (not vetoed) AND blocking_enabled AND priority ≥ block_threshold;
      * else ADVISORY.

    ``impact_fn`` is the PER-GATE impact model (story fishable-apivorous-redhead). It defaults
    to the mean :func:`impact` — so any caller that does not pass it (e.g. the code-review path
    today) is byte-unchanged — while the plan-review gate threads ``impact_fn=impact_plan`` and
    code-review later threads its own. The signed-verdict shape is identical either way; only
    the ``impact`` scalar's provenance differs.

    ``execution_review`` (default False) enables the on-target veto ONLY for a plan-review
    EXECUTION re-review — a planning-phase review and every code-review call leave it False, so
    a genuine planning-stage true positive (e.g. "remove X that never existed") is byte-unchanged.
    """
    if not verification:
        return {
            "decision": "indeterminate",
            "reason": "no-verification",
            "validity": 0.0,
            "impact": 0.0,
            "priority": 0.0,
            "severity": "none",
            "block_threshold": block_threshold,
            "blocking_enabled": blocking_enabled,
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
            "block_threshold": block_threshold,
            "blocking_enabled": blocking_enabled,
        }
    # a8e5 Component 1: absence-claim veto — the finding is premised on an absence
    # ("claims_absence" == "yes") that the verifier REFUTED by finding a covering provision in
    # the plan ("absence_confirmed_in_context" == "no"). Mirrors the cited-reference veto: a
    # conditional drop that fires only on a DEFINITE refutation ("insufficient"/"yes" never veto),
    # so an older/absent verifier (both default "na") is byte-unchanged.
    if binary.get("claims_absence") == "yes" and binary.get("absence_confirmed_in_context") == "no":
        return {
            "decision": "dropped",
            "reason": "veto:absence-refuted",
            "validity": val,
            "impact": imp,
            "priority": priority,
            "severity": sev,
            "block_threshold": block_threshold,
            "blocking_enabled": blocking_enabled,
        }
    # Execution-phase on-target veto: only during a plan-review EXECUTION re-review, a finding
    # that the code lacks (or already contains) something the plan directs is DROPPED when the
    # verifier confirms the code already SATISFIES the plan's directed end state
    # ("current_state_satisfies_plan_goal" == "yes") — the finding merely re-reports completed,
    # on-target work. A DEFINITE confirmation only ("insufficient"/"no"/"na" never veto); the
    # field is na-default and plan-review-only, and execution_review is False for planning
    # reviews + every code-review call, so those paths are byte-unchanged.
    if execution_review and binary.get("current_state_satisfies_plan_goal") == "yes":
        return {
            "decision": "dropped",
            "reason": "veto:plan-goal-satisfied",
            "validity": val,
            "impact": imp,
            "priority": priority,
            "severity": sev,
            "block_threshold": block_threshold,
            "blocking_enabled": blocking_enabled,
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
        "block_threshold": block_threshold,
        "blocking_enabled": blocking_enabled,
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
    execution_review: bool = False,
) -> list[dict[str, Any]]:
    """Deterministic Pass-3 over the verifiable findings: per-criterion thresholds
    (resolved by the consumer-supplied ``threshold_for``) + :func:`pass3_decide`
    keyed by each finding's index into ``findings`` (matching the
    ``{index: verification}`` map Pass-2 produced). The shared decision core every
    gate calls — the too_big/shed routing is the caller's (it differs by
    index-domain).

    ``impact_fn`` (story fishable-apivorous-redhead) is threaded verbatim to
    :func:`pass3_decide` — the plan-review wrapper passes ``impact_plan``; a caller that
    omits it gets the mean :func:`impact` unchanged. ``execution_review`` is likewise threaded
    verbatim (default False) — only a plan-review execution re-review sets it, enabling the
    on-target veto; every other caller is byte-unchanged."""
    decided: list[dict[str, Any]] = []
    for i, f in enumerate(findings):
        block_threshold, blocking_enabled = threshold_for(f.get("criteria", []))
        d = pass3_decide(
            verifs.get(i),
            block_threshold=block_threshold,
            blocking_enabled=blocking_enabled,
            impact_fn=impact_fn,
            execution_review=execution_review,
        )
        decided.append({**f, **d, "verification": verifs.get(i), "tier": "LLM"})
    return decided
