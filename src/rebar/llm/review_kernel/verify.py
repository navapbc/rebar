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

from rebar.llm.errors import StructuredOutputError

from .verify_models import (  # re-exported: models live in verify_models (module-size split)
    code_review_verification_model as code_review_verification_model,
)
from .verify_models import (
    novelty_model as novelty_model,
)
from .verify_models import (
    plan_review_verification_model as plan_review_verification_model,
)
from .verify_models import (
    register_novelty_contract as register_novelty_contract,
)
from .verify_models import (
    register_verification_contract as register_verification_contract,
)
from .verify_models import (
    verification_model as verification_model,
)

logger = logging.getLogger(__name__)

# ── token-budget chunking: split verify requests by TOKEN budget vs model window ──
DEFAULT_VERIFY_WINDOW_HEADROOM = 0.8  # config-overridable: verify.verify_window_headroom
# Per-finding OUTPUT reserve: the verify response carries one verification object per
# finding, so output scales with finding count. A documented, adjustable constant.
PER_FINDING_VERIFY_TOKENS = 256
# Approximate size of the ~constant verifier SYSTEM prompt: a flat reserve on top of the
# rendered per-finding instructions, not re-estimated per chunk.
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
    empty_outcome_indices: list[int] = []
    outcome_counts = {"clean": 0, "recovered": 0, "empty_outcomes": 0, "unrecoverable": 0}
    for chunk in chunks:
        chunk_indices = [gi for gi, _ in chunk]
        try:
            raw_output = list(run_chunk(verify_instructions(chunk), context) or [])
        except StructuredOutputError:
            # The verifier's turn could not be validated to the `verification` contract — a SHAPE
            # contract failure, NOT a benign "couldn't verify". Surface it LOUDLY (distinct from
            # the quiet degrade below); the chunk's findings still degrade to INDETERMINATE.
            logger.error(
                "verify chunk failed the structured `verification` contract; findings %s "
                "degrade to INDETERMINATE",
                chunk_indices,
            )
            shape_failures.extend(chunk_indices)
            chunk_outputs.append([])
            outcome_counts["unrecoverable"] += 1
            continue
        except Exception:  # noqa: BLE001 — a non-contract failure (network/etc.) → honest degrade
            chunk_outputs.append([])
            continue
        chunk_outputs.append(raw_output)
        if not raw_output and chunk_indices:
            # Returned successfully but structurally EMPTY despite non-empty input — a distinct,
            # loud signal from a legitimate "no findings sent" outcome. Purely additive: those
            # findings still degrade to INDETERMINATE exactly as before.
            empty_outcome_indices.extend(chunk_indices)
            outcome_counts["empty_outcomes"] += 1
        elif raw_output:
            chunk_reshape = reshape_verifications(raw_output, valid_indices=set(chunk_indices))
            if chunk_reshape.has_violations:
                outcome_counts["recovered"] += 1
            else:
                outcome_counts["clean"] += 1
    reshape = reshape_verifications(
        [v for out in chunk_outputs for v in out], valid_indices=sent_indices
    )
    violations = reshape.summary()
    if shape_failures:
        violations["shape_failures"] = sorted(shape_failures)
    if empty_outcome_indices:
        violations["empty_outcomes"] = sorted(empty_outcome_indices)
    if violations:
        logger.error("verification contract violations detected: %s", violations)
    return {
        "verifications": reshape.verifications,
        "omitted": omitted,
        "contract_violations": violations,
        "outcome_counts": outcome_counts,
    }


# ── the SEPARATE Pass-2 novelty sub-call (child 150b) — scores carryover-vs-novel for a
#    remediation re-review. This sub-call ALONE receives the PRIOR findings (as its context); the
#    verification sub-call above and Pass-1 never do, so the independence invariant holds by
#    construction. ────────────────────────────────────────────────────────────────────────────
def prior_findings_block(prior_findings: list[dict[str, Any]]) -> str:
    """The PRIOR-review findings rendered as the novelty sub-call's context (the ONLY place the
    prior findings appear). Each block carries the prior finding's id + prose so the sub-call can
    answer the matches-prior questions and name ``matched_prior_id``."""
    return "\n\n".join(
        f"### prior finding {p.get('id', '?')}\n"
        f"finding: {p.get('finding', '')}\n"
        f"location: {p.get('location', '')}\n"
        f"suggested_fix: {p.get('suggested_fix', '')}\n"
        f"criteria: {', '.join(p.get('criteria', []) or [])}"
        for p in prior_findings
    )


def novelty_instructions(batch: list[tuple[int, dict[str, Any]]]) -> str:
    """The novelty sub-call INSTRUCTIONS over a batch of ``(global_index, finding)`` pairs — the
    CURRENT findings to score. The prior findings are supplied SEPARATELY as context (see
    :func:`prior_findings_block`); they are never inlined here so the listing stays the current
    review's own findings."""
    if not batch:
        return "Score each finding's novelty by its index. Emit one novelty per finding."
    return (
        "For EACH current finding below, by its index, decide whether it MATCHES a specific PRIOR "
        "finding (provided as context). Answer the factual matches-prior sub-answers "
        "(yes|insufficient|no) and name the matched prior id (empty if none). These are FACTUAL "
        "match questions, NOT a judgement of whether to downrank.\n\n"
        f"{finding_listing(batch)}"
    )


def reshape_novelties(
    raw: list[dict[str, Any]] | None, *, valid_indices: Collection[int] | None = None
) -> dict[int, dict[str, Any]]:
    """Reshape a flat novelty-output list into ``{index: {matches_prior, matched_prior_id}}``.
    Mirrors :func:`reshape_verifications` tolerance: a non-int ``index`` is dropped, later wins on
    a duplicate, an out-of-``valid_indices`` index is excluded. A dropped/absent index yields no
    entry — and :func:`rebar.llm.review_kernel.decide.novelty` of an empty matches-prior is 0.0
    (carryover), so a malformed item degrades to the fail-safe automatically."""
    merged: dict[int, dict[str, Any]] = {}
    for v in raw or []:
        idx = v.get("index") if isinstance(v, dict) else None
        if not isinstance(idx, int):
            continue
        if valid_indices is not None and idx not in valid_indices:
            continue
        merged[idx] = {
            "matches_prior": v.get("matches_prior", {}) or {},
            "matched_prior_id": v.get("matched_prior_id", ""),
        }
    return merged


def score_novelty(
    findings: list[dict[str, Any]],
    *,
    prior_findings: list[dict[str, Any]],
    run_chunk: RunChunk,
    window_tokens: int,
    est_tokens: TokenEstimator,
    headroom: float = DEFAULT_VERIFY_WINDOW_HEADROOM,
) -> dict[int, float]:
    """Pass-2 novelty over ``findings`` against ``prior_findings``: token-budget chunk the CURRENT
    findings → run each chunk via ``run_chunk`` (the injected LLM seam, given the prior findings as
    context) → map each finding's matches-prior sub-answers to a novelty via
    :func:`rebar.llm.review_kernel.decide.novelty`. Returns ``{global_index: novelty}`` ∈ [0,1].

    FAIL-SAFE (never silently suppress): with no findings or NO prior findings there is nothing to
    match, so this returns ``{}`` (cc5b then treats every finding as carryover, novelty 0.0). A
    chunk whose ``run_chunk`` raises (any error — contract, timeout, network) contributes no
    novelties and is logged at WARNING; those findings fall back to novelty 0.0 (carryover → never
    dropped). A malformed/garbage item likewise reshapes away to 0.0. A broken novelty signal can
    only make the gate STRICTER, never drop a finding."""
    from .decide import novelty as _novelty_of

    if not findings or not prior_findings:
        return {}
    chunks, _omitted = verify_request_chunks(
        findings, window_tokens=window_tokens, est_tokens=est_tokens, headroom=headroom
    )
    context = prior_findings_block(prior_findings)
    raw: list[dict[str, Any]] = []
    for chunk in chunks:
        try:
            raw.extend(run_chunk(novelty_instructions(chunk), context) or [])
        except Exception:  # noqa: BLE001 — fail-safe: any failure → those findings degrade to carryover (0.0)
            logger.warning(
                "novelty sub-call failed for findings %s; falling back to novelty=0.0 (carryover)",
                [gi for gi, _ in chunk],
            )
    sent_indices = {gi for chunk in chunks for gi, _ in chunk}
    reshaped = reshape_novelties(raw, valid_indices=sent_indices)
    # Every sent finding gets a novelty; an index the sub-call did not (or malformed-ly) cover
    # maps through decide.novelty({}) == 0.0 — the carryover fail-safe.
    return {gi: _novelty_of(reshaped.get(gi, {}).get("matches_prior", {})) for gi in sent_indices}
