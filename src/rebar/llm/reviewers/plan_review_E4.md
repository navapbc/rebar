---
schema_version: 1
title: Assumption/premise verification [agent]
description: Plan-review codebase-grounding criterion E4 (AGENT). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: codebase-grounding
---
Scan the plan for assertions about the codebase ('X already exists', 'Y does Z', hedges/confident-assertions) and FORCE a Grep/Read probe per assertion; cached/training knowledge is not a substitute. Fail-closed on absent evidence (unverifiable assertion = gap). ANTI-FP: read the named implementation file before flagging a contract-doc-only claim.

CONFIDENT-ASSERTION SCAN PROTOCOL (G-7a): enumerate the assertion-shaped sentences and probe each — do not eyeball. Trigger frames: "X already {does/handles/returns/supports} Y", "X is {safe/idempotent/atomic/thread-safe}", "there is no X", "X guarantees Y", "X can't/never Z". Each such frame is an empirically-checkable claim: Grep/Read for it and treat an unverifiable one as a gap. A committed element resting on such an unbacked claim is graded in Pass-2 via committed_work_relies_on_unbacked_claim.

SCOPE-EXCLUSION SUB-CHECK (G-4): a descoping claim used to EXCLUDE work ("OUT: X — already exists", "handled by Y", "covered by Z") is where a false premise deletes work invisibly (nothing downstream references it), so probe it like any other assertion. DISCRIMINATION (co-located rule): FIRE on an empirically-checkable / codebase exclusion ("X already exists / is handled in code") that a Grep/Read can and does refute; ABSTAIN-with-coverage on an external-fact exclusion the tools cannot settle ("another team owns X", "the vendor already does Y") — record it as covered-but-unverifiable rather than asserting a gap you cannot ground. THIRD-PARTY SYMBOLS: an existence/capability claim about an INSTALLED dependency's symbol ("library.Thing exists / is importable") is settleable by `resolve_symbol` (the installed environment), not by a Grep of the repo — resolve it there and treat an environment-resolved symbol as verified rather than an unbacked assertion.
