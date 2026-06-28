"""Pass-2 of the four-pass review framework: the finding-VERIFIER (epic ``vivid-gang-day`` WS2).

Pass-2 validates each finding independently. A SEPARATE verifier re-grounds the finding
and emits coarse severity ATTRIBUTES + a typed BINARY sub-answer set
(``{yes | no | insufficient}``). The binary sub-questions and the severity-attribute enums
are properties of *a finding*, not of plans-vs-diffs — a code-review finding and a plan
finding answer the SAME questions — so this is the strongest reuse candidate. Extracting it
gives ONE binary vocabulary + ONE ``verification`` output contract that every review surface
shares, eliminating the silent-drift class.

What this module owns (domain-AGNOSTIC):

* the single registered ``verification`` CONTRACT — the binary sub-question vocabulary
  (the 7 graded questions from :data:`rebar.llm.review_kernel.decide.GRADED_BINARY` + the
  conditional ``cited_reference_accurate`` veto) and the severity-attribute enums;
* the verify ORCHESTRATION: the per-finding listing format, the token-budget chunking
  (preserving GLOBAL indices), the merge-by-global-index, and the non-frontier
  verifier-model default;
* :func:`verify_findings` — the Pass-2 entry: chunk → run each chunk (via an injected
  ``run_chunk`` LLM seam) → merge by global index → DEGRADE to "no verification" (which
  Pass-3's ``pass3_decide(None)`` routes to INDETERMINATE) for any finding the verifier
  could not produce, NEVER crashing the gate.

What stays per-gate (NOT here): the verify-prompt PREAMBLE (the prompt file — the workflow
shell the v3 engine provides), the domain-context ASSEMBLER (plan text vs diff), and the
token estimator / model window (injected, since the tokenizer is infra, not a review concern).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from rebar.llm import contracts
from rebar.llm.errors import StructuredOutputError

from .decide import GRADED_BINARY

logger = logging.getLogger(__name__)

# ── token-budget chunking constants (the principled replacement for a magic count-batch:
#    split the verify request by TOKEN budget vs the model window, not an arbitrary count) ──
DEFAULT_VERIFY_WINDOW_HEADROOM = 0.8  # config-overridable: verify.verify_window_headroom
# Per-finding OUTPUT reserve: the verify response carries one verification object per finding
# (severity_attributes + the binary sub-answers), so output scales with the finding count and
# must be reserved. A documented, adjustable constant (NOT a derived value).
PER_FINDING_VERIFY_TOKENS = 256
# A documented approximation of the verifier SYSTEM prompt's size, reserved on top of the
# rendered per-finding instructions (the system prompt is ~constant, so it is a flat reserve
# rather than re-estimated per chunk).
VERIFY_SYSTEM_RESERVE_TOKENS = 2_000


# ── the canonical verifier-rules SCAFFOLD (the soft prompt rules, for discoverability) ──────
# The four soft rules a Pass-2 verifier's prompt PREAMBLE should embed, recorded ONCE here as
# the single discoverable source (epic vivid-gang-day WS4). These are NOT enforced by a
# prompt-text lint (an anti-pattern — mature stacks enforce typed contracts + behavior, not
# prompt-string greps); they are enforced BEHAVIORALLY by evals (deterministic FakeRunner
# assertions on the gate path + a small gated live eval). A gate author embeds the scaffold
# TEXT in their verify prompt; the plan-review verifier prompts are the worked reference (they
# carry these exact rules). See docs/review-kernel.md.
VERIFIER_RULES: tuple[tuple[str, str], ...] = (
    (
        "independence",
        "Treat each finding as an unproven CLAIM TO TEST — its conclusion is NOT asserted; "
        "do not assume it is correct. (Never show the verifier the finding's own decision.)",
    ),
    (
        "atomicity",
        "Be atomic: answer each binary sub-question on its own merits, independently.",
    ),
    (
        "allow-insufficient",
        "'insufficient' is an allowed and honest answer when the evidence does not decide it.",
    ),
    (
        "verdict-with-citation-not-fix",
        "Verdict-with-citation, never verdict-with-fix — judge the claim; do not author a fix.",
    ),
)

VERIFIER_RULES_SCAFFOLD = "\n".join(f"- {name}: {text}" for name, text in VERIFIER_RULES)


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
    from pydantic import BaseModel, ConfigDict, Field, create_model

    forbid = ConfigDict(extra="forbid") if strict else ConfigDict()

    class SeverityAttrs(BaseModel):
        model_config = forbid
        prod_impact: str = Field(default="none", description="none|low|medium|high")
        debt_impact: str = Field(default="none", description="none|low|medium|high")
        blast_radius: str = Field(default="local", description="local|module|system")
        likelihood: str = Field(default="low", description="low|medium|high")
        reversibility: str = Field(default="easy", description="easy|moderate|hard")

    # The binary field set is DERIVED from GRADED_BINARY (the single vocabulary in .decide) so
    # the contract and the validity math can never name different sub-questions.
    binary_fields: dict[str, Any] = {
        "cited_reference_accurate": (str, Field(default="na", description="yes|no|insufficient|na"))
    }
    for q in GRADED_BINARY:
        binary_fields[q] = (str, Field(default="insufficient"))
    Binary = create_model("Binary", __config__=forbid, **binary_fields)

    class Verification(BaseModel):
        model_config = forbid
        index: int = Field(description="The 0-based index of the finding being verified.")
        severity_attributes: SeverityAttrs = Field(default_factory=SeverityAttrs)
        binary: Binary = Field(default_factory=Binary)  # type: ignore[valid-type]

    class VerificationOutput(BaseModel):
        model_config = forbid
        verifications: list[Verification] = Field(default_factory=list)

    return VerificationOutput


def register_verification_contract() -> None:
    """Register the kernel ``verification`` contract (idempotent) — the single source of the
    binary vocabulary + severity enums every review gate's Pass-2 emits."""
    contracts.register_contract("verification", verification_model)


register_verification_contract()


# ── the per-finding listing (the ONE canonical format every gate's verifier consumes) ──────
def finding_listing(batch: list[tuple[int, dict[str, Any]]]) -> str:
    """The Pass-2 per-finding listing for a batch of ``(global_index, finding)`` pairs
    (``### finding index {i}`` blocks with claim / criteria / evidence / impact)."""
    return "\n\n".join(
        f"### finding index {i}\nclaim: {f['finding']}\ncriteria: {', '.join(f['criteria'])}\n"
        f"evidence: {' | '.join(f.get('evidence', []))}\nimpact: {f.get('impact', '')}"
        for i, f in batch
    )


def verify_instructions(batch: list[tuple[int, dict[str, Any]]]) -> str:
    """The full Pass-2 verifier INSTRUCTIONS (header + :func:`finding_listing`) over one
    batch of ``(global_index, finding)`` pairs. An empty batch yields a benign header only."""
    if not batch:
        return "Verify each finding below by its index. Emit one verification per finding."
    return (
        "Verify each finding below by its index. Emit one verification per finding "
        f"(indices {batch[0][0]}–{batch[-1][0]}).\n\n{finding_listing(batch)}"
    )


# ── token-budget chunking (preserves GLOBAL indices so per-chunk outputs re-merge) ─────────
TokenEstimator = Callable[[str], int]


def verify_request_chunks(
    findings: list[dict[str, Any]],
    *,
    window_tokens: int,
    est_tokens: TokenEstimator,
    headroom: float = DEFAULT_VERIFY_WINDOW_HEADROOM,
    per_finding_out_tokens: int = PER_FINDING_VERIFY_TOKENS,
    system_reserve_tokens: int = VERIFY_SYSTEM_RESERVE_TOKENS,
) -> tuple[list[list[tuple[int, dict[str, Any]]]], list[int]]:
    """Split ``findings`` into token-budgeted Pass-2 verify chunks, preserving GLOBAL
    indices so the per-chunk verifications re-merge by ``index``.

    Returns ``(chunks, omitted_indices)`` where each chunk is a list of
    ``(global_index, finding)`` pairs. The fit test for a chunk ``C`` is::

        est_tokens(verify_instructions(C)) + system_reserve_tokens
            + len(C) * per_finding_out_tokens  <=  floor(window_tokens * headroom)

    The token estimator + the model window are INJECTED (the tokenizer is infra, not a review
    concern — a consuming gate passes its own). The common case returns ONE chunk == the whole
    enumerated list (no behavior change). A single finding whose own request still exceeds the
    budget at the largest reachable model is OMITTED from every chunk (its index is returned in
    ``omitted_indices``) so it is left UNVERIFIED — ``pass3_decide(None)`` then routes it to
    INDETERMINATE (non-blocking, surfaced) rather than silently dropping it."""
    budget = int(window_tokens * headroom)

    def request_tokens(chunk: list[tuple[int, dict[str, Any]]]) -> int:
        return (
            est_tokens(verify_instructions(chunk))
            + system_reserve_tokens
            + len(chunk) * per_finding_out_tokens
        )

    chunks: list[list[tuple[int, dict[str, Any]]]] = []
    omitted: list[int] = []
    cur: list[tuple[int, dict[str, Any]]] = []
    for item in list(enumerate(findings)):
        if request_tokens([item]) > budget:
            omitted.append(item[0])  # too big even alone → unverifiable at any model
            continue
        if cur and request_tokens(cur + [item]) > budget:
            chunks.append(cur)
            cur = []
        cur.append(item)
    if cur:
        chunks.append(cur)
    return chunks, omitted


# ── the SHARED, structural reshape seam (the SINGLE place a flat verifier output list becomes
#    the {index: verification} map Pass-3 consumes) — classifies the contract violations the old
#    silent-drop hid, instead of dropping them invisibly. Both the kernel `verify_findings` and
#    plan-review's `plan_review_decide` route through this, so the verifier→decide keying contract
#    lives in ONE place (epic drag-gripe-brake). ──────────────────────────────────────────────
@dataclass(frozen=True)
class VerificationReshape:
    """The result of :func:`reshape_verifications`: the tolerant ``{index: verification}`` map
    Pass-3 consumes (BYTE-IDENTICAL to the old silent-drop), PLUS the structurally-detected
    contract violations the old code dropped invisibly.

    ``malformed`` — items with no usable integer ``index`` (a divergent per-item shape).
    ``duplicates`` — indices that appeared more than once (ambiguous; last-wins in the map, as
    before). ``unexpected`` — integer indices outside the expected set (an invented/out-of-range
    index; left out of the map, as before). These are a DISTINCT signal from a finding that
    simply has no verification (an honest "couldn't verify" → ``no-verification`` →
    INDETERMINATE)."""

    verifications: dict[int, dict[str, Any]]
    malformed: int = 0
    duplicates: tuple[int, ...] = ()
    unexpected: tuple[int, ...] = ()

    @property
    def has_violations(self) -> bool:
        return bool(self.malformed or self.duplicates or self.unexpected)

    def summary(self) -> dict[str, Any]:
        """The non-zero violation counts/indices, for an ERROR log + a verdict-coverage count.
        Empty (falsy) when the reshape conformed — so a clean run surfaces NOTHING."""
        out: dict[str, Any] = {}
        if self.malformed:
            out["malformed"] = self.malformed
        if self.duplicates:
            out["duplicates"] = list(self.duplicates)
        if self.unexpected:
            out["unexpected"] = list(self.unexpected)
        return out


def reshape_verifications(
    raw: list[dict[str, Any]] | None, *, valid_indices: Collection[int] | None = None
) -> VerificationReshape:
    """Reshape a FLAT verifier output list into the ``{index: {severity_attributes, binary}}``
    map Pass-3 consumes, classifying the contract violations the old silent-drop hid.

    The returned ``verifications`` map is byte-identical to the prior tolerant behavior (non-int
    ``index`` dropped; later wins on a duplicate; out-of-range entries never read downstream), so
    routing a consumer through this changes NO outcome — it only ADDS the violation report. When
    ``valid_indices`` is given (the chunker's GLOBAL indices, or ``range(len(findings))``), an
    integer index outside it is recorded as ``unexpected`` (and excluded from the map)."""
    merged: dict[int, dict[str, Any]] = {}
    seen: set[int] = set()
    malformed = 0
    duplicates: list[int] = []
    unexpected: list[int] = []
    for v in raw or []:
        idx = v.get("index") if isinstance(v, dict) else None
        if not isinstance(idx, int):
            malformed += 1
            continue
        if valid_indices is not None and idx not in valid_indices:
            unexpected.append(idx)
            continue
        if idx in seen:
            duplicates.append(idx)
        seen.add(idx)
        merged[idx] = {
            "severity_attributes": v.get("severity_attributes", {}) or {},
            "binary": v.get("binary", {}) or {},
        }
    return VerificationReshape(merged, malformed, tuple(duplicates), tuple(unexpected))


def merge_verifications_by_index(
    chunk_outputs: list[list[dict[str, Any]]],
) -> dict[int, dict[str, Any]]:
    """Merge the per-chunk verification lists into the ``{global_index: {severity_attributes,
    binary}}`` map Pass-3 consumes. Each verification carries its GLOBAL ``index`` (the chunker
    preserved it); a later chunk wins on a duplicate index (chunks are disjoint, so this is a
    no-op in practice). Verifications without a usable integer ``index`` are dropped (the
    finding then has no verification → INDETERMINATE downstream). Thin wrapper over
    :func:`reshape_verifications` (the single reshape seam) — drops the violation report; callers
    that want the report (loud surfacing) call ``reshape_verifications`` directly."""
    return reshape_verifications([v for out in chunk_outputs for v in (out or [])]).verifications


# ── the non-frontier verifier-model default ────────────────────────────────────────────────
def resolve_verifier_model(model: str | None, *, default_model: str, verifier_default: str) -> str:
    """The verifier-model DEFAULT rule (shared across gates): a Pass-2 verifier runs under the
    decisive NON-FRONTIER ``verifier_default`` model UNLESS the operator EXPLICITLY chose a
    model (``model != default_model`` — any non-default value is an explicit choice and wins).
    Returns the model id the verify call should use. Pure — the cfg/env plumbing is the
    consumer's (e.g. plan-review's ``_verifier_cfg``)."""
    return verifier_default if model == default_model else (model or verifier_default)


# ── the Pass-2 entry: chunk → run → merge → degrade ────────────────────────────────────────
# The injected per-chunk LLM seam: given the verifier INSTRUCTIONS for one chunk and the domain
# CONTEXT (plan text / diff), return that chunk's `verifications` list (each item a dict with a
# global `index` + severity_attributes + binary). The workflow shell (the v3 engine's prompt
# step) is one such seam; a FakeRunner-backed callable is the offline seam; b744 supplies its
# own. A run that fails/returns nothing degrades that chunk (its findings → INDETERMINATE).
RunChunk = Callable[[str, str], list[dict[str, Any]]]


def verify_findings(
    findings: list[dict[str, Any]],
    *,
    context: str,
    run_chunk: RunChunk,
    window_tokens: int,
    est_tokens: TokenEstimator,
    headroom: float = DEFAULT_VERIFY_WINDOW_HEADROOM,
) -> dict[str, Any]:
    """Pass-2 over ``findings``: token-budget chunk → run each chunk via ``run_chunk`` (the
    injected LLM seam) → reshape by GLOBAL index. Returns ``{"verifications": {index:
    {severity_attributes, binary}}, "omitted": [index, ...], "contract_violations": {...}}``.

    DEGRADE, never crash: a chunk whose ``run_chunk`` raises contributes no verifications, so
    those findings have no verification and ``pass3_decide(None)`` routes them to INDETERMINATE.
    Findings too big to verify even alone are ``omitted`` (also → INDETERMINATE). The whole-batch
    verifier vocabulary + the reshape are shared; only ``run_chunk`` + the tokenizer are the
    consumer's.

    LOUD on a contract break (epic drag-gripe-brake): a ``StructuredOutputError`` from
    ``run_chunk`` (the verifier's turn could not be validated to the ``verification`` contract
    even after json-repair + the bounded retry — a divergent SHAPE) is logged at ERROR and
    recorded in ``contract_violations`` (distinct from a benign degrade), as are malformed /
    duplicate / out-of-range indices the :func:`reshape_verifications` seam detects. The OUTCOME
    is unchanged (those findings still degrade to INDETERMINATE); the report is purely additive
    observability. ``contract_violations`` is empty/falsy on a clean run."""
    chunks, omitted = verify_request_chunks(
        findings, window_tokens=window_tokens, est_tokens=est_tokens, headroom=headroom
    )
    sent_indices = {gi for chunk in chunks for gi, _ in chunk}
    chunk_outputs: list[list[dict[str, Any]]] = []
    shape_failures: list[int] = []
    for chunk in chunks:
        try:
            chunk_outputs.append(list(run_chunk(verify_instructions(chunk), context) or []))
        except StructuredOutputError:
            # The verifier's turn could not be validated to the `verification` contract — a SHAPE
            # contract failure, NOT a benign "couldn't verify". Surface it LOUDLY (distinct from
            # the quiet degrade below); the chunk's findings still degrade to INDETERMINATE.
            chunk_indices = [gi for gi, _ in chunk]
            logger.error(
                "verify chunk failed the structured `verification` contract; findings %s "
                "degrade to INDETERMINATE",
                chunk_indices,
            )
            shape_failures.extend(chunk_indices)
            chunk_outputs.append([])
        except Exception:  # noqa: BLE001 — a non-contract failure (network/etc.) → honest degrade
            chunk_outputs.append([])
    reshape = reshape_verifications(
        [v for out in chunk_outputs for v in out], valid_indices=sent_indices
    )
    violations = reshape.summary()
    if shape_failures:
        violations["shape_failures"] = sorted(shape_failures)
    if violations:
        logger.error("verification contract violations detected: %s", violations)
    return {
        "verifications": reshape.verifications,
        "omitted": omitted,
        "contract_violations": violations,
    }
