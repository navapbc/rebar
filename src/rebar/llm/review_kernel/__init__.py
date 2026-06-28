"""The shared four-pass review KERNEL (epic ``vivid-gang-day``).

Industry practice and 2025–2026 research converge on a **multi-pass** LLM review: a
finder surfaces cited EVIDENCE against a locked rubric (no model-emitted
severity/confidence); a SEPARATE verifier validates each finding via atomic,
independent binary sub-questions; a DETERMINISTIC policy (not the model) decides
severity and blocking; and an affirmative COACH maps the surviving advisories to a
locked move registry. The value-bearing, divergence-dangerous passes are extracted
HERE so every review surface (the plan-review gate; the future code-review gate,
epic ``b744``) consumes ONE decision core and ONE binary vocabulary — the decision
math and the verification contract cannot fork.

What the kernel owns (domain-AGNOSTIC):

* **Pass-3** deterministic decision (:mod:`.decide`) — the single ``pass3_decide``
  (+ the validity/impact/veto math) and ``pass3_over_findings``, with the
  per-criterion thresholds **parameterized** so a consuming gate sets its own
  posture without forking the math. This is the framework's single decision core.

What stays per-gate (the consumer SEAM, NOT in the kernel): the criteria + routing,
the finder prompts, the domain-context assembler (plan text vs diff), the
verify-prompt preamble, and the move-catalog CONTENT. The plan-review gate is the
worked reference consumer.

This package owns the three divergence-dangerous passes: Pass-2 (:mod:`.verify`) —
the finding-verifier + the single registered ``verification`` contract + the verify
orchestration (chunking, merge-by-global-index, the verifier-model default); Pass-3
(:mod:`.decide`) — the deterministic decision core; and Pass-4 (:mod:`.coach`) — the
affirmative-coach mechanism + the pluggable move-registry schema (the applicability
filter + the subject validator + the deterministic render).
"""

from __future__ import annotations

from .coach import (
    MOVE_REGISTRY_SCHEMA,
    applicable_moves,
    coach,
    coach_listing,
    move_applies,
    render_coach_notes,
    validate_move_registry,
    validate_subject,
)
from .decide import (
    DEFAULT_BLOCK_THRESHOLD,
    GRADED_BINARY,
    impact,
    pass3_decide,
    pass3_over_findings,
    severity_label,
    validity,
)
from .verify import (
    DEFAULT_VERIFY_WINDOW_HEADROOM,
    VERIFIER_RULES,
    VERIFIER_RULES_SCAFFOLD,
    finding_listing,
    merge_verifications_by_index,
    register_verification_contract,
    resolve_verifier_model,
    verification_model,
    verify_findings,
    verify_instructions,
    verify_request_chunks,
)

__all__ = [
    # Pass-3 — deterministic decision
    "DEFAULT_BLOCK_THRESHOLD",
    "GRADED_BINARY",
    "impact",
    "pass3_decide",
    "pass3_over_findings",
    "severity_label",
    "validity",
    # Pass-2 — finding verifier + the verification contract
    "DEFAULT_VERIFY_WINDOW_HEADROOM",
    "VERIFIER_RULES",
    "VERIFIER_RULES_SCAFFOLD",
    "finding_listing",
    "merge_verifications_by_index",
    "register_verification_contract",
    "resolve_verifier_model",
    "verification_model",
    "verify_findings",
    "verify_instructions",
    "verify_request_chunks",
    # Pass-4 — coach mechanism + move-registry schema
    "MOVE_REGISTRY_SCHEMA",
    "applicable_moves",
    "coach",
    "coach_listing",
    "move_applies",
    "render_coach_notes",
    "validate_move_registry",
    "validate_subject",
]
