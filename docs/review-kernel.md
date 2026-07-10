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

### Code-review consumer (`b744`) — what it plugs in

The code-review gate lives in the `src/rebar/llm/code_review/` package and supplies the
consumer-seam plug-ins (WS1 + WS2), mirroring plan-review's shapes:

- **Domain context** = the DIFF (not plan text). `code_review/assemble.py`'s
  `assemble_diff_context(...)` produces the kernel `context` string (changed-files /
  orientation / diff), the analog of `PlanContext.plan_text`.
- **Pass-1 finders** = the base reviewer (`reviewers/code-review-base.md`, which also emits the
  bounded `recommend_overlays` escalation) + 11 specialist OVERLAY finders
  (`reviewers/code-review-<overlay>.md`, one per `code_review/registry.py:OVERLAY_IDS`). All are
  `category: code-review-pass` (NOT `review`), so they stay OUT of the single-pass
  reviewer-selection catalog + the `reviewers/index.json` drift gate, and resolve via
  `get_prompt(...)`. Their structured-output contracts are registered in
  `code_review/contracts.py` (`code_review_base_output` / `code_review_findings`).
- **Per-criterion routing + thresholds** live in the COMMITTED `code_review/criteria_routing.json`
  (the analog of `plan_review/criteria_routing.json` — hand-maintained + test-validated, NOT
  auto-derived and NOT in any gate YAML). `code_review/registry.py:threshold_for(criteria)`
  reads it and returns `(block_threshold, blocking_enabled)` — the `ThresholdResolver` the
  kernel `pass3_over_findings` consumes (min threshold; any blocking_enabled). The
  secret-detection / high-critical-security keys ship `blocking_enabled: false` (advisory v1);
  WS5 flips exactly those two to `true` (fail-closed). The `applies_to` globs in the routing
  index are the single source for WS3's deterministic Round-A glob-trigger logic.
- **Verify preamble** = `reviewers/code-review-verify.md`, which embeds the
  `VERIFIER_RULES_SCAFFOLD` verbatim and re-grounds findings against the DIFF (severity scored as
  the harm of the change as written). It reuses the kernel's gate-agnostic `verification` contract.
- **Pass-4 move-catalog** = `code_review/moves.py` (a `MOVE_REGISTRY_SCHEMA` instance,
  `validate_move_registry`'d at load; `applies_when` tags from `{OVERLAY_IDS ∪ "always"}`) +
  `reviewers/code-review-coach.md` (the move-pick prompt, `code_review_coach` contract). The
  gate passes `active_triggers` = the union of `criteria` carried by the surviving findings; the
  kernel `coach()` renders the picked move templates deterministically.

The escalation orchestration (base → overlay union, one-hop, capped) + the `gates/code-review.yaml`
workflow + `produce_code_review_verdict` are WS3/WS4 (they do not change the kernel).

### Code-review novelty convergence (epic `374d`, the region-gated rising floor)

Code review converges across patchsets/sessions the way plan-review converges across re-reviews
(ADR 0008), REUSING the kernel novelty primitives unchanged and adding only gate-specific
orchestration — full rationale in **[ADR 0037](adr/0037-code-review-novelty-convergence.md)**. The
behavior a reviewer/author sees:

- **Region-gated drop.** On re-review, a NOVEL + low-priority advisory finding is DROPPED **only if
  the cited code REGION is unchanged** since the prior review. The floor reuses the kernel
  `decide.rising_floor_drop(priority, novelty)` unchanged and ANDs it with a per-citation region
  check (`code_review/region_gate.py`): `REGION_UNCHANGED` → droppable; `REGION_CHANGED` and
  `REGION_UNKNOWN` (path absent / multi-location / absence-evidence / moved / renamed / any error)
  → always RAISE. A dropped finding carries `drop_reason = "novelty-region"`.
- **Carryover labeling.** A finding that MATCHES a prior surfaced finding (low novelty) but is not
  dropped is stamped `carried_from = <matched prior id>` and has its coaching stripped, while
  remaining SURFACED — so a genuine repeat is acknowledged, not re-coached.
- **Keying (typed, disjoint keyspaces).** Local `rebar review-code` memory is keyed
  `session:<session_id>` (artifact `code-review: session:{id}`); Gerrit memory is keyed
  `change:<change_id>` (artifact `code-review: {change_id} @ {revision}`, spanning revisions). The
  reader (`code_review/sidecar.py::latest_code_review_result`) matches only its own title scheme, so
  a prior LOCAL review can never seed a change's FIRST Gerrit review.
- **Resilience.** Region state is CONTENT-ADDRESSED — the artifact stores a `{path: sha256}` map
  (`reviewed_file_hashes`, reusing `plan_review.attest._hash_file`); comparison keys on content, not
  a reachable commit, so it survives a rebase / force-push. File-level in v1.
- **Surfaced-only + fail-safe.** Novelty is scored ONLY against prior SURFACED findings (the
  `blocking` + `advisory` buckets), so a dropped finding never re-enters the prior set (the
  code-review counterpart of the plan-review fix, bug `old-frilly-plankton`). The whole floor is
  fail-safe: any reader/hash/novelty error leaves the verdict fully unfiltered (no drops). It is
  OFF by default (gated on the shared `verify.novelty_drop_active`) and self-gates inert with no
  prior memory. The novelty sub-call (`reviewers/code_review_novelty.md`, `code-novelty` reviewer,
  `code_review_novelty` contract = the SAME kernel `novelty_model`) sees prior findings ONLY in its
  instructions — never the Pass-1 finder (ADR 0008 Invariant 1).

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
- The throwaway single-pass demo reviewers (`code_review/single_pass.py`, formerly
  `code_review.py`) are not migrated (real code review = `b744`, which consumes this kernel; WS4
  retires the single-pass route).
- Threshold/severity **calibration** is deferred; thresholds start high → mostly
  advisory in v1.
