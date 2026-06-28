# The review kernel — the shared four-pass review framework

`rebar.llm.review_kernel` is the **shared kernel** behind rebar's multi-pass LLM
reviews. Industry practice and 2025–2026 research converge on a four-pass review: a
**finder** surfaces cited EVIDENCE against a locked rubric (no model-emitted
severity/confidence); a **separate verifier** validates each finding via atomic,
independent binary sub-questions; a **deterministic policy** (not the model) decides
severity and blocking; and an affirmative **coach** maps the surviving advisories to a
locked move registry.

The value-bearing, **divergence-dangerous** passes live here so that every review
surface — the [plan-review gate](plan-review-gate.md) (the worked reference) and the
code-review gate (epic `b744`, the named second consumer; this epic `blocks` it) —
consumes ONE decision core and ONE binary vocabulary. The decision math and the
verification contract **cannot fork**.

## What the kernel owns (domain-agnostic)

| Pass | Module | Public surface |
|------|--------|----------------|
| **Pass-2 — verify** | `review_kernel.verify` | `verify_findings(findings, *, context, run_chunk, window_tokens, est_tokens, headroom)` → `{verifications: {index: {severity_attributes, binary}}, omitted: [...]}`; the registered **`verification` contract** (`verification_model` / `register_verification_contract`) — the single source of the binary sub-question vocabulary + the severity-attribute enums; the verify orchestration: `verify_request_chunks` (token-budget chunking, GLOBAL indices preserved), `merge_verifications_by_index`, `finding_listing` / `verify_instructions`, and `resolve_verifier_model` (the non-frontier verifier default); the `VERIFIER_RULES` / `VERIFIER_RULES_SCAFFOLD` (the soft prompt rules, below). |
| **Pass-3 — decide** | `review_kernel.decide` | `pass3_decide(verification, *, block_threshold, blocking_enabled)` and `pass3_over_findings(findings, verifs, *, threshold_for)` — the deterministic decision core (validity = graded fraction of the binary sub-answers; impact = mean of the ordinal-mapped severity attributes; priority = validity×impact; the conditional cited-reference veto; per-criterion thresholds **parameterized**). |
| **Pass-4 — coach** | `review_kernel.coach` | `coach(surviving, registry, *, pick, active_triggers)` — gate-on-surviving>0 → the deterministic **applicability filter** (`applicable_moves` / `move_applies`) → the LLM `pick` among ONLY the applicable moves → deterministic render; the move-registry **schema** (`MOVE_REGISTRY_SCHEMA` / `validate_move_registry`, with the `applies_when` field) + the subject validator (`validate_subject`) + `render_coach_notes`. |

Pass-1 (the finder's criteria→batch orchestration) is **not** re-abstracted: the
`ProductionBatchRunner` + the workflow `batch` step already provide it. The workflow
shell (steps/branch/batch/`RunnerAgentStep`/`gate_dispatch`) is engine-provided and is
**not** in the kernel — each gate keeps a thin workflow.

## The consumer seam — what a gate plugs in

A gate consumes the kernel for Pass-2/3/4 and supplies these per-gate plug-points:

1. **Criteria + routing** — the rubric and which criteria run (proportionate
   scrutiny / overlay triggering). Plan-review: `plan_review/registry.py` +
   `orchestrator.route_criteria`.
2. **Finder prompts** — the Pass-1 prompts. Plan-review: `reviewers/plan-review-*.md`.
3. **The domain-context assembler** — the `context` string the verifier re-grounds
   against (plan text vs a diff). Plan-review: `assemble_context(...).plan_text`.
4. **The verify-prompt preamble** — the verifier's system prompt; it **embeds the
   `VERIFIER_RULES_SCAFFOLD`** (below). Plan-review: `reviewers/plan_review_verifier*.md`.
5. **The move catalog** — the Pass-4 moves (their CONTENT). Plan-review:
   `passes.MOVE_REGISTRY` (a registry INSTANCE of the kernel schema). Code review
   supplies code moves.

The per-criterion threshold/posture LOOKUP is a consumer concern too:
`pass3_over_findings` takes a `threshold_for(criteria) -> (block_threshold,
blocking_enabled)` callable, and the token estimator + model window are injected into
the chunker — so the kernel never depends on a gate's registry or tokenizer.

### Worked reference (plan-review) → second consumer (`b744`)

Plan-review is the worked reference: `plan_review/passes.py` re-exports the decision
math + the verify listing/contract + the coach mechanism from the kernel (thin
re-exports, no second copy); `orchestrator.pass3_over_findings` is the thin
threshold-resolver wrapper; `workflow_ops.py` wires the verify/coach steps. The
code-review gate (`b744`) builds on this seam **without copying the passes**: it
supplies its own criteria/prompts/context-assembler/move-catalog and calls
`verify_findings` / `pass3_over_findings` / `coach` directly.

## The verifier-rules scaffold (soft prompt rules)

`review_kernel.VERIFIER_RULES_SCAFFOLD` records the four soft rules a verifier's
preamble should embed — the single discoverable source:

- **independence** — treat each finding as an unproven claim to test; never show the
  verifier the finding's own conclusion/decision;
- **atomicity** — answer each binary sub-question on its own merits;
- **allow-insufficient** — `insufficient` is an allowed, honest answer;
- **verdict-with-citation-not-fix** — judge the claim; do not author a fix.

A gate embeds the scaffold TEXT in its verify prompt. The plan-review verifier prompts
(`plan_review_verifier.md` / `plan_review_verifier_agentic.md`) carry these exact rules.

## Enforcement — structure mechanically, behavior via evals, NO prompt-text lint

Reuse is kept honest WITHOUT making the workflow system brittle:

- **Vocabulary → ONE registered `verification` contract.** Validated through the
  tolerant stack (json-repair + bounded retry + pydantic-ai native outputs); an
  unparseable turn **DEGRADES to INDETERMINATE** (`pass3_decide(None)`), never crashes
  a gate. The contract is small/flat (strict validation's reliability cost is the
  failure mode to avoid).
- **Decision interpretation → ONE `pass3_decide`** (thresholds parameterized).
- **Move registry → a registered schema + load-time validation** (`validate_move_registry`),
  with the `applies_when` applicability field.
- **Drift → the existing contract / derived-prompt-index drift gate.** It keys on the
  output CONTRACT (the prompt front-matter `outputs:` binding), not on prompt wording,
  so the `verification` vocabulary is already covered.
- **Soft prompt rules → the scaffold + this doc for discoverability, ENFORCED by
  BEHAVIORAL evals** — deterministic FakeRunner / structural assertions on the gate
  path (in CI: `tests/unit/test_review_kernel_rules.py`) + a small GATED live eval kept
  OFF the blocking CI path (`tests/external/`, multi-run, lenient threshold).

**No prompt-marker lint is added — deliberately.** A grep over prompt strings for a
marker is brittle and gameable; mature stacks (DSPy, Guardrails, Instructor,
promptfoo) enforce typed CONTRACTS + OUTPUT/BEHAVIOR, never prompt-string greps. We
enforce the binary vocabulary as a typed contract and the soft rules as observable
behavior; the rules' *wording* is intentionally not gated by a lint.

## Out of scope (decisions recorded)

- The **completion verifier stays single-pass** (already binary-per-criterion; a full
  independent verify pass ≈2× the agentic cost for a small, unmeasured gain).
- The throwaway `code_review.py` demo reviewers are not migrated (real code review =
  `b744`, which consumes this kernel).
- Threshold/severity **calibration** is deferred; thresholds start high → mostly
  advisory in v1.
