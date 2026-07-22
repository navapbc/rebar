"""Pass-2 structured-output MODELS + registered contracts (extracted from ``verify.py``).

The binary sub-question vocabulary, the severity-attribute enums, and the per-gate
``Verification``/``Novelty`` structured-output models that every review surface's Pass-2
emits. Extracted from :mod:`rebar.llm.review_kernel.verify` (which keeps the verify
ORCHESTRATION) purely to stay under the module-size cap; the public names are re-exported
from ``verify`` so ``from ...review_kernel.verify import verification_model`` still resolves.

Depends only on :data:`rebar.llm.review_kernel.decide.GRADED_BINARY` (+ ``NOVELTY_SUBANSWERS``,
imported lazily) and the contract registry, so there is no import cycle back to ``verify``.
"""

from __future__ import annotations

from typing import Any, Literal

from rebar.llm import contracts

from .decide import GRADED_BINARY

# The binary sub-question descriptions + na-defaults, at MODULE scope so the base and
# plan-review models build the SAME Binary vocabulary from ONE source (no drift). Kept out
# of the functions (which lazily import pydantic) because these are plain data.
_BINARY_DESC = {
    "is_verifiable": "yes|no|insufficient|na — finding stated concretely enough to test.",
    "evidence_entails_finding": (
        "yes|no|insufficient|na — the cited evidence actually ENTAILS the finding under a "
        "charitable reading (load-bearing; do not answer na)."
    ),
    "path_reachable": (
        "yes|no|insufficient|na — the situation is reachable as written (na if the finding is "
        "structural/organisational with no execution path)."
    ),
    "impact_follows_necessarily": "yes|no|insufficient|na — asserted harm NECESSARILY follows.",
    "no_viable_alternative_explanation": (
        "yes|no|insufficient|na — no reasonable benign reading dissolves the finding."
    ),
    "no_existing_mitigation": "yes|no|insufficient|na — nothing already mitigates the flaw.",
    "severity_claim_justified": (
        "yes|no|insufficient|na — the finding's asserted impact is proportionate, not inflated."
    ),
    # DSO-adopted (epic cite-stone-sea / WS1, ADR 0032). Both na-default (see below).
    "committed_work_relies_on_unbacked_claim": (
        "yes|no|insufficient|na — a COMMITTED element of the plan (an AC, a task, an "
        "edit, or a scope EXCLUSION such as 'OUT: X — already exists / handled by Y') "
        "rests on a factual claim the plan neither verifies (a run Verify command / "
        "cited evidence) nor guards with a fallback. 'yes' upholds a confident-assertion "
        "or false-exclusion finding; `na` unless the finding is about a committed element "
        "depending on such a claim."
    ),
    "respects_artifact_altitude": (
        "yes|no|insufficient|na — the finding does NOT demand a detail, or presume a "
        "design choice, that this artifact at its level (epic/story/task) legitimately "
        "defers to a child or to implementation. 'no' marks an altitude-error false "
        "positive (it then LOWERS validity like any other sub-answer); `na` if altitude "
        "is not in question."
    ),
    # R5 (story empty-microbial-antlion). Code-review counterpart to R1's plan-review probe.
    # A VALIDITY sub-answer (same yes=finding-holds polarity as the rest of GRADED_BINARY): it
    # verifies a finding that asserts a plan/ticket-claimed capability is NOT delivered by the
    # code, and DROPS the finding when the capability is in fact present (a false non-delivery
    # claim). This is deliberately the validity axis, NOT the plan-review impact-promotion sketch
    # in docs/research/task-decomposition-sota-2026.md — at code-review time the capability's
    # presence is DIRECTLY checkable against the diff, so the honest move is to confirm/refute the
    # finding (weeding false positives), not to inflate its priority on a forward claim.
    "asserted_capability_confirmed": (
        "yes|no|insufficient|na — for a finding asserting that a capability the plan/ticket "
        "claimed is NOT delivered by the code under review: 'yes' = the review CONFIRMS the "
        "asserted gap — the capability really is absent/unwired/dead, so the finding HOLDS (like "
        "any yes, it keeps validity up); 'no' = the capability is in fact present and exercised, "
        "REFUTING the finding (lowers validity → a false non-delivery claim is dropped); "
        "'insufficient' if the diff does not decide it. Answer `na` — the default — for every "
        "finding OUTSIDE the G6/E4/T3 asserted-capability cohort, so it abstains from validity "
        "and never destabilises an existing review."
    ),
}
# Sub-answers that default to "na" (abstain, EXCLUDED from validity) rather than
# "insufficient": they apply only to a specific finding SHAPE, so a verifier that does not
# engage them must not drag validity, and old sidecars predating them stay comparable
# (absent key == na, both excluded by decide.validity). Data-driven default over the SAME
# uniform loop — no per-criterion branching in the pass. See ADR 0032 (epic cite-stone-sea).
_BINARY_NA_DEFAULT = frozenset(
    {
        "committed_work_relies_on_unbacked_claim",
        "respects_artifact_altitude",
        "asserted_capability_confirmed",  # R5 — na-default (abstains outside the G6/E4/T3 cohort)
    }
)


def _build_binary(forbid: Any, *, extra_fields: dict[str, tuple[type, Any]] | None = None) -> type:
    """Build the shared ``Binary`` sub-answer model — the 7 GRADED sub-questions (whose
    graded fraction is the finding's validity) PLUS the ``cited_reference_accurate`` veto —
    from the module-level vocabulary, so the base and plan-review models never diverge."""
    from pydantic import Field, create_model

    # Conditional VETO binaries (na-default, NOT graded): cited-reference + the a8e5 absence pair.
    binary_fields: dict[str, Any] = {
        q: (str, Field(default="na", description="yes|no|insufficient|na"))
        for q in ("cited_reference_accurate", "claims_absence", "absence_confirmed_in_context")
    }
    for q in GRADED_BINARY:
        _default = "na" if q in _BINARY_NA_DEFAULT else "insufficient"
        binary_fields[q] = (str, Field(default=_default, description=_BINARY_DESC.get(q, "")))
    binary_fields.update(extra_fields or {})
    return create_model("Binary", __config__=forbid, **binary_fields)


def _build_verification_output(
    forbid: Any,
    severity_cls: type,
    *,
    binary_extra_fields: dict[str, tuple[type, Any]] | None = None,
) -> type:
    """Wrap a severity-attributes class + the shared ``Binary`` into the per-finding
    ``Verification`` and its ``VerificationOutput`` wrapper. Built via ``create_model`` (real
    type objects, NOT string annotations) so the parametric ``severity_cls`` resolves cleanly
    under this module's ``from __future__ import annotations``."""
    from pydantic import Field, create_model

    Binary = _build_binary(forbid, extra_fields=binary_extra_fields)
    Verification = create_model(
        "Verification",
        __config__=forbid,
        index=(int, Field(description="The 0-based index of the finding being verified.")),
        analysis=(
            str,
            Field(
                default="",
                description="REASON FIRST: brief reasoning through this finding's sub-questions "
                "(as an unproven claim) before the attributes/answers.",
            ),
        ),
        severity_attributes=(severity_cls, Field(default_factory=severity_cls)),
        binary=(Binary, Field(default_factory=Binary)),
    )
    verifications_type = list[Verification]  # type: ignore[valid-type]  # runtime-built model
    return create_model(
        "VerificationOutput",
        __config__=forbid,
        verifications=(verifications_type, Field(default_factory=list)),
    )


# ── the registered `verification` CONTRACT (the SINGLE source of the binary vocabulary +
#    severity-attribute enums; small/flat for tolerant validation) ────────────────────────
def verification_model(*, strict: bool = False) -> type:
    """The Pass-2 ``verification`` structured-output model: one ``Verification`` per finding
    (by ``index``) carrying the coarse severity ATTRIBUTES + the typed BINARY sub-answers.

    The ``Binary`` fields are the 7 GRADED sub-questions (whose graded fraction is the
    finding's *validity*, computed in :mod:`.decide`) PLUS ``cited_reference_accurate`` — the
    conditional VETO (``"na"`` when the finding cites no code reference; ``"no"`` drops the
    finding). Kept small/flat so the tolerant validation stack (json-repair + bounded retry +
    pydantic-ai native outputs) rarely needs to degrade.

    ``strict`` switches the whole model tree to ``extra="forbid"`` — the REJECT-don't-ignore
    boundary. Under the default (``strict=False``, the LIVE registered contract) Pydantic's
    ``extra="ignore"`` silently drops unknown keys, so a divergent verifier shape (a ``findings``
    wrapper instead of ``verifications``, or per-item ``attributes`` instead of
    ``severity_attributes``) validates to an EMPTY object — the exact class of silent degrade
    this contract guards against. ``strict=True`` makes that shape FAIL validation loudly; it is
    pinned by tests and is the flip-ready future default (expand-contract: see
    docs/adr/0006-llm-stage-seam-contracts.md). Flipping the live contract is a one-liner —
    change the registration below to ``verification_model(strict=True)``."""
    from pydantic import BaseModel, ConfigDict, Field

    forbid = ConfigDict(extra="forbid") if strict else ConfigDict()

    class SeverityAttrs(BaseModel):
        model_config = forbid
        prod_impact: str = Field(default="none", description="none|low|medium|high")
        debt_impact: str = Field(default="none", description="none|low|medium|high")
        blast_radius: str = Field(
            default="local",
            description=(
                "local|module|system — ONE-WAY ratchet: a wide radius only LOWERS tolerance for "
                "an already-real defect; it never raises a trivial finding's severity."
            ),
        )
        likelihood: str = Field(default="low", description="low|medium|high")
        reversibility: str = Field(default="easy", description="easy|moderate|hard")

    # Binary vocabulary + the Verification/VerificationOutput wrapper come from the shared
    # module-level builders, so the plan-review model reuses this EXACT shape (no drift).
    return _build_verification_output(forbid, SeverityAttrs)


def plan_review_verification_model(*, strict: bool = False) -> type:
    """The PLAN-REVIEW Pass-2 model (story fishable-apivorous-redhead): the same ``Verification``
    shape as :func:`verification_model`, but its ``severity_attributes`` is a ``PlanSeverityAttrs``
    that ADDS 7 plan-severity axes + a detection axis on top of the base five. Registered as the
    plan-review-specific ``plan_review_verification`` contract; the kernel ``verification`` (used
    by code-review + the kernel default) is UNCHANGED. Ordinal axes grade
    ``none|low|medium|high``; ``ac_unverifiable`` grades by ORACLE KIND (its Literal, plan-v3).
    Every axis defaults to ``"none"`` (detection to ``""``), so an older/absent verifier ABSTAINS
    (a missing axis maps to 0 in :func:`rebar.llm.review_kernel.decide.impact_plan`); see
    ``impact_plan`` for the MAX/floor/amplifier compose."""
    from pydantic import BaseModel, ConfigDict, Field

    forbid = ConfigDict(extra="forbid") if strict else ConfigDict()

    class PlanSeverityAttrs(BaseModel):
        model_config = forbid
        # Base severity attributes (kept for sidecar continuity; impact_plan reads the axes below).
        prod_impact: str = Field(default="none", description="none|low|medium|high")
        debt_impact: str = Field(default="none", description="none|low|medium|high")
        blast_radius: str = Field(
            default="local",
            description=(
                "local|module|system — ONE-WAY ratchet: a wide radius only LOWERS tolerance for "
                "an already-real defect; it never raises a trivial finding's severity."
            ),
        )
        likelihood: str = Field(default="low", description="low|medium|high")
        reversibility: str = Field(default="easy", description="easy|moderate|hard")
        # ── The 7 plan-severity axes (decide.impact_plan aggregates these by MAX). ──
        ac_unverifiable: Literal[
            "none", "underspecified_oracle", "broken_oracle", "missing_oracle"
        ] = Field(
            default="none",
            description="Oracle-kind grade (closed set — NOT the ordinal ladder): missing_oracle"
            " = no verification method exists as phrased; broken_oracle = a stated proving"
            " command/symbol/count is factually wrong so the stated verification CANNOT pass;"
            " underspecified_oracle = a check is constructible but the exact command/file/value"
            " is not spelled out. HARD-OVERRIDE for missing/broken ONLY (floors impact to 0.85);"
            " underspecified scores below every blocking threshold and never floors.",
        )
        dod_uncertifiable: str = Field(
            default="none",
            description="none|low|medium|high — a definition-of-done / success criterion cannot be "
            "certified true. HARD-OVERRIDE axis; also forces the detection multiplier to x1.0.",
        )
        undecomposed: str = Field(
            default="none",
            description="none|low|medium|high — work is left undecomposed (a flat plan that should "
            "be broken down). Grade only a GENUINE gap: the deterministic G5 signal already "
            "suppresses false 'flat' findings on tickets that have children. HARD-OVERRIDE axis.",
        )
        divergent_implementation: str = Field(
            default="none",
            description="none|low|medium|high — the plan diverges from the implementation/reality "
            "it claims to describe (builds the wrong thing). HARD-OVERRIDE axis.",
        )
        internal_conflict: str = Field(
            default="none",
            description="none|low|medium|high — the plan internally contradicts itself.",
        )
        vague_directive: str = Field(
            default="none",
            description="none|low|medium|high — a load-bearing directive is too vague to act on "
            "unambiguously.",
        )
        irreversible_without_rationale: str = Field(
            default="none",
            description="none|low|medium|high — an irreversible/destructive step is taken without "
            "a stated rationale or fallback.",
        )
        # ── Detection axis (drives decide.impact_plan's detection amplifier). ──
        silent_vs_self_revealing: str = Field(
            default="",
            description="silent|self_revealing — 'silent' = the plan builds the wrong thing "
            "undetectably (amplifier x1.0); 'self_revealing' = the mistake hits an obvious wall "
            "and is caught (x0.8). Leave empty when not applicable (treated as x1.0).",
        )

    return _build_verification_output(
        forbid,
        PlanSeverityAttrs,
        binary_extra_fields={
            "prerequisite_attribution_valid": (
                str,
                Field(default="na", description="yes|no|na"),
            )
        },
    )


def code_review_verification_model(*, strict: bool = False) -> type:
    """The CODE-REVIEW Pass-2 model (story albite-lazy-barb): the same kernel ``Binary`` vocabulary
    and ``VerificationOutput`` wrapper, but ``severity_attributes`` is a ``CodeSeverityAttrs`` that
    EXTENDS the base five with the code-review consequence binaries + detection judgment that
    :func:`rebar.llm.review_kernel.decide.impact_code` reads (the base five are kept for sidecar
    continuity). Each binary/bool defaults ``False`` so an older/absent verifier ABSTAINS (a missing
    binary contributes 0 to its lane — never inflates). ``trigger_likelihood`` is DELIBERATELY named
    apart from the base ``likelihood`` (mapped by ``_LIKE01``) to avoid a field-override collision;
    absent ⇒ ``common`` (1.0) so a serious correctness finding is not silently dampened."""
    from pydantic import BaseModel, ConfigDict, Field

    forbid = ConfigDict(extra="forbid") if strict else ConfigDict()

    class CodeSeverityAttrs(BaseModel):
        model_config = forbid
        # Base severity attributes (kept for sidecar continuity; impact_code reads binaries below).
        prod_impact: str = Field(default="none", description="none|low|medium|high")
        debt_impact: str = Field(default="none", description="none|low|medium|high")
        blast_radius: str = Field(default="local", description="local|module|system")
        likelihood: str = Field(default="low", description="low|medium|high")
        reversibility: str = Field(default="easy", description="easy|moderate|hard")
        # ── PRODUCTION-lane consequence binaries (decide.impact_code MAXes tier values). ──
        data_loss_without_recovery: bool = Field(
            default=False,
            description="TRUE if data can be lost/corrupted with no recovery (serious).",
        )
        security_bypass_not_enforced_elsewhere: bool = Field(
            default=False,
            description="TRUE if a security check is bypassed and not enforced (serious).",
        )
        silent_wrong_feeding_a_decision: bool = Field(
            default=False,
            description="TRUE if a silently-wrong value feeds a real decision (serious).",
        )
        capability_degraded: bool = Field(
            default=False,
            description="TRUE if a user-facing capability is degraded/partially broken (moderate).",
        )
        # ── MAINTAINABILITY-lane consequence binaries. ──
        unversioned_published_contract_break: bool = Field(
            default=False,
            description="TRUE if a PUBLISHED contract breaks with no version bump (serious).",
        )
        safety_net_removal_without_replacement: bool = Field(
            default=False,
            description="TRUE if a test/guard/assert is removed with no replacement (serious).",
        )
        contract_drift: bool = Field(
            default=False,
            description="TRUE if an interface drifts from its documented contract (moderate).",
        )
        hidden_invariant: bool = Field(
            default=False,
            description="TRUE if the change relies on/breaks an undocumented invariant (moderate).",
        )
        reachable_path_without_automated_coverage: bool = Field(
            default=False,
            description="TRUE if a reachable path lacks automated coverage (moderate).",
        )
        implicit_coupling: bool = Field(
            default=False,
            description="TRUE if the change adds implicit cross-module coupling (minor).",
        )
        dead_code: bool = Field(
            default=False,
            description="TRUE if the change introduces dead/unreachable code (minor).",
        )
        # ── Per-lane likelihood + detection (drive impact_code multipliers/amplifier). ──
        trigger_likelihood: str = Field(
            default="common",
            description="common|sometimes|rare — how often the PRODUCTION consequence triggers "
            "(prod-lane multiplier common=1.0/sometimes=0.6/rare=0.25; distinct from base "
            "likelihood; absent => common).",
        )
        silent_failure: bool = Field(
            default=False,
            description="TRUE if the defect fails SILENTLY (no error surfaces) — detection x1.0.",
        )
        escapes_automation: bool = Field(
            default=False,
            description="TRUE if the defect escapes existing tests/CI/lint — detection x1.0.",
        )

    # Binary vocabulary + the Verification/VerificationOutput wrapper come from the shared
    # module-level builders; code-review's model reuses the EXACT Binary shape (only
    # CodeSeverityAttrs differs), so the graded binary set never diverges across gates.
    return _build_verification_output(forbid, CodeSeverityAttrs)


def register_verification_contract() -> None:
    """Register the kernel ``verification`` contract (idempotent) — the single source of the
    binary vocabulary + severity enums every review gate's Pass-2 emits."""
    contracts.register_contract("verification", verification_model)


register_verification_contract()


# ── the registered `novelty` CONTRACT — the SEPARATE Pass-2 sub-call that ALONE receives the
#    prior findings (child 150b). Kept distinct from `verification` so the severity/validity
#    sub-call structurally never sees the prior findings (the independence invariant, enforced by
#    construction, not by prompt assertion). The matches-prior fields are anchored to the
#    `decide.NOVELTY_SUBANSWERS` vocabulary so the contract and `decide.novelty()` can never name
#    different questions. ───────────────────────────────────────────────────────────────────────
def novelty_model(*, strict: bool = False) -> type:
    """The Pass-2 ``novelty`` structured-output model: one ``Novelty`` per finding (by ``index``)
    carrying the factual matches-prior sub-answers + the matched prior-finding id.

    The sub-answers (``decide.NOVELTY_SUBANSWERS``) are graded yes/insufficient/no questions;
    ``decide.novelty()`` maps them to a [0,1] novelty (``1 − mean``). Each defaults to
    ``"insufficient"`` (mirroring the verification ``Binary`` defaults — a neutral 0.5, so a
    partially-filled object never spuriously reads as fully novel/droppable). ``matched_prior_id``
    defaults to ``""`` (no prior match). ``strict`` flips the tree to ``extra="forbid"`` (the
    reject-don't-ignore boundary), mirroring :func:`verification_model`."""
    from pydantic import BaseModel, ConfigDict, Field, create_model

    from .decide import NOVELTY_SUBANSWERS

    forbid = ConfigDict(extra="forbid") if strict else ConfigDict()

    # The sub-answer field set is DERIVED from NOVELTY_SUBANSWERS (the single vocabulary in
    # .decide) so the contract and the novelty math can never name different sub-questions.
    matches_fields: dict[str, Any] = {
        q: (str, Field(default="insufficient", description="yes|insufficient|no"))
        for q in NOVELTY_SUBANSWERS
    }
    MatchesPrior = create_model("MatchesPrior", __config__=forbid, **matches_fields)

    class Novelty(BaseModel):
        model_config = forbid
        index: int = Field(description="The 0-based index of the finding being scored.")
        matched_prior_id: str = Field(
            default="", description="The prior finding id this matches, or empty if none."
        )
        matches_prior: MatchesPrior = Field(default_factory=MatchesPrior)  # type: ignore[valid-type]

    class NoveltyOutput(BaseModel):
        model_config = forbid
        novelties: list[Novelty] = Field(default_factory=list)

    return NoveltyOutput


def register_novelty_contract() -> None:
    """Register the kernel ``novelty`` contract (idempotent) — the SEPARATE Pass-2 sub-call's
    matches-prior vocabulary, distinct from ``verification`` so the prior findings reach ONLY
    this sub-call (the structural independence invariant)."""
    contracts.register_contract("novelty", novelty_model)


register_novelty_contract()
