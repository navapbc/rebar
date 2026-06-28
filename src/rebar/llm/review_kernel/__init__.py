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

Subsequent workstreams add Pass-2 (the finding-verifier + the ``verification``
contract) and the Pass-4 coach mechanism + the pluggable move-registry schema to
this package.
"""

from __future__ import annotations

from .decide import (
    DEFAULT_BLOCK_THRESHOLD,
    GRADED_BINARY,
    impact,
    pass3_decide,
    pass3_over_findings,
    severity_label,
    validity,
)

__all__ = [
    "DEFAULT_BLOCK_THRESHOLD",
    "GRADED_BINARY",
    "impact",
    "pass3_decide",
    "pass3_over_findings",
    "severity_label",
    "validity",
]
