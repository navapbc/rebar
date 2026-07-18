---
schema_version: 1
title: Asserted-capability grounding probe
description: Plan-review criterion `asserted-capability` (AGENT, code-grounded, advisory).
  The finer capability-SURFACE dual of E4 — a plan asserts a NAMED module provides
  (or lacks) a capability it relies on, and the code refutes it (the dc58/db7b/5886
  miss class). Routing in criteria_routing.json. Ships advisory; promotion is a future
  dogfood-gated change. See docs/plan-review-gate.md.
execution_mode: agentic
category: plan-review-criterion
dimension: codebase-grounding
---
GATE — apply only when the plan RELIES on a checkable claim about a NAMED module/symbol's
CAPABILITY SURFACE. This is the E1-verified miss class (epic 6982 §5): a plan asserts what an
existing, named module can or cannot do, builds on that assertion, and the assertion is false
against the code — so the executing agent builds on a capability that isn't there, or rebuilds
one that already is. Two symmetric directions (ONE concern — capability claims about named
modules must be grounded):

- (A) PROVIDES-claim: the plan asserts named module/symbol M provides capability C and a
  committed step depends on C. Miss examples: `dc58` planned a sign step using a helper
  `src/rebar/llm/completion.py` does NOT have (signing lives in `rebar.signing`); `db7b`
  planned to bound the REVIEW_RESULT sidecar via event COMPACTION, but compaction folds only
  `KNOWN_EVENT_TYPES` and REVIEW_RESULT is reducer-ignored, so compaction never absorbs it.
- (B) ABSENT-claim: the plan asserts capability C is ABSENT / must be BUILT, but M already
  provides it. Miss example: `5886` planned a NEW unmapped-Jira-status alert that `fetcher.py`
  already emits (`_flag_unmapped_statuses` → `fetcher-unmapped-jira-status`).

DISTINCT FROM E4 (blocking, broad assertion/existence probe): in this miss class the named
module EXISTS, so E4's existence check PASSES; the defect is the finer-grained capability-surface
mismatch (module present, specific capability absent — or present when the plan says absent).
Probe that surface. If the plan makes no such named-module capability claim it relies on, this
is not-applicable → PASS.

HOW TO GROUND (use your tools — do NOT rely on training knowledge): for each capability-surface
claim the plan relies on, name the module/symbol, then Grep/Read the module to confirm whether
the specific capability is actually present. For a claim about an INSTALLED dependency's symbol,
use `resolve_symbol` against the environment, not a repo Grep. A capability you cannot corroborate
is treated per the fail-open rule below, not asserted as a gap.

FIRE A FINDING only when the code REFUTES a relied-on claim:
- (A) the named module exists but LACKS the asserted capability the plan builds on; or
- (B) the named module ALREADY PROVIDES the capability the plan says is absent / must be built.
Cite the module path and the line/symbol that grounds the refutation as evidence, and name the
absent (or already-present) capability in the finding.

FAIL-OPEN (abstain-with-coverage): if the claim concerns an EXTERNAL fact the repo tools cannot
settle (another team owns X, a vendor already does Y), or the module reference is too vague to
locate, ABSTAIN — record it as covered-but-unverifiable rather than asserting an ungroundable
gap. Never fabricate a module or a capability.

CHECKLIST SUB-ANSWERS (criterion-local):
- asserts_named_module_capability {yes|no|insufficient} — the GATE: does a committed part of the
  plan rely on a checkable capability-surface claim about a named module/symbol (direction A or
  B)? `no` → not-applicable → PASS.
- asserted_capability_grounded {yes|no|insufficient} — only meaningful when gated in: does the
  claim MATCH the code (verified with Grep/Read/resolve_symbol)? A claim the code refutes is
  `no` (the miss — a finding); an external/unlocatable claim is `insufficient` (abstain).

ADVISORY: this criterion errs toward surfacing and coaches; it does NOT block a claim. Promotion
to a blocking posture is a future dogfood-gated `criteria_routing.json` change per the
advisory→blocking promotion gate in docs/plan-review-gate.md (the standing recorder
`criterion_effectiveness.py` auto-monitors this criterion with zero per-criterion wiring).
