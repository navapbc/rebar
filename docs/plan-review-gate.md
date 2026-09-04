# The plan-review gate

The plan-review gate is the **inverse of the completion-verification close gate**:
done, the plan-review gate checks when work **starts** (on **any** entry into
`in_progress` — via `claim`, a plain `transition`, a `blocked` resume, or
reactivating a `closed` ticket) that a ticket's **plan is sound before an agent
executes it**. Early plan defects compound over an autonomous agent's trajectory, so
catching them at the start is high-leverage.

Its posture is to **coach, not roadblock**: it surfaces grounded, actionable
findings the author can address, and emits a signed **attestation** that a review
process was followed — a composable "rigorous agentic development vs vibe-coding"
signal a CI process can check. It is built for a broad, polyglot client base, so it
is **fail-open**: anything it cannot soundly check is skipped (recorded as
coverage), never turned into a false accusation.

> Implementation: `src/rebar/llm/plan_review/` (epic `5fd2-a7c2-0aec-48fa`).
> Reusable machinery it builds on: [llm-framework.md](llm-framework.md) (the runner
> + contracts), [reuse-surface.md](reuse-surface.md) (the signing surface + LLM
> runtime API + the prompt library), [grounding.md](grounding.md) (the code-grounding
> oracle), [event-schema.md](event-schema.md) (the `SIGNATURE` + `REVIEW_RESULT`
> events). The gate runs **on the workflow engine** — see the workflow-engine usage
> docs [workflow-authoring-v2.md](workflow-authoring-v2.md) +
> [workflow-editor.md](workflow-editor.md) (the consolidated `docs/workflow-engine.md`
> is the pending 6f2d WS-DOC deliverable; these are the current authoritative refs).

## Two surfaces

The heavy review and the fast enforcement are **decoupled** (so the claim path
stays fast — target p95 < ~50 ms, no LLM, no network):

1. **`rebar review-plan <ticket>`** (CLI) / **`review_plan`** (write-gated MCP tool)
   / **`rebar.llm.review_plan(ticket_id)`** (library) — the out-of-band review. It
   runs the deterministic floor + the four-pass LLM review on the ticket's *whole*
   plan, emits a `REVIEW_RESULT` sidecar, and on a non-blocking `PASS` **signs** a
   plan-review attestation. This is where the cost + latency live; run it on a
   claim-block or from CI.

   **Not-claimable fast-fail (no LLM).** Because the review's only product is a claim
   attestation, `review-plan` first checks — cheaply, with no LLM or network — whether
   the ticket can be claimed at all. If it **can't** — its status is terminal/paused
   (`closed` / `idea` / `blocked`), or it is `open` but still blocked by an **unclosed
   dependency** (`ready_to_work` is false) — the review returns immediately with an
   unsigned `INDETERMINATE` verdict (`coverage.llm_ran = false`, an `indeterminate`
   finding `ticket-not-claimable`, CLI exit `2`) instead of spending a billable review.
   Delaying the review until the ticket is claimable also means the plan is assessed
   against the environment it will actually execute in — a prerequisite closing may move
   both the plan material and the codebase, which would otherwise invalidate an early
   review anyway (see [Review dependencies FIRST](#review-dependencies-first--review-in-dependency-graph-order)).
   An `in_progress` ticket is **never** fast-failed (it is already being worked, so
   drift/execution re-reviews stay legitimate), and `--force` (library `force=True`)
   bypasses the check to review a not-yet-claimable ticket anyway. `session_log` /
   `code_review` / `identity` tickets are exempt from the whole gate and unaffected; a
   `bug` is reviewed under the bug tier (see above) and so is subject to this fast-fail
   like any other reviewed type.

2. **The start-work gate.** When `verify.require_plan_review_for_claim` is enabled, every entry into `in_progress` checks for a current certified plan-review attestation. This includes `claim`, `transition <id> open in_progress`, a resume from `blocked`, and reactivation from `closed`. Checking the target status prevents the paths `open → blocked → in_progress` and `open → closed → in_progress` from bypassing review. A reviewed ticket keeps its valid attestation across a normal block and resume. Every entry point calls `rebar._commands.gates.plan_review_precheck`. The check invokes no LLM and makes no network request. It verifies the DSSE envelope through SSHSIG against the signer's Ed25519 public key and recomputes the material fingerprint. It does **not** require the certificate principal to be this environment. Certification environment is **not** a gate under current operator policy (bug `c21f-6f29-5d2d-4a5a`): *"Any certification is as good as any other certification right now. Limited to a trusted set of environments is a future feature, but not currently in use."* So a review signed by the on-box MCP server satisfies a claim run from a local CLI worktree, and vice versa. A signature that does not verify is still refused — see `docs/manifest-signing.md` for the `trust_basis` the verifier reports and for the opt-in `verify.require_environment` restriction that narrows the trusted set again. Bugs and `session_log` tickets are exempt from the start-work gate. A bug can still receive a bug-tier plan review. `--force="<reason>"` bypasses the gate and records the reason. `claim` also uses the atomic claim primitive, so two agents cannot claim one ticket.

A review is a **process, not a dialog**: when a finding blocks (or you want to
clear advisories), revise the ticket and re-run `review-plan` to earn a fresh
signature — exactly like the completion verifier.

## The verdict model — four passes (find → verify → decide → coach)

The gate has **two layers**:

* **Layer 1 — the deterministic floor (P1–P11)** — `det_floor.py`. The *only* tier
  that blocks **by default**. Frozen, deterministic, polyglot, fail-open. The
  sound, unambiguous blockers are **P1** (missing `## Acceptance Criteria`
  checklist), **P4** (description above `verify.max_ticket_description_chars`, default 8,000),
  **P5** (a dependency *cycle* among children), **P8** (the ticket
  is too big to review in full even one criterion at a time → "reduce/decompose"),
  **P10** (verification-presence), and **P11** (AC vagueness).
  P2/P3 (file/package resolution via the grounding oracle) are coverage-only;
  P4's AC-count and file-impact signals, P6, and P7 are advisory. That
  blocking / never-blocking line is also the module seam: the four checks that can
  never block live in `det_advisory.py`, P9 in `det_lint.py`, P10/P11 in
  `det_clarity.py`, all re-exported from `det_floor.py`, which stays the entry point.

  **Clarity floor P10 + P11 (ticket 49b8; `det_clarity.py`, the same module-size
  seam as `det_lint`).** Two BLOCKING checks added after backtests over the 7-day
  population and a 200-ticket extended set, operator-approved for blocking; P6
  stays advisory and monolithic, so they are separate checks with their own
  coverage entries:

  * **P10 verification-presence** — a *leaf* plan must state how it is verified:
    a `## Testing` or `## Verification` H2 section, OR at least one `- [ ]`/`- [x]`
    acceptance-criteria item that contains an inline code span (a backtick-fenced
    token) or matches the exhaustive verification vocabulary (pytest / `test_*` /
    `make …` / `rebar …` / `git …` / grep / assert\* / `checked:` / verify-family
    words / "exit code" — nothing else qualifies). A container is a natural pass
    (its children carry the verification detail).
  * **P11 AC vagueness** — the boundary-**fixed** vague lexicon scanned over AC
    item lines only (`det_operator_attested.ac_item_lines`). The old P6 rule
    matched word *prefixes* with no trailing boundary, so `clean` fired on
    "cleanly" / "lint clean"; the fixed rule uses BOTH word boundaries, drops
    `clean`, and keeps `etc.` with a code-span-proximity exemption — span
    positions are recorded on the ORIGINAL line before spans are blanked for
    matching, a hit inside a span never fires, and an `etc.` starting within 30
    characters after a span's end is exempt (a non-exhaustive enumeration of
    already-concrete examples). Measured 0 false positives across 304 passed
    plans. P6's advisory lexicon shares the same fixed matcher so the two
    surfaces agree.

  **Operator-attested evidence-kind lint (P6 family; ticket b080, ADR-0043 ×
  ADR-0016).** `p6_ac_quality` additionally runs an ADVISORY, prompt-less lexicon
  lint (`det_operator_attested.operator_evidence_ac_gaps`, extracted from
  `det_floor` to keep that size-ceilinged module within its ratchet) that flags AC
  checklist items whose
  "done" evidence inherently lives OUTSIDE the codebase — a deploy, a prod/live-run
  outcome, an IaC apply, a cloud-resource state, a merge-gate (Gerrit vote) result,
  a human/operator action, an operator drill, live-store surgery, or a recorded
  out-of-band attestation — but which are NOT tagged `[operator-attested]`. Such an
  AC makes the completion verifier hunt for code proof that cannot exist and burn a
  close-gate cycle (the motivating cases were tickets 115b and 8c4f); surfacing it
  at PLAN time is the cheap fix. It NEVER blocks (it rides P6, which is advisory),
  it is prompt-less (a DET criterion per ADR-0016 — it deliberately does *not* add a
  `criteria_routing.json` entry: DET floor checks are hardcoded in `DET_CHECKS` and
  are not routed through that index, which the packaged-routing CI gate enforces),
  and it is precision-first (a codebase-verifiable suppression co-signal drops items
  that name an in-repo proving command / test / doc / config file, plus a negation
  guard). The `[operator-attested]` tag matcher is single-sourced in
  `det_operator_attested` (`_OPERATOR_ATTESTED_TAG_RE`, re-exported by `workflow_ops`)
  so the lint and the
  completion-verifier enrichment agree on "tagged" by construction. It is self-gated
  by a DETERMINISTIC lexicon precision/recall eval over the historical AC corpus
  (`docs/experiments/plan-review-gate/harnesses/operator_attested_eval.py` over
  `runs/operator_attested_ac_corpus.jsonl`; committed result
  `runs/operator_attested_eval.json`): precision 92.2% on a 64-item flagged census
  (gate ≥70%), flag rate 2.07% (gate ≤5%), recall 61.3% (reported), both known
  cases fire — NO LLM runs in the lint or the eval.

* **Layer 2 — the advisory coaching review (the four passes)** — never blocks by
  default. Each criterion (e.g. the F/E/G/A judgment criteria, the T1–T15
  triggered overlays, COH, ISF, and the advisory `ac-text-quality` / scope
  criteria) ships as a **contract-bearing prompt in the
  prompt library** (`src/rebar/llm/reviewers/plan_review_<id>.md`, `category:
  plan-review-criterion`), loaded via `get_prompt` with `.rebar/prompts/` project
  overrides; its routing (exec/applies_at/block_threshold/posture/checklist) is the
  derived `criteria_routing.json` index. The five pass prompts (finder/verifier/
  coach/ISF/container) are `plan-review-pass` library prompts resolved via
  `resolve_prompt` — no inline prompt strings. (`criteria_v8.json` under
  `docs/experiments/` is the design reference, not the production artifact.) See
  [reuse-surface.md](reuse-surface.md) §3.

The four passes — the find → verify → decide decision core is the shared three-pass
framework (epic `9da1`), plus a coach — the
model emits **no** holistic severity/confidence anywhere in the decision path:

| Pass | What | Where |
|------|------|-------|
| **1 — find** | Surfaces grounded findings `{finding, criteria[], evidence[], scenarios[], impact}` — no severity/confidence. Facet-chunked single-turn finders + one agent per code-grounding criterion. | `passes.pass1_chunk` |
| **2 — verify** | A *separate* verifier re-grounds each finding and emits coarse severity **attributes** + a typed **binary** sub-answer set `{yes\|no\|insufficient}`. One aggregate pass over all findings (token-budget-split only when oversized — see below). | workflow `plan-review-verifier` step |
| **3 — decide** | **Deterministic.** validity = graded fraction of the binary answers; impact = mean of the ordinal-mapped severity attributes; **priority = validity × impact**; decision = `block \| advisory \| dropped`. | `passes.pass3_decide` |
| **4 — coach** | A single-turn call over the *coachable* findings (blocking first, then surviving advisory) maps each to a move from a locked registry; the coaching prose is rendered **deterministically** from the move's template (the LLM only picks the move + names a bounded noun-phrase subject — validated). | workflow `plan_review_coach_inputs` + `plan-review-coach` |

**Verifier model.** Pass-2 verify (and the Pass-4 coach, which share the verify cfg) run on
the decisive non-frontier `VERIFIER_DEFAULT_MODEL` (`claude-sonnet-4-6`) **unless the operator
explicitly chose a model** (a `[tool.rebar.llm.model_classes]` slot, or the deprecated
`[tool.rebar.llm].model`, set to a non-default).

**Verify token-budget chunking.** Pass-2 verify is normally ONE aggregate call. For a
pathological huge-findings ticket whose request would exceed the verifier model's context
window, the findings are split into the minimal number of token-budgeted chunks (a principled
token estimate vs `floor(window × verify.verify_window_headroom)`, default 0.8 — **not** a magic
count), each verified in its own call, and the per-chunk verifications are re-merged by their
global finding `index`. The chunking is encapsulated inside the verify step (the LangChain
MapReduce / LlamaIndex map_reduce pattern), not exposed as a workflow fan-out, so the common
case is byte-identical to a single call. A single finding too large to verify even at the largest
reachable model is left unverified → Pass-3 marks it INDETERMINATE (never silently dropped).
A focused yes/no verification is a decisive, non-open-ended judgement, so a cheaper model
suffices — the same trade-off the completion verifier makes. The downgrade is applied on the
**config** at `review_plan`'s entry (`_verifier_cfg`), *not* as a static step `model:` in
`gates/plan-review.yaml`, because step-level model precedence (step > workflow > config) would
override the operator's choice. The Pass-1 finder is unaffected — it runs the workflow's own
`model_ladder` (Haiku → Sonnet → Opus).

### Pass-3 math (authoritative)

```
validity = mean over the answerable graded binary sub-answers of
           {yes: 1.0, insufficient: 0.5, no: 0.0}          ∈ [0, 1]

impact   = mean( max(prod_impact, debt_impact),            # none/low/medium/high → 0/.33/.67/1
                 blast_radius,                              # local/module/system  → .33/.67/1
                 likelihood,                                # low/medium/high      → .33/.67/1
                 reversibility )                            # easy/moderate/hard   → .33/.67/1

priority = validity × impact                                ∈ [0, 1]
```

Decision rules: the only veto is `cited_reference_accurate == "no"` (fires only
when a finding cites a specific code reference) → **dropped**; `validity < 0.5` →
**dropped**; else **block** iff the criterion has opted into blocking *and*
`priority ≥ block_threshold` (a criterion left at its `0.95` default ⇒ near-certain
*and* high-impact); else **advisory**. Three dogfood-data calibrations have run (stories
`3d3d`, then `usable-chattery-coelacanth`, then the plan-v2 segmented replay in task
`relishable-ammonitic-hoverfly`; see
`docs/experiments/plan-review-threshold-calibration.md`). The current blocking tiers —
the source of truth is `src/rebar/llm/plan_review/criteria_routing.json` — are:
**G6, COH, E2, G5, F1 at `block_threshold: 0.60`** (calibration 1 flipped them to
blocking at 0.70; calibration 2 lowered them to 0.60 on zero-false-positive band
adjudication), **T1, T4, T8, G1G2 at `0.70`** (T4 from calibration 1; T1/T8/G1G2
promoted in calibration 2), and **E4 at `0.75`** (promoted in calibration 2).
Calibration 3 (the first replay segmented to the plan-v2 impact model, per ADR 0036)
demoted **T5e to advisory** — FP-PRONE on the segmented corpus (validity 0.391, 59%
verifier-drop rate, surviving p90 priority 0.27) — and kept the other ten tiers. Every
other LLM criterion stays advisory (`0.95`), including the false-positive-prone
T6/T5b/E5/E6/F4 and the confident-but-routinely-ignored T3/T10. The DET floor
(P1/P5/P8/P10/P11) still blocks unconditionally.

**The hard-override floor is oracle-graded for `ac_unverifiable` (plan-v3, story
`large-sleepful-needlefish`).** `impact_plan` floors a finding at 0.85 when a hard-override axis
is graded at one of its GENUINE-GAP kinds. As of plan-v5 all four override axes are closed kind
sets (see below for `undecomposed` / `dod_uncertifiable`, which were the last two on the ordinal
ladder); `ac_unverifiable` was the first, graded by ORACLE KIND, a closed vocabulary enforced at
verification-parse time (`review_kernel.verify.PlanSeverityAttrs`): **`missing_oracle`**
(no verification method exists as phrased) and **`broken_oracle`** (a stated proving
command/symbol/count is factually wrong, so the stated verification cannot pass) keep
the 0.85 floor; **`underspecified_oracle`** (a check exists or is clearly constructible
— the plan just doesn't spell out the exact command/file/expected value) contributes
`UNDERSPECIFIED_ORACLE_CONTRIB` (0.55, pinned below every blocking threshold) and never
floors — it surfaces and is coached instead of auto-blocking. The split is grounded in
the calibration-3 floor-attribution evidence recorded on the story's ticket: 35.5% of
all plan-v2 blocks were floor-driven, `ac_unverifiable` carried 48.9% of them, and 56%
of a classified sample were specificity demands, not missing oracles. Operator-attested
enrichment clears `missing_oracle`/`underspecified_oracle` (the recorded attestation IS
the oracle) but never `broken_oracle`. Legacy plan-v2 sidecars keep the old ordinal
grades and are read as-is — calibration replay segments by `impact_model_version`
(ADR 0036), which that change bumped to `plan-v3`.

**`divergent_implementation` is divergence-graded the same way (plan-v4, story
`doggish-nonorganic-tsetsefly`).** The second override axis to move off the ordinal ladder onto a
closed kind set: **`contradicts_reality`** (the plan asserts something about the code/system that
is FALSE — a named symbol/file/behavior does not exist as described) and
**`omits_required_site`** (the plan's scope omits a site the change provably MUST touch, where
omitting it changes runtime behavior or leaves the goal unmet) keep the 0.85 floor;
**`incomplete_enumeration`** (the omitted site is optional/cosmetic — a doc mention, a comment, a
redundant reference — and the goal still holds) contributes `DIVERGENCE_INCOMPLETE_CONTRIB`
(0.55, pinned below every blocking threshold) and never floors. The test between the second and
third grade is **consequence, not count**: can the plan's own goal still be met with the site
untouched? Operator-attested enrichment clears `incomplete_enumeration` but never either floor
grade — attesting an outcome neither makes a false claim about the code true nor conjures a
required site the plan omits.

The grading is grounded in plan-v3 field evidence (18,085 verified findings): the axis fired on
only 7.72% of findings, and across the 1,307-finding "omitted scope site / unenumerated consumer"
class it exists to describe it was graded `none` ~90% of the time (1,173) — so a plan that
provably under-scoped reality scored impact **0.0** and could not block, even at G6's permissive
0.60 threshold. Grading rather than merely widening the axis follows the calibration-3 lesson: a
blunt widening would have routed 113 corpus findings into the 0.85 floor at once (4.3% of runs
flipping PASS→BLOCK), the same over-fire the oracle split had to walk back.

**`undecomposed` and `dod_uncertifiable` are kind-graded too — the last two off the ladder
(plan-v5, story `fixable-angular-caribou`).** Ordinal severity labels are an LLM anti-pattern:
models do not apply `none|low|medium|high` reliably enough for deterministic gate behavior, so
each remaining ladder became narrow semantic kinds that map to a consequence in code.

- **`undecomposed`** — **`missing_required_child`** (work the plan or its parent explicitly
  commits to has no corresponding child/sibling; the decomposition is incomplete against its own
  declared scope) and **`no_executable_breakdown`** (no executable step sequence for the unit's
  own scope, or an all-or-nothing build whose riskiest unknown is never de-risked first) keep the
  0.85 floor; **`bundles_separable_slices`** (the unit is executable as written but packs several
  outcomes that could each ship alone) contributes `UNDECOMPOSED_BUNDLED_CONTRIB` (0.55) and never
  floors. The test between the first and third kind is **commitment, not size**: did the plan
  already promise the missing piece as separate work?
- **`dod_uncertifiable`** — **`uncertifiable_outcome`** (a committed outcome has no acceptance
  criterion, test, or proving mechanism at all) and **`certification_cannot_prove`** (a stated
  mechanism cannot establish the outcome — it names something that does not exist or cannot detect
  what it claims, or a trivially broken implementation satisfies it) keep the floor;
  **`underspecified_certification`** (the outcome is certifiable and an oracle exists, the plan
  just doesn't spell out the exact command/path/assertion) contributes
  `DOD_UNDERSPECIFIED_CONTRIB` (0.55) and never floors. Any non-none kind still forces the
  detection amplifier to full weight.

The evidence is a zero-LLM re-score of the recorded corpus (22,631 findings), reported as a
**bound** because the grade→kind mapping over historical prose is inexact: 15–47 of 2,891
at-threshold findings lose their block and 35–64 gain one (gains are recorded grades outside the
old ordinal vocabulary, which `_SEV01` silently scored 0.0). Every loss surviving the optimistic
bound was read individually and is either a bundling observation or a specificity/framing
complaint — **no committed-outcome-without-an-oracle and no broken oracle loses its block**. Method,
mapping rules and the audited loss list: `docs/calibration/plan_v5_kind_sets.md`. Pre-plan-v5
sidecars keep their ordinal grades and are read as-is; `impact_plan` scores a stale ordinal on
these axes as 0.0, so replay MUST segment by `impact_model_version` (ADR 0036).

> **Constraint on any future impact change — loop termination.** A rejected alternative was a
> `prod_impact` floor (lift impact whenever production severity is medium+). It was rejected
> because it runs counter to the **novelty convergence floor** (`rising_floor_drop`,
> `novelty_priority_floor = 0.4`), which makes the remediation loop terminate by dropping
> novel + low-priority findings. 73.7% of plan-v3 findings sit below that 0.4 floor and 47.7% are
> at priority exactly 0.0 — that population IS the convergence reservoir. The decisive objection
> is qualitative: a divergence grade describes something the AUTHOR CAN FIX (add the site, and the
> next review scores it `none`), so the loop still converges; `prod_impact` describes a
> consequence the author cannot edit away, so a lifted finding can recur at high priority every
> round with no action that resolves it. **Any future impact change must preserve the property
> that a lifted finding is author-resolvable.** A `prod_impact` floor at the 0.70 it would need
> also inverts the deliberate calibration-3 ordering that keeps `UNDERSPECIFIED_ORACLE_CONTRIB`
> (0.55) below every blocking threshold.

### The Pass-4 move registry

The coach maps each surviving advisory finding to one **move** and renders the prose
**deterministically** from the move's locked template (the LLM only picks the move id
and fills a bounded noun-phrase `{subject}`). The built-in registry
(`orchestrator.MOVE_REGISTRY`):

| id | move | template (rendered with `{subject}`) |
|----|------|--------------------------------------|
| 1 | spike | "Consider a short spike to de-risk {subject} before committing the plan." |
| 2 | prior-art research | "Research prior art / OSS for {subject} before building it custom." |
| 3 | pre-mortem | "Run a quick pre-mortem on {subject}: how could this plan fail?" |
| 4 | riskiest-assumption test | "Test the riskiest assumption behind {subject} first." |
| 5 | weigh alternatives | "Weigh at least one structural alternative for {subject}." |
| 6 | specification by example | "Pin down {subject} with a concrete worked example." |
| 7 | thin vertical slice | "Prove {subject} end-to-end with a thin vertical slice first." |
| 8 | ADR / one-way-door | "Record an ADR for {subject} — it reads like a one-way door." |
| 9 | plan the verification | "Plan how {subject} will be verified in-session — restate any deferred or unobservable success target as an observable proxy." |
| 14 | state attestation evidence | "State the concrete attestation evidence the [non-codebase] {subject} will require (a change id / vote outcome / timestamp), recorded on the ticket." |
| 10 | foundation/enhancement split | "Deliver {subject} with existing machinery first; make the ideal version a dependent follow-on ticket." |
| 11 | propagate to children | "Propagate the revision for {subject} to the child tickets." |
| 12 | generalize the finding | "Generalize {subject} across the rest of the work." |
| 13 | realign to parent plan | "Realign {subject} to the parent's plan — the parent wins on conflict; if the parent is genuinely wrong, update the PARENT first (which forces its re-review), never silently diverge the leaf." |
| 15 | sample, not the population | "Treat {subject} as one sample from a population, not a single item to fix — enumerate the whole population by the distinct ways the pattern occurs, then add a machine-checkable acceptance criterion that fails while any instance remains." |
| 16 | out-of-loop proof | "Build an out-of-loop proof of {subject} into the plan — an execution step that confirms the implementation works via the fastest local run or manual probe against the real target, before it is committed to the slow delivery loop." |

Move **15** is scoped to `G1G2`, `E4`, `G6`, `A1` — the criteria where a finding names one
member of a population. It exists because coaching is rendered per finding, so a broken
enumeration *method* otherwise surfaces as N instance-level fixes across N review passes;
the machine-checkable half is what makes "I enumerated them all" falsifiable rather than a
claim the next pass has to re-test.

**Project-extensible:** a project adds or overrides moves by id via
`.rebar/plan_review_moves.json` (`{move_id: {name, template, applies_when?}}`; the
template must contain a single `{subject}` placeholder). An absent or empty
`applies_when` makes the move **always applicable**; a non-empty list makes it apply only
when its entries **intersect** the active criterion triggers of the surviving findings.
The **C1 subject validator** (`passes._validate_subject`) rejects
code/imperatives/overlong subjects so the move can only ever name what to investigate,
never hand over a solution.

### Dogfooding a project portability guard

rebar dogfoods its own extension boundary with a real project criterion,
`project.portability` (epic `jira-reb-1003`), which flags a plan that bakes in an
assumption breaking one of rebar's supported client shapes. It is a **project** criterion
— authored in this repo's `.rebar/` overlay, never a packaged built-in — yet it composes
across all four passes with no core change.

**Pass 1 — the finder (criterion + rubric).** Activated and routed in
`.rebar/criteria_routing.json`:

```json
{
  "plan_review": {
    "project.portability": {
      "exec": "1-TURN",
      "facet": "project-invariants",
      "applies_at": { "scope": ["container", "leaf"] },
      "default_posture": "blocking",
      "block_threshold": 0.9
    }
  },
  "activate": {
    "project.portability": ["plan_review"]
  }
}
```

Its rubric lives at `.rebar/prompts/plan-review-project-portability.md`
(`execution_mode: single_turn`) under four second-level headings — `## Finding threshold`,
`## Required finding fields`, `## Supported client-shape matrix`, and `## Non-findings` —
and emits a finding only when all four counterexample elements are present, typed as:

- `location: str` — the plan citation;
- `finding: str` — the assumption plus its causal mechanism;
- `scenarios: list[str]` — the alternate client shape plus the observable breakage;
- `evidence: list[str]` — the plan quote plus grounding facts;
- `criteria: list[str]` — containing `project.portability`.

A finding's alternate shape must come from the supported client-shape matrix:

- `Harness`: Python library, CLI, remote MCP; no Claude Code or Codex dependency.
- `Target project`: Ruby, Python, Java, Next.js, .NET, Terraform subprojects in a monorepo.
- `Platform and venue`: macOS, Windows, Linux, BSD, CI under any provider (GitHub Actions, GitLab CI, Jenkins, and others), projects with NO CI provider at all, servers, developer workstations.
- `Project location and access`: in-checkout current working directory, explicitly located workspace, server outside the checkout, no unrestricted-local-filesystem assumption.

One rule under the matrix makes the CI venue concrete: **a capability whose only
trigger is a specific CI system is not portable.** A plan that schedules recurring or
automatic work must also name an operation-linked or in-process fallback; naming only a
CI trigger is itself a finding, whose alternate shape is `a project with no CI provider`
and whose observable breakage is that the capability never runs at all.

Two non-findings keep it from firing on benign plans:
`Silence about portability is not a finding`, and
`Project-specific behavior behind project configuration or an explicit extension boundary is allowed`.

**Pass 2 and Pass 3 — unchanged.** Pass 2 verifies the finding's validity and impact, and
Pass 3 decides its disposition, exactly as for a built-in; the project criterion plugs into
the generic machinery with no override.

**Pass 4 — the coaching move.** A project-owned move in `.rebar/plan_review_moves.json`,
id `project-portability`, name `restore rebar portability`, `applies_when
[project.portability]`, with the locked template
`Rework {subject} so it remains portable across supported rebar client shapes; keep project-specific behavior in project configuration or an explicit extension boundary.`
— Pass 4 consumes this project move for the surviving `project.portability` findings.

**Calibration.** A balanced eight-case corpus at
`.rebar/evals/plan-review-project-portability.eval.yaml` (four must-fire, four
must-not-fire) is run live with `rebar criteria eval project.portability --runs 3`; the
release thresholds are `recall: 1.0`, `false_accept: 0.0`, `agreement: 1.0`, per-case
`stability >= 0.6666666667`, plus expected-vs-observed fire/no-fire `kappa >= 0.70`.

### An advisory failure-disposition guard

A second project criterion, `project.failure-disposition-contract` (ticket
`slavish-unwieldy-mastiff`, incident `1c0d` prevention), dogfoods the same extension
boundary but ships **advisory** rather than blocking. It flags a plan that **adds or
alters** failure / timeout / exception / retry / fallback / circuit-breaker semantics but
does not state its **failure-disposition contract** — per affected arm, whether the
surfaced disposition is retryable/transient vs fatal/permanent; and, when a fallback chain
exists, which leg wins on fallback-failure (a retryable primary must never be masked by the
fallback's own non-retryable failure — the `1c0d` root cause, where a config flip enabled a
fallback whose degraded terminal arm masked a retryable primary throttle). Like
`project.portability` it is a **project** criterion authored in this repo's `.rebar/`
overlay, and like `necessity` it carries **no DET trigger** in v1 — applicability is
LLM-judged from the rubric, so the criterion self-gates (PASS when the plan touches no such
semantics).

Activated and routed in `.rebar/criteria_routing.json`, advisory and single-turn:

```json
{
  "plan_review": {
    "project.failure-disposition-contract": {
      "exec": "1-TURN",
      "facet": "project-invariants",
      "applies_at": { "scope": ["container", "leaf"] },
      "default_posture": "advisory",
      "block_threshold": 0.9,
      "checklist": [
        { "key": "affects_failure_disposition", "check": "GATE ..." },
        { "key": "disposition_contract_stated", "check": "REQ ..." }
      ]
    }
  },
  "activate": {
    "project.failure-disposition-contract": ["plan_review"]
  }
}
```

Its rubric lives at `.rebar/prompts/plan-review-project-failure-disposition-contract.md`
(`execution_mode: single_turn`, `dimension: project-invariants`) and answers two
checklist sub-answers: the applicability **GATE** `affects_failure_disposition
{yes|no|insufficient}` (no → not-applicable → PASS) and, only when gated in, the requirement
`disposition_contract_stated {yes|no|insufficient}` — clause (1) a disposition word per
affected arm, and clause (2), when a fallback chain exists, the fallback-failure winner
preserving the most-recoverable leg. It carries **no** `suppress_types`, so it is silent on
the light bug tier (only `registry.BUG_TIER_CRITERIA` run there) yet reviews an **escalated
bug** — one whose `file_impact` declares a non-test path, like the `8fbd` `rebar.toml`
config flip — under the full rubric with `ticket_type=None`. An explicit clause keeps it
**orthogonal to T5b**: T5b asks whether error handling exists at all; this criterion asks
whether an added/altered failure path's disposition contract is *stated* — a new call that
ships retry/backoff *and* a per-arm disposition statement PASSES here.

Because it ships advisory, it **never blocks**; promotion to a blocking posture is a future
dogfood-gated `.rebar/criteria_routing.json` change, monitored with zero per-criterion
wiring by the standing effectiveness recorder. **Calibration.** A bounded hand-authored
seven-case sanity corpus at
`.rebar/evals/plan-review-project-failure-disposition-contract.eval.yaml` (three must-fire,
four must-not-fire) — NOT an E2/E3 batch eval — is run live with `rebar criteria eval
project.failure-disposition-contract --runs 3`; the frozen operator-attested expected
outcome is committed at
`docs/experiments/plan-review-gate/runs/failure_disposition_sanity.json` (`recall: 1.0`,
`false_accept: 0.0`), and the offline CI proxy proves the corpus TP/TN shape and calibration
arithmetic under an injected perfect solve.

Per-criterion regression fixtures are **mined** from the persisted `REVIEW_RESULT` sidecar
corpus rather than hand-authored, and admitted only under a written bar — admissible
evidence, the escaped-defect positive-only rule, the vintage gate, tiered admission bars, a
reproduction-consensus-plus-epoch-majority reproduce-before-admit rule, change-triggered
execution off the weekly cron, and the gap-only heal loop with its `unreliable-criterion:`
breaker. That protocol is recorded in
[ADR 0113](adr/0113-fixture-mining-and-admission-protocol.md)
(`docs/adr/0113-fixture-mining-and-admission-protocol.md`), which extends the ADR 0109 replay
harness.

### The advisory cap

The surfaced advisory findings are capped at the top-N by priority (default **20**,
`orchestrator.DEFAULT_ADVISORY_CAP`); the overflow goes to the `REVIEW_RESULT` sidecar,
not the agent, and the **overflow count** is reported on the verdict
(`coverage.counts.advisory_overflow`, shown as `overflow=N` in the CLI summary) so a
capped list never reads as a complete count. **Blocking findings are exempt** — all of
them are always returned; the cap can never weaken the block decision. (Volume is the
lever that preserves an LLM's ability to act on feedback; the cap is a tunable default,
not a validated constant.)

### Advisory triage (apply-now vs defer)

Report §5.2 found the dominant plan-review leak is advisory **latency**, not blindness:
4/8 tickets with persisted reviews applied a surfaced advisory only *after* claim
(CAUGHT-BUT-IGNORED). Nothing told the author *which* surviving advisories were worth
applying now. So Pass-4 also runs a **deterministic advisory triage** over the surviving
advisory findings (`passes.triage_advisories`), attached to the verdict as `verdict["triage"]`
— a structured array `[{id, criteria, priority, block_threshold, bucket, reason}]`, one entry
per surviving advisory. It makes **no** LLM call and emits no free prose (only fixed tokens +
the findings' recorded numbers), so the same finding set yields byte-identical output. It is
NOT a `MOVE_REGISTRY` entry — the registry's per-finding `{subject}` template cannot express a
ranked bucket split — and the shared kernel coach mechanism is unchanged.

**Ranking rule.** For each surviving advisory, using only its recorded `priority`
(= `validity × impact`) and `block_threshold` (the criterion's blocking waterline; DET-tier
advisories that don't carry it fall back to `DEFAULT_BLOCK_THRESHOLD = 0.95`):

- **Bucket** — `apply-now` iff `priority >= block_threshold - APPLY_NOW_MARGIN` (default
  `APPLY_NOW_MARGIN = 0.10`, i.e. the advisory came within the margin of blocking); otherwise
  `defer`, with a numeric `reason` (e.g. `deferred: priority 0.32 is 0.28 below its 0.60 block
  line`).
- **Order** — `priority` DESC, then `criteria[0]` ASC (empty `criteria` sorts last via the
  sentinel `"~"`), then `id` ASC — a total order, so the output is byte-identical run to run.
- **Eligibility** — only findings with `decision == "advisory"`; blocking findings must be
  remediated regardless and are excluded.

**Dogfood loop.** This is a ship-first coaching move with no eval gate; its effect is watched
post-ship via **R7**'s instrumentation — the per-criterion advisory-application-latency signal
(does the CAUGHT-BUT-IGNORED rate fall as authors act on the `apply-now` bucket pre-claim?).

## Proportionate scrutiny & routing

Criteria carry an `applies_at` descriptor (`registry.applies`) whose proportionate
scrutiny is keyed on **container (has children) vs leaf (no children)** — never on
ticket TYPE, so a childless epic is scrutinised as a leaf and a story with children
as a container. `applies_at.scope` lists the nodes a criterion runs at (`["container",
"leaf"]`, either or both; absent ⇒ both): leaf-implementation and code-grounding
criteria are `["leaf"]`, container child-coverage criteria (G3/G4) are `["container"]`,
and cross-cutting criteria (incl. the **T5c security** overlay) run at both — a
regression fix, since a type-`levels` gate previously withheld security review from
container epics that stand up infrastructure. The **T10 infra** overlay additionally
checks an *endpoint access contract*: any network-reachable service a plan stands up
must state its human/admin authentication (a named mechanism **or** a justified
no-auth), independently of the machine credentials (deploy keys/tokens) it configures.
Separately, **`session_log` / `code_review` / `identity` tickets are exempt** from the whole
gate (a distinct exemption axis, not part of container/leaf scrutiny). A **bug is NOT
exempt**: since the bug review tier (epic 6982/R4) it gets a light advisory review — the DET
floor plus the restricted `BUG_TIER_CRITERIA` probe. P1/P10 readiness-floor failures and P4
description admission failures still BLOCK and short-circuit before the LLM tier; remaining DET
findings are downgraded to advisory so a well-formed bug in that tier can be coached without the
full rubric. A bug whose declared blast radius names non-test paths is reviewed by the full
blocking rubric instead (see R4(c) below). (A bug still needs no signed attestation
to be *claimed*; that CLI-side exemption is a separate axis and is unchanged.)
**The intended sequence**: claim a bug without plan review to perform root-cause
analysis (RCA) first — the claim-time exemption exists exactly so RCA is not
blocked on a review of work that doesn't exist yet. Once RCA yields an
implementation plan and a recorded `file_impact` for a **complex** remediation (one
whose blast radius names non-test paths), that plan escalates out of the light bug
tier and must pass the full, blocking-capable LLM plan review (an explicit `rebar
review-plan <bug>`) before implementation begins — see R4(c) below. A **simple**
bug (blast radius stays test-only) may proceed through the light advisory tier
without a blocking gate. Neither case implies every bug needs review before claim,
nor that a complex bug's remediation stays exempt once its blast radius is known.
Mechanical/test *leaves* suppress
noisy criteria. Overlays fire from
low-false-positive deterministic triggers where safe (T5a/T5d/T7/T12) and are
LLM-routed otherwise. **Only the code-grounding set (E4/G1G2/A1/G6) greps the
codebase**; everything else reasons from the plan text. The reviewed plan is
**always whole** — never truncated, never content-chunked; the rubric is the lever
that fits a context window (batch criteria → one-criterion-per-call → escalate the
model → if still too big, P8 fails it as "reduce the ticket").

## Attestation, freshness & invalidation

On a non-blocking `PASS`, `review_plan` calls `rebar.signing.sign_manifest` and appends a `SIGNATURE` event. The record contains a DSSE envelope whose in-toto Statement binds the full manifest. The envelope carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key. The certificate principal identifies that environment. The manifest begins with `plan-review: PASS` and includes the material fingerprint derived from the description, acceptance criteria, file impact, and decomposition. The start-work gate verifies the following conditions:

1. the signature is **certified** under the environment key;
2. it is a **plan-review** manifest (not a completion one);
3. it was made at the **current code HEAD** (the same freshness binding the close
   gate uses) — a code commit since the review invalidates it;
4. the bound material fingerprint matches the **current** ticket — a material edit
   (description/AC/file-impact/decomposition) invalidates it. (Tags/comments/links/
   assignee are *not* material and do not invalidate. Neither is AC **checkbox
   state**: `- [ ]` vs `- [x]` — either bullet (`-`/`*`), either case — is
   normalized to `[ ]` before the fingerprint is hashed (change 330c), so flipping
   boxes never stales an attestation; only edits to an item's TEXT do. Neither is
   **insignificant whitespace**: line-ending form (CRLF/CR vs LF), whitespace at the
   end of a line, and blank lines at the start/end of the description are normalized
   out too (bug 2be7), so a stripped or added trailing newline never stales an
   attestation. LEADING indentation *is* material — it restructures markdown list
   nesting — as is any interior whitespace change.)
5. it **post-dates the latest reopen** — reactivating a ticket (`closed → open`,
   recorded as `state["last_reopened_at"]`) invalidates an attestation signed before it.

**Validity is computed on read.** `plan_review.attest.compute_validity(attestation, ticket_state, kind)` evaluates applicability whenever a gate reads a record. Transitions do not clear or mutate the record. A DSSE operation certificate that carries an SSHSIG signature over its PAE bytes, produced with the environment's Ed25519 key and attributed to that environment, can remain cryptographically certified while becoming invalid for the gate after a reopen, material edit, code change, or pin change. Gates therefore call `compute_validity` after certification. A legacy HMAC record for `plan-review` or `completion-verifier` remains in append-only history, but it returns `unknown_scheme` and cannot certify a current gated operation. The kind-keyed `attestations` map lets plan-review and completion-verifier records coexist. The top-level `signature` field remains a compatibility mirror of the most recent record. See ADR 0073 for the validity-on-read decision.

### Ticking an AC checkbox is attestation-SAFE — and the message now names what changed

**Flipping `- [ ]` to `- [x]` never invalidates an attestation.** Box state is normalized to
`[ ]` before the fingerprint is hashed (change 330c), so the close precheck's
require-all-ticked rule and the claim gate's material binding cannot contradict each other.
Adding *evidence prose* next to a ticked box **is** an edit to `description`, and that does
invalidate — which is why three agents once reported three different answers to the same
question (bug 94a3). Re-saving a description whose only difference is insignificant
whitespace (a stripped/added trailing newline, trailing spaces, CRLF) is likewise safe —
it is normalized out before hashing (bug 2be7).

They could not tell the cases apart because every staleness message recited a fixed list —
"description/AC/file_impact/children" — that named an input the fingerprint does not even
have ("AC" is not a component; acceptance criteria live *inside* `description`). Since 94a3
the manifest carries a per-component fingerprint for each basis key, as additive
`material-part: <name> <hash16> <size>` lines, and a `stale-material` reason **names the
component that actually moved**:

```
plan-review close gate: stale-material: the plan was materially edited since review —
changed: description (4210 -> 4396 chars), children (3 -> 4)
```

The components are exactly `ticket_id`, `description`, `file_impact`, `children`, plus
`file_impact_scope` when the scope is an explicit `none`. The lines are diagnostic only —
nothing decides on them, and a malformed one is skipped rather than raised, so they can never
turn a staleness refusal into a parse error. An attestation signed **before** 94a3 has no such
lines; it degrades to naming a `children` change from its signed `plan-material-pin:` ids
where it can, and otherwise says plainly that the component cannot be named and that
re-running `rebar review-plan` will make future messages specific.

The same principle applies to the other verdicts: `stale-code` names the drifted dependency
files, `stale-head` names both SHAs, `stale-reopened` names the sign and reopen timestamps,
and `unsigned` names both remedies (`review-plan` to earn an attestation, `sign-review` when a
review PASSed but its attestation failed to persist).

The attestation means **"a review process was followed, no blocking red flags, with
coverage recorded"** — *not* "perfect". The rich per-criterion verdicts live in the
sidecar; a project composes any hard CI gate by checking the signed result + its
coverage.

### Review dependencies FIRST — review in dependency-graph order

A plan review binds **more than the subject's own material**: it also pins the material of
the subject's **direct dependencies** — its children (for a container/epic) and its
prerequisites (`depends_on` / `blocks` targets). A review is valid only while **both** the
subject's own material **and** every pinned dependency's material are unchanged since the
review ran. If a dependency's plan changes **after** the review, the recorded `PASS` no
longer describes the plan it was based on, so it is **invalidated** — and this holds on both
paths that could certify it: the in-review sign (`generation.sign_manifest`) and the cheap
recovery `rebar sign-review` (`resign_plan_review`) **both** refuse a review whose dependency
drifted. `rebar sign-review` is **not** an escape hatch for a changed dependency — it recovers
only a *transient* signing failure where nothing material actually moved.

The practical consequence — **review the dependency graph bottom-up, not in parallel:**

- Review (and settle) a ticket's **prerequisites and children before the ticket itself**. A
  dependency whose own review is still landing is still changing; reviewing its dependent now
  will just be invalidated when the dependency's plan is finalized.
- **Do not review a ticket and its dependencies concurrently.** If you fan reviews out across an
  epic and its children at once, a child's plan moving mid-review invalidates the epic's review
  (correctly) — you pay for the epic's (minutes-long) LLM review and then have to re-run it. This
  is the single most common cause of a `PASS` that ends unsigned with
  *"a dependency's plan material changed since the review; re-review required"*.
- When a review is invalidated by a genuine dependency change, a fresh `rebar review-plan`
  **is** required (the recorded verdict is stale). Only a nothing-changed transient is
  recoverable with `rebar sign-review <id>`.

The gate now **enforces the first rule** rather than just advising it: reviewing a ticket that
is still blocked by an **unclosed** `depends_on` / `blocks` prerequisite is
[fast-failed](#two-surfaces) (`INDETERMINATE`, no LLM) instead of producing a `PASS` that a
prerequisite closing would immediately invalidate. So the natural order is forced — close a
ticket's prerequisites, *then* review it. (Use `--force` only when you deliberately want to
review a not-yet-claimable plan; expect to re-review once the prerequisites land.)

`next-batch` returns a conflict-aware, dependency-ready batch; prefer it (and plain dependency
order) over ad-hoc parallel review of related tickets.

### Checking currency cheaply — `review-plan --status` (no LLM)

An attestation that read `PASS` when it was signed can silently stop being **current** —
most often because `origin/main` (the base ref the review was pinned against) advanced,
or the plan was edited. Because validity is computed **on read**, you never have to re-run
the billable review just to *learn* the current verdict: the exact check the `claim` gate
runs is exposed as a read-only, no-LLM, no-network command:

```sh
rebar review-plan <id> --status        # exit 0 = current, 12 = stale/absent; add -o json
```

It prints the currency verdict — `certified` when the attestation is valid right now, else
the specific reason (`stale-code` / `stale-head`, `stale-material`,
`stale-reopened`, `unsigned`, …) — plus the **code anchor the plan was reviewed against**
(the pinned `verified-at-sha` for a `--source attested` review, else the signed HEAD). The
library seam is `rebar.llm.plan_review_status(ticket_id)` (wrapping `claim_gate_check`).

**`--source local` never signs.** A local review reads the in-place checkout — uncommitted
edits included — so its PASS is real feedback but is **not certifiable**: it carries
`signature.signed=false` with the machine-readable reason `local-source-never-signs`, the
claim gate stays unsatisfied, and `rebar sign-review` refuses to re-certify a local-source
PASS from the sidecar (the sidecar records the resolved `source` for exactly this refusal).
The invariant behind the rule: **no new signed attestation may carry a null
`verified-at-sha`** — a signature asserts the plan was reviewed against a specific committed
tree, which a dirty worktree cannot name. Enforced once at the signing seam
(`attest.sign_plan_review`), so the review, recovery (`sign-review`), and drift-refresh
paths all inherit it (ADR 0005; bug `melancholy-firstborn-shihtzu`). Pre-existing unpinned
(pre-S4b) attestations remain readable. To review-and-sign offline, use the attested source
with a local ref instead: `--ref HEAD` resolves from the local object DB with no network.

### Running the gate over MCP without a client timeout — `*_start` + poll

Over the MCP server a gate can run longer than the client's request deadline (~60s). When
it does, the client gets a `-32001` timeout **while the server keeps running the gate** — an
ambiguous non-signal: the caller cannot tell whether the review is still in flight, already
signed, or dead, and a blind re-run launches a **second, double-billed** LLM pass (bug
`jeanlike-hick-azurevase` / `d80d-7be7-1c0a-4231`). Two additive protections close this:

- **Prefer the async starters.** `review_plan_start(ticket_id, …)` and
  `verify_completion_start(ticket_id, …)` return a `{job_id, ticket_id, gate_type,
  status:"running"}` handle in milliseconds and run the gate on a background daemon thread
  (mirroring `run_workflow`), so it OUTLIVES the request deadline. Then POLL for the verdict:
  `plan_review_status` / `verify_completion_status` read the durable **signed attestation**
  (the authoritative result), and `gate_status(job_id)` reads the run handle
  (`running` → `passed` / `failed`, or `stale-running` if the daemon died mid-run). For
  plan-review jobs, `gate_status(job_id).findings.readable` is the per-run
  `REVIEW_RESULT` receipt: do not read the latest findings sidecar for remediation until it
  is `true`, because a terminal BLOCK verdict has no signed attestation and an older
  sidecar may otherwise still be the newest readable record. The
  `.rebar/gate_runs/<job_id>` index is a **local** handle only — like `run_workflow`, the
  daemon does not survive the process exiting and there is no reaper; the verdict a fresh
  process trusts is always the attestation, not the index.
- **The sync tools are de-dup-protected fallbacks.** `review_plan` / `verify_completion`
  still work, and a concurrent same-key call now **attaches to the in-flight run** and shares
  its verdict instead of starting a second billable pass — so an accidental re-fire after a
  `-32001` no longer double-charges. The key is `sha256` over the gate type, the canonical
  ticket id, the resolved base SHA, the variant, and the readonly flag; `force=True` bypasses
  de-dup, and the kill-switch `REBAR_MCP_DEDUP=0` disables it entirely. A **different**
  `basis_ref` is a different key (a legitimately distinct run), and a re-call **after** the
  first completes re-invokes (the slot is purged on completion).

Never wrap either gate in a shell `timeout` (see AGENTS.md's bounding section): they are
bounded workloads that terminate with a verdict — background them if you must keep working,
but let them finish.

### A congestion refusal is an expected outcome, not a gate failure

A review host bounds how many plan-review and completion-verifier runs may execute at once
(`[snapshot].max_concurrent_gates`, default 4 — see
[config.md](config.md#repo-snapshot-gates-snapshot--optional-env-first-see-repo-snapshot-gatesmd)).
**At capacity a gate is refused immediately rather than queued.** A queued gate would still hold
its thread and its ~739 MB resident while waiting, converting disk pressure into memory
pressure, and would hold the MCP client's request past its deadline.

The refusal is **not a verdict**. It is never `INDETERMINATE`, never a `BLOCK`, and on the
review-bot path never an `LLM-Review -1`: nothing about the ticket was judged, because the gate
never started. Recognise it by shape:

| Surface | What a congestion refusal looks like |
|---|---|
| MCP | `{"error": "gate_congested", "retryable": true, "resolution_class": "WAIT_AND_RETRY", ...}` — no `verdict` key |
| CLI | exit **11** ("transient — retry"), with an `Error:` line naming host congestion |
| host logs | one `GATE_CONGESTED {json}` marker naming the gate, ticket and limit |

If the cap's own plumbing is locally unusable the gate is admitted and a
`GATE_ADMISSION_DISARMED` marker is emitted instead — worth alerting on, since the bound was not
in force. If the scratch volume itself is unreachable the gate is refused with
`gate_scratch_unavailable`, which is a host fault to fix, not a review outcome.

The correct response is to **back off and retry**, not to re-run immediately, escalate, or record
a result. Persistent congestion means the host is oversubscribed: either reduce parallelism, or
raise the cap if the box genuinely has the memory and scratch space for it.

### Resuming exactly the latest review — `review-plan --retry`

A plan review can land on **INDETERMINATE** without failing on the merits: an LLM call for a
finder unit degrades non-transiently, or the per-invocation budget cap sheds some criteria
before they run. Re-running the full `review-plan` recomputes every unit; `--retry` is the
narrow operator override that **resumes only the exact latest retained review** and pays the
model only for what is still missing:

```sh
rebar review-plan <id> --retry         # resume the latest INDETERMINATE; add --no-sign to skip signing
```

**When it is eligible.** `--retry` acts only when the ticket's newest retained
`REVIEW_RESULT` sidecar is an **INDETERMINATE** verdict carrying a current, versioned
discovery journal with **at least one retryable missing unit** (a `failed`/`cancelled` unit,
or a budget-shed criterion). It then reuses the checkpointed findings of the units that
already succeeded — issuing **zero** model calls for them — and re-attempts only the missing
units, under a **fresh per-invocation attempt budget** (so a unit shed or degraded last time
gets a clean re-attempt; whether a shed unit actually issues a call still depends on the
fresh cap admitting it). The reused set spans both clean and blocking successes; verification
and coaching run only for findings newly recovered by the re-attempt, and the deterministic
decision/assembly reruns over the combined set.

**When it refuses — before any model call.** If the latest result is **not** an eligible
retryable INDETERMINATE — a PASS or BLOCK, a non-retryable indeterminate, or a
missing / legacy (no versioned journal) / corrupt / **stale** / digest-mismatched journal —
`--retry` **refuses up front, issues no model or provider call, emits no new sidecar**, and
exits **2** (the plan-review non-runnable code) with the normal full-review remedy on stderr:
run `rebar review-plan <id>` for a fresh full review. "Stale" means the latest sidecar no
longer reflects the present plan/code/registry (its recorded material fingerprint,
verified-at SHA, registry version, or reusable checkpoints no longer match) — the same
currency notion `--status` reports.

**Fresh budget, recorded lineage.** Each explicit `--retry` gets its own attempt budget; the
**cumulative** retry lineage (how much successive retries have spent) is recorded on the new
sidecar as audit telemetry and is **never** enforced as a cap. The surfaced result stays a
narrow end-result view — the same fields a normal verdict and `review-plan --status` expose;
the per-unit discovery journal is never printed.

**Flag interactions.** `--retry` is **mutually exclusive** with `--force`, `--status`, and
`--check` (combining them is a usage error, exit 2) and is **compatible** with `--no-sign`
(a recovered PASS can be reviewed without signing). It differs from `--status` (which only
*reads* currency and never runs a model) and from `sign-review` (which re-certifies an
existing attestation without re-running the review): `--retry` is the only path that issues
fresh model calls for just the missing units of the latest review.

**Containers inherit their children's declared scope.** File impact is tri-state: a ticket is
**undeclared** until it records a scope, **paths** when it records one or more `{path, reason}`
entries, and **none** when it explicitly declares that no repository files change. A
freshness check treats `undeclared` as unscoped and therefore binds it to the whole HEAD,
treats `paths` as dependency-scoped to the declared files, and treats an authenticated
`none` declaration as a scoped empty dependency set.

The container's signed dependency set is
its own `file_impact` ∪ the review's file citations ∪ the
union of its **direct** children's live `paths` scopes. Thus a live child with `none` is
neutral: it contributes no paths and does not make the container unscoped. A live child with an
**undeclared** scope is poison: inheritance is disabled rather than taking a partial union;
the P9 advisory names the offending children. **Closed children are ignored** — they neither
contribute nor poison, because later churn in their delivered files belongs to other tickets
(ADR 0024's completion floor).

For example, a container with one live child declaring `src/rebar/cli.py`, one live child set
to `none`, and one closed child declaring `docs/old-guide.md` inherits only
`src/rebar/cli.py`. Replace the live `none` child with an undeclared child and the whole
container becomes unscoped. This prevents an explicit no-file-change task from causing the
same false fail-closed result as a missing declaration.

Inheritance is one level deep by design: the container review pins each direct child's
**material fingerprint** (including its file-impact state, paths, and no-file-impact reason).
Changing any of those fields invalidates the container attestation and forces the union to be
recomputed — that self-healing invalidates the **claim** only under
`verify.enforce_plan_material_pins = true`, the recommended pairing (this project sets it).

**Currency rule.** A current plan-review certificate is a DSSE envelope that carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key and attributed to that environment through its principal. It remains current only when all of the following conditions hold:

- The certificate is cryptographically certified.
- The reviewed code has not drifted. Scoped reviews compare each signed dependency hash at the pinned SHA with a fresh hash from the current gate ref. A landed change to a reviewed file invalidates the certificate. An unrelated commit or an uncommitted working-tree edit does not. Unscoped reviews compare the whole HEAD.
- The bound material fingerprint matches the current ticket.
- The certificate postdates the latest reopen.
- Every reviewed related-material pin remains current.

Repository state and ticket content are independent staleness axes. Reopen state and related-material pins add separate guards. A change on any axis moves the verdict away from `certified`. The criteria-registry stamp is not a validity condition. ADR 0053 grandfathered a rotated `regver`, which appears as non-blocking `registry_drift` because a criteria edit changes neither the reviewed plan nor its code snapshot. Run `review-plan` again before implementation whenever `--status` is not `certified`. A moving base ref commonly requires this refresh in parallel epic and child workflows.

### `audit show` is a history view, not a status view

`rebar audit show <id>`'s `plan_reviews` array is the retained **`REVIEW_RESULT` sidecar
history** (every review that ran), **not** the current signed attestation. It is **newest-first**
and each entry carries a `reviewed_at` ns-epoch timestamp (so ordering is checkable from the
data — do **not** read `[-1]` as "current"; `[0]` is newest, and neither answers "is it approved
*now*"). The history is append-only — emitting a review never deletes an earlier one, so it is
bounded only by an explicit operator prune — and is a *different store* from the
`SIGNATURE` attestation the gate consumes, so a fresh review can be reflected here while the
current-approval question is still answered only by `--status` / `claim`. Use `audit show` to
see *what reviews happened*; use `review-plan --status` to ask *is it approved right now*.

### Phase/floor manifest contract

Compiled tickets carry `plan_review_phase: planning|execution`. A winning transition into
`in_progress` selects execution, a winning transition into `open` selects planning, and other
statuses preserve the projection. A signed manifest records the phase the review actually used,
not a later re-read: immediately after `advisory:` it contains `review-phase: planning`, or
`review-phase: execution` followed by `priority-floor: 0.80`. Legacy manifests with neither line
mean planning. Duplicate, partial, non-finite, out-of-range, or policy-inconsistent metadata is
malformed rather than normalized.

### `phase_status` compatibility

| Current compiled phase | Signed review phase | Result |
|---|---|---|
| planning | planning or legacy planning | compatible |
| planning | execution | incompatible |
| execution | planning or legacy planning | compatible |
| execution | execution with floor >= 0.80 | compatible |
| execution | execution with floor < 0.80 | incompatible |
| either | malformed phase/floor grammar | malformed |

`compute_validity` returns this as `health.phase_status` for every parsed plan-review result.
Completion-verifier validity does not parse plan-review phase, floor, or material pins.

The **CLOSE** profile uses this same table (it is not stricter on phase): a planning (or
legacy-planning) attestation certifies close for an execution-phase ticket. The close gate's
purpose is to catch a plan that *changed* during execution — enforced by own-material and pin
drift, which still invalidate at close — not to compel a fresh execution-phase review when the
plan is unchanged. The implementation itself is validated by the separate completion-verifier
gate. (An execution-phase ticket whose plan *did* drift invalidates on material/pin, forcing a
re-review, which — run while `in_progress` — is an execution review.)

### Why the execution floor is fixed at 0.80

Execution edits happen after implementation has begun, when accepting an under-specified change is
costlier than asking for another planning pass. Pass 3 therefore raises blocking-enabled criterion
thresholds to at least `0.80` for execution reviews. Advisory-only criteria remain advisory and
all findings remain visible. This protocol value is deliberately not configurable: raising it
later invalidates lower-floor execution attestations, while lowering it accepts higher-floor ones
without rewriting stored records.

### `PlanReviewGeneration` signing transaction

An immutable generation binds phase, priority floor, own material, the exact relation snapshot,
and its clean ticket-store revision. Full review captures it before the LLM; drift refresh and
re-sign capture it before their probe. Before signing, a stable fresh generation must equal that
initial value. Store-HEAD instability and under-lock mismatches retry at most three times; a stable
field change is terminal stale. The canonical event writer repeats the exact generation check
under its global write lock before renaming, staging, and committing the `SIGNATURE`, so failures,
lock timeouts, and model/provider errors leave no partial signature event.

### Phase rollback and precision loss

Disable claim/close enforcement first, then pause every `rebar compact` invocation during the
downgrade window. Existing phase-bearing snapshots are lossless because old reducers
preserve unknown snapshot fields. If old code instead fully re-reduces raw events after its own
cache bump, it drops the derived phase; a later forward upgrade bootstraps planning for `open` or
`idea` and execution for every other known status. Operators explicitly accept that historical
phase precision loss when compaction/re-signing was not paused. No stored signature is rewritten
merely because policy changes.

### Signing is the DEFAULT on a passing review

You do not ask `review-plan` for a signature — you ask it to *skip* one. On a non-blocking
`PASS` every public route signs automatically, because the attestation, not the printed
findings, is the review's durable product and the only thing the claim gate consumes:

| Route | Signs by default | Explicit opt-out |
|---|---|---|
| CLI `rebar review-plan <id>` | yes | `--no-sign` |
| Library `rebar.llm.review_plan(tid)` | yes (`sign: bool = True`) | `sign=False` |

An unsigned `PASS` is a deliberate act with a real consequence: the claim gate stays
unsatisfied and `rebar claim` still fails. If a genuine `PASS` was computed but its signature
failed to *persist*, do not re-run the review — `rebar sign-review <id>`
(library `rebar.llm.resign_plan_review`) re-signs from the recorded `REVIEW_RESULT` sidecar
with **no LLM call**, refusing if the plan changed since the review or the recorded verdict
was not a signable `PASS`.

### Interpreting the `signature` field

A durable `SIGNATURE` event whose manifest begins with `plan-review: PASS` is emitted only after a non-blocking `PASS` in which the LLM tier ran. A degraded run carries a `resolution_class` and cannot be signed because `attest.sign_plan_review` raises `SigningError`. `BLOCK`, `INDETERMINATE`, and exempt runners produce no `SIGNATURE` event. The exempt runner set contains `session_log`, `code_review`, and `identity`. A bug is reviewed under the bug tier and is not exempt from review. Without a durable certificate, the start-work gate denies the claim.

The `review_plan` verdict JSON always contains a `signature` object with a `signed` Boolean. A signed PASS uses `{signed: true, key_id, head_sha}`. Other outcomes use `{signed: false, reason: "<VERDICT>"}` or an error field when persistence fails. This object reports the signing attempt. It is not the durable certificate.

> Read `signature.signed`. Do not infer signing from the presence or truth value of the `signature` object. The durable proof of PASS is a certified `SIGNATURE` event containing a DSSE envelope that carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key and attributed to that environment through its principal. `rebar verify-signature <ticket>` performs this local certification. A `BLOCK` cannot produce that event.

## The idempotence short-circuit (skip the LLM when nothing changed)

`review-plan` runs a billable, multi-pass LLM review. Re-running it on a ticket that has **not
changed at all** — and already carries a still-valid plan-review attestation — is pure waste:
the result would be the same PASS and the same signature. So on the **signing path** the review
**short-circuits before any LLM call** when the ticket is fully unchanged: it computes the
current material fingerprint and asks the *same* validity oracle the claim gate consumes
(`claim_gate_check` -> `compute_validity`) whether a **certified** plan-review attestation still
binds that fingerprint, whose reviewed code has not drifted, whose criteria-registry stamp still
matches, and which post-dates any reopen. When that holds, it **reuses** the existing
attestation instead of re-reviewing.

- The skip fires **precisely when a `claim` would already pass**, so it can never weaken the
  gate — the attestation it reuses is the one already on the ticket (no re-sign, no new
  sidecar).
- The reused verdict is a well-formed `plan_review_verdict` with `verdict: PASS`,
  `coverage.llm_ran: false`, `coverage.idempotent_skip: true`, the current
  `material_fingerprint`, and `signature.signed: true` mirroring the live attestation. A
  concise log line (`plan review reused ... -- pass --force to re-run`) marks the skip, and
  `-o text` prints an explicit `reused: existing attestation is still current` line so a cache
  hit is never mistaken for a fresh review (the JSON carries `coverage.idempotent_skip`).
- It is ordered **before** the code-drift `drift_refresh` check below (a fully-valid
  attestation beats a needs-refresh one), and applies only when signing (a `--no-sign` /
  readonly review has no attestation to reuse).
- **`--force`** (CLI `rebar review-plan --force`, library/MCP `force=True`) bypasses **both**
  the idempotence skip and the drift-refresh, forcing a full multi-pass re-review. Any real
  change to the ticket (a material edit, code drift, a registry change, a reopen) already
  defeats the skip on its own; `--force` is the manual override for an otherwise-unchanged
  ticket.

## The convergent remediation re-review (rising floor)

A re-review of an **edited** plan used to be at risk of not converging: each remediation
round could surface *new*, lower-stakes findings in previously-clean criteria, expanding
scope every run and never going green. The **rising-floor remediation re-review** (epic
`7d43`; ADR [0008](adr/0072-convergent-plan-edit-re-review.md)) makes it converge while
preserving full recall.

It runs the **full criteria set every time** (no skipping, no Pass-1 anchoring → high-stakes
defects an edit introduces are still found), and applies a **deterministic Pass-3 floor**
that drops only **novel, low-priority** findings:

> A finding is dropped **iff** `novelty ≥ T_novel` **and** `priority < floor`
> (`priority = validity × impact`).

- **Carryover findings** (low novelty — they match a prior-review finding) are enforced at
  the normal threshold and must still be resolved.
- **Novel high-priority findings** are preserved (and may block) — nothing high-stakes is
  ever frozen.

**Novelty is scored in a SEPARATE Pass-2 sub-call** (its own `novelty` contract + the
`plan-review-novelty` prompt) that ALONE receives the prior findings (read from the
`REVIEW_RESULT` sidecar) and answers factual *matches-prior* sub-answers; `novelty = 1 −
mean(matches-prior)`. The verification sub-call (severity + validity) and Pass-1 receive NO
prior findings, so the independence invariant holds **by construction**. A failed/malformed
novelty sub-call defaults novelty to `0.0` (carryover → never dropped — a broken signal can
only make the gate stricter).

**Remediation mode is always-on and unconditional.** The floor applies only
when ALL hold: the plan changed; the **code is unchanged** since the baseline
(detected by `verified_at_sha` equality against the prior signed manifest — reusing the
signed snapshot ref, no new diff machinery); the registry is unchanged; a prior sidecar with
finding text exists; and the last review of any kind is within the freshness window
(default 60 min, measured from the last review and **reset on each review**, so the loop
persists across a series of edits and lapses to a normal full review only after the agent
goes idle). Any precondition failing → a **byte-identical full review**. The
**evidence gate** completes the triple gate and is likewise always-on.

> **Two eligibility baselines — signature first, sidecar fallback (story a850).** When a
> valid **certified PASS** signature exists, it supplies the material/SHA/registry
> reference points exactly as originally designed (`baseline: "signature"`, byte-identical
> decision shape). When none exists — the BLOCK-loop regime, since a BLOCK never signs —
> eligibility falls back to the prior `REVIEW_RESULT` payload (`baseline: "sidecar"`),
> which since story a850 stamps `material_fingerprint` + `verified_at_sha` + `regver` on
> every verdict (PASS and BLOCK alike; in local-mode reads the SHA falls back to the
> committed git HEAD so local BLOCK loops still qualify). The sidecar branch's reasons are
> exactly `{sidecar_baseline, plan_changed, code_unchanged, registry_unchanged,
> within_window}`; a pre-a850 sidecar without the stamps simply fails `sidecar_baseline`
> (fail-safe). Field motivation (2026-07-14): 287/382 post-flip reviews were ineligible,
> 240 of them only because no certified signature existed. Field evidence (782
post-recalibration runs: 32% verdict instability on byte-identical plans, 95% of remediation
edits minting new findings) motivated making both unconditional; the `discriminates_novelty`
eval (`rebar prompt eval plan-review-novelty`) remains available to re-run.

A narrowed verdict records `narrowed: true` + `floored_criteria` + `floored_finding_ids` on
its `coverage`, and the dropped novel findings are written to the `REVIEW_RESULT` sidecar
(joinable by `norm_id`) — so within-session suppression is always **observable**. This is
the *complement* of the code-drift `drift_refresh` path (ADR 0002): drift-refresh is plan
**unchanged** + code drifted; remediation is plan **changed** + code unchanged. Both the
remediation floor and its evidence gate are always-on and unconditional.

## Completion-aware container review (the completion floor)

When a **container** ticket (an epic, or a story with children) is re-reviewed after some of its
children are already **delivered**, a full re-review re-litigates the *settled* acceptance text of
that done work — raising throw-away findings about the wording/scope of an AC a closed, verified
child already satisfies. The **completion floor** (ADR 0024) drops exactly those, and nothing live.

A finding is dropped **iff all** hold: it is attributed to a **delivered-now** child, is
**limited-to-closed** work (not spanning an open sibling / the system), is about **plan-semantics**
(scope/clarity/sizing — not the delivered mechanism/contract), its `priority` < the floor, and its
criterion is **not** in the always-preserve set. A **delivered-now** child is one that is `closed`
with a **valid completion-verifier attestation** (ADR 0009 — so a *force-closed* child does **not**
qualify) **or** is superseded by a **live in-epic sibling**. Every ambiguous sub-answer fails toward
**KEEP**, so security (`T5c`), contract (`T10`), delivered-functionality, and spanning findings are
always surfaced.

The three sub-answers (`attribution` / `containment` / `layer`) come from a separate Pass-2
sub-call (`plan_review_completion`); the drop is deterministic in Pass-3 (no LLM). A completion drop
is recorded on `coverage` (`completion_floored_criteria` / `completion_floored_finding_ids`) and in
the `REVIEW_RESULT` sidecar with `drop_reason: "completion"` (vs `"novelty"` / `null`) — the offline
join key. This is the third of three deterministic Pass-3 floors — **novelty** (plan-edit
convergence, above), **material freshness** (drift-refresh, ADR 0002 — default ON since 2026-07-12),
and **delivered-completion** (this) — each along an independent axis, firing only when its own
staleness/completion condition is met.

## Configuration

| Key | Default | Effect |
|-----|---------|--------|
| `verify.require_plan_review_for_claim` | `false` | When true, starting work on a work ticket (`claim`, or `transition open→in_progress`) requires a fresh certified plan-review attestation. **Turning it off is the rollback** — an ordinary preference, no kill-switch needed. |
| `verify.remediation_window_minutes` | `60` | Freshness window for the (always-on) rising-floor remediation re-review: a re-review is eligible only if the last review of any kind was within this many minutes (measured from it, reset on each review). |
| `verify.novelty_drop_threshold` | `0.7` | `T_novel`: a finding is droppable only if its novelty ≥ this. |
| `verify.novelty_priority_floor` | `0.4` | The rising floor: drop a novel finding only if its priority < this (a scalar ≈ the corpus p40 impact percentile; `scripts/plan_review_impact_distribution.py` prints the inputs). The evidence gate that arms both this floor and the code-review region-gated floor is always-on and unconditional. |
| `verify.completion_floor_active` | `false` | **Evidence gate** for the completion floor (container completion-awareness, above). Off/absent → byte-identical full review (the back-out); flip true only after the calibration gold-set clears its must-never-suppress bar. |
| `verify.completion_priority_floor` | `0.4` | The completion floor: drop a delivered, plan-semantics finding only if its priority < this (same "below major" band as the novelty floor). |
| `verify.completion_preserve_criteria` | `["T5c","T10"]` | Always-preserve criterion ids the completion floor never drops (security overlay + endpoint/interface contract). Accepts a TOML array or a comma-separated string; add privacy/compliance ids here — a config change, not code. |

Enable it in a `[verify]` table in `rebar.toml` / `pyproject.toml`:

```toml
# rebar.toml or [tool.rebar.verify] in pyproject.toml
[verify]
require_plan_review_for_claim = true
```

Default **off** ⇒ `claim` keeps today's behavior exactly. An unreadable config is an
**error**: the gated operation fails loudly with the parse fault (operator ruling
39f8-ae7c) — it never silently resolves the gate to its default, and it never
auto-enables across a repo.

## Close-time attestation gate

`verify.require_plan_review_for_close = false` is the default. When enabled, a normal
close of a `task`, `story`, or `epic` must have a separately certified plan-review
attestation. Close only performs local signature and attestation-validity reads; it never
starts `review-plan`, invokes an LLM, or contacts the network. Bugs and lifecycle artifacts
keep their existing exemptions.

The close check uses `PlanValidityProfile.CLOSE`. It requires a valid signature, current
registry, current ticket material, compatible review phase, and (when
`verify.enforce_plan_material_pins = true`) healthy related-material pins. Unlike the claim
profile, CLOSE deliberately does **not** reject ordinary implementation HEAD or reviewed-file
drift: those changes are expected while the approved plan is being executed. Pin enforcement
remains independently opt-in; a disabled pin policy leaves pin health advisory, but malformed
phase metadata, signature failures, reopen, registry drift, and own-material drift still block.

The close path first enforces the unresolved-child structural invariant, then runs this local
check before completion verification. It supplies the same check through the generic
`txn.transition_core(..., pre_status_check=...)` callback. That callback runs under the existing
write lock after the ticket is freshly reduced and transition guards have passed, immediately
before the STATUS append. A changed signature, plan, phase, material, or enforced pin between
the precheck and append therefore aborts the transaction without writing STATUS.

Failures are deliberately remediated out of band and use this stable form:

```
plan-review close gate: <verdict>: <reason>. Run rebar review-plan <canonical-id> separately, then retry close.
```

Typical verdicts include `unsigned`, `stale-reopened`, `stale-material`,
`unverifiable-material`, `stale-pin-drift`, `stale-pin-missing`, `malformed-pin`,
`incompatible-phase`, `malformed-phase`, and `unavailable`. An unexpected local read, parser,
or signature failure is fail-closed as `unavailable` and emits the structured warning event
`plan_review_close_gate_unavailable`.

A non-empty `--force` reason bypasses this and completion verification while retaining its
audit comment. Closing `idea → closed` also bypasses the plan gate because it is a rejection,
not delivery. Neither bypass relaxes the structural child-closure invariant.

### Link-backed administrative dispositions — the `disposition` verdict

A close carrying `--class=superseded` or `--class=duplicate` **against a live replacement
link** skips the attestation requirement and reports the distinct verdict `disposition`.
The gate exists to certify work about to be done, or just done; neither applies to a ticket
whose work landed elsewhere and which is being closed as a bookkeeping act. Worse, the
requirement was unsatisfiable by construction: `review-plan` correctly BLOCKs a plan whose
edits already exist, so the more truly superseded a ticket was the more certainly it could
never earn the attestation, leaving `--force` — which records no signature — as the only exit.

Two conditions must BOTH hold, and the exemption is deliberately narrow:

- the class is **link-backed** administrative vocabulary — only `superseded` and
  `duplicate`. `obsolete` and `wontfix` are **reason**-required, justified by operator prose
  no gate can verify, so they still require the attestation;
- the claimed replacement link is **live**, checked with the very predicate the
  completion-verification close gate already uses (`close_precheck._has_live_replacement_link`),
  so the two gates cannot drift on what counts as evidence. No live link ⇒ no exemption.

`disposition` is a separate verdict from the ticket-type `exempt` precisely so the audit
trail distinguishes an evidence-backed administrative close from a type exemption and from
an unaudited `--force` bypass.

### AC-checkbox completeness precheck (deterministic, pre-LLM)

Before the completion verifier runs, the close gate performs a deterministic check: if the
ticket's `## Acceptance Criteria` section contains any unchecked `- [ ]` item, the close
fails immediately (exit 1) **without making any LLM call**. Items whose text begins with the
`[operator-attested]` tag (ADR-0043) are exempt — the shared matcher `_OPERATOR_ATTESTED_TAG_RE`
from `det_operator_attested.py` is reused so the two surfaces cannot drift. To override when
a gate-level bypass is warranted, pass `--force="<reason>"`. Checking boxes to satisfy
this precheck is attestation-safe: checkbox state is normalized out of the material
fingerprint (330c; the single normalization seam covers both the plan-review claim gate and
the completion-verifier staleness check), so the flips do not invalidate a signed plan review.

### Attested-item validity precheck (deterministic, pre-LLM)

The tag that earns the exemption above is itself validated by a second deterministic
precheck (bug `2f56-313f-6175-41b1`). The completion verifier classifies criteria **solely
from the author tag** (ADR-0043, by design), so a mistagged criterion would launder
repository-verifiable work past verification — the verifier accepts a ticket comment where an
exact path/symbol check was possible. Before any LLM call, the close now fails (exit 1) when
an `[operator-attested]` AC item:

1. **cites exact repo path/symbol evidence** in its own text or indented continuation lines.
   Test artifacts always fire (`tests/unit/test_x.py`, `pkg/mod.py::test_y`, bare `test_*`
   symbols — a test is inherently completion proof); other repo paths (`src/…`, `docs/…`,
   slash paths with a code extension) fire only when an evidence-introducing word (proxy,
   proof, evidence, verified, covered, documented, …) presents them as the proof — a path
   mentioned in plain prose for *orientation* ("the fix shipped in `src/…` is deployed")
   never blocks a legitimately-external AC. URLs are scrubbed first, and commit hashes /
   Gerrit change numbers deliberately do *not* fire: they are the attestation-event
   provenance ADR-0043's contract demands. The remedy is to **untag** — the completion
   verifier can check the repository — or, for a criterion that *mixes* repository and
   external evidence, **split**: the cited paths/symbols move to a new untagged criterion
   and the external outcome keeps the tag with its provenance line; or
2. **lacks its complete `provenance:` continuation line** (ADR-0043 × ADR-0016) — the same
   detector the advisory review-side P6 lint uses (`det_measurement_provenance`), promoted
   to blocking on the close path.

The laundering detector is `det_attestation_launder.py` (pure `re`, precision-first); the
guard is `txn.ensure_attested_items_valid`, wired into `_completion_precheck` immediately
after the checkbox precheck. `--force="<reason>"` bypasses it like every close precheck, and
legitimately external items (deploy/vote/console evidence **with** a complete provenance
line) close exactly as before. Known interaction (f680): the untag/split/provenance remedy
edits the description, which stales a signed plan-review attestation as a material change —
the block message says so, so the re-review cycle is expected rather than a surprise.

## The Gerrit bugfix-size attestation gate (code review, not claim/close)

A third consumer of the plan-review attestation lives at code-review time, on Gerrit only:
`rebar.llm.code_review.bugfix_size_gate`. A change whose `rebar-ticket:` trailer names a **bug**
and whose diff exceeds **150 non-test lines** must carry an acceptable plan-review attestation on
that bug, or the review bot casts `LLM-Review -1` with the `bugfix-size-attestation` criterion. A
fix that large is a design change wearing a bug label — the "drive-by rewrite" mode this project's
bug-trend analysis surfaced. Only Gerrit reviews run it; a local `rebar review-code` preview never
blocks on it, and test-only diffs and non-bug tickets are exempt.

### The second escalation signal: a **repeat-fix** (ticket `1dd5`)

Size is not the only shape of "a design change wearing a bug label". A *small* fix to a file the
base branch has **already bug-fixed twice in the last 7 days** is the other one, and it is the
better predictor: backtested over `origin/main` and labelled from the store's own `caused_by`
links ("this fix's ticket was later named as the cause of another bug"), the repeat-fix signal
recalls more later-culprit fixes than the 150-line floor does.

So the gate escalates a bug fix when **either** signal fires:

| signal | fires when | parameters |
|---|---|---|
| `size` | the diff exceeds 150 non-test lines | `BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES` |
| `repeat-fix` | some non-test file it touches was touched by >= 2 other bug-fix commits on the base branch in the prior 7 days | `REPEAT_FIX_WINDOW_DAYS`, `REPEAT_FIX_MIN_PRIOR` in `rebar.llm.code_review.repeat_fix` |

The remedy is identical either way — the coverage record's `escalation_reason` (`size`,
`repeat-fix`, or `size+repeat-fix`) and the blocking finding say which fired, and the finding
names the prior fixes so the evidence is checkable. The predicate needs nothing but git history
plus the ticket type: no path allowlist, no CI provider, so it runs in any environment. It is
**fail-open** — history it cannot read is never an escalation.

Reproduce the comparison from any checkout:

```sh
python scripts/backtest_bugfix_size.py --rev-range origin/main --repeat-fix --labels-from-caused-by
```

That script imports the shipped predicate rather than reimplementing it, so the measurement
cannot drift from the gate. It keeps `flagged` meaning the size floor **alone**; the repeat-fix
verdict is a separate field, which is what lets `--check-planning-corpus` keep pinning the
original adjudication.

**`review-plan` is deliberately NOT mirrored.** The obvious symmetry — teach the claim-time
plan-review escalation about repeat fixes too — is a no-op and should not be re-proposed.
`orchestrator.bug_blast_radius_escalates` already escalates a bug whose `file_impact` names
**any** non-test path, and every repeat-fix bug names one by construction. The only bugs a mirror
could newly escalate are those with empty or test-only `file_impact`, where a repeat-fix
predicate has no path to walk and so cannot fire either.

**The remedy, executable end to end from any developer environment:**

```sh
rebar review-plan <id> --status        # is there a current attestation? (read-only, no LLM)
# not current → write the fix plan into the ticket's description, declare its file impact, then:
rebar review-plan <id>                 # runs the review and SIGNS an attestation on a PASS
rebar sign-review <id>                 # only if the review PASSED but no attestation landed
git commit --amend --no-edit && git push gerrit HEAD:refs/for/main
```

**The gate asks whether an attested plan review was COMPLETED — never which environment certified
it** (current policy, bug `846b`). It does not consult `.rebar/trusted_environments.yaml`. That
distinction is what makes the remedy above executable: a review run on your own machine signs with
*your* environment id as the DSSE principal, and a contributor cannot pin their own environment
(the pin file is CODEOWNERS-protected on the code branch), so a source-gated check rejected
genuinely passing, genuinely signed reviews purely on provenance and could not be satisfied by
anyone. Lifecycle and freshness still bind normally: `certified` / `stale-code` / `stale-head` are
accepted (the plan *was* reviewed; the trunk moved on), while `stale-material` — you attested and
then edited the plan — still blocks, so the gate is not bypassable by editing after signing.

**Subject binding is still enforced**, because it asks *what* the attestation covers rather than
*who* signed it: a cert bound to another ticket, or of another kind, classifies `wrong-kind` and
blocks — read from the SIGNED payload, so a lying plaintext mirror on the record buys nothing.

**The security posture, stated plainly.** This argument once read: "`.rebar/trusted_environments.yaml`
is the only trust root, so an unpinned principal has no key to verify against — there is no coherent
middle where signatures are checked but unpinned signers are waved through." That names a real
trade-off, but it is **no longer current policy** and it was never quite right about the mechanics:
an SSHSIG blob carries the public half it was made with, so an unpinned signer's signature *can* be
checked for self-consistency — full signature, namespace and principal binding — just not tied to a
*known* environment. Bug `c21f-6f29-5d2d-4a5a` took the middle deliberately, on the operator's ruling that *"any
certification is as good as any other certification right now — limited to a trusted set of
environments is a future feature, but not currently in use."* The
verifier reports which key certified in `trust_basis`, so the weaker `envelope_key` basis is visible
rather than silent, and restricting the trusted set again is the opt-in `verify.require_environment`
feature. The conclusion below is unchanged.

Dropping provenance makes this criterion an **anti-sloppiness gate**
("did you write and review a plan before shipping 150+ non-test lines under a bug label?"), not an
anti-adversary one: the attestation record lives on the auto-pushed, non-Gerrit-gated tickets
branch, which `opcert.opcert_from_record` documents as attacker-writable. That is acceptable
because this gate was never the adversarial boundary — landing on `main` still needs `LLM-Review
+1` **and** `Verified +1`, both cast only by bots/admins, and the code branch (where the pin file
and the rubric live) stays Gerrit-gated. Nothing here widens who may vote, so a change still cannot
self-approve. Restoring provenance gating is re-adding the keyring lookup and the `verify_opcert`
call to `classify_plan_review_attestation`; the bucket partition and the fail-open default are
unchanged, so nothing else would have to move.

Store trouble and any unrecognized future verdict degrade to an **advisory**, never a block. The
same classifier backs the self-service `rerun-llm-review` trigger, which stays fail-closed: anything
other than an accepted attestation refuses the re-review.

### The epic-close bug screen (three stages, epic closes only)

Agents file bugs OUTSIDE an epic's hierarchy during epic execution and deem them
out-of-scope/pre-existing even when they are defects in the epic's own deliverable; the
direct-children invariant cannot see them (backtest over 56 epic closes: 2 real at-close
escapes). Closing an **epic** therefore adds three stages, cheapest-first (ticket 4b54):

1. **Deterministic `caused_by` floor** (hard tier, no LLM). Any open/in_progress bug carrying
   a `caused_by` link into the epic's subtree (the epic or any descendant, any depth) blocks
   the close exactly like an unclosed direct child — the bug RECORDS that this work broke it.
   The verdict teaches the three exits: fix (close) the bug, re-parent it under the epic, or
   dispute the `caused_by` link.
2. **Deterministic candidate filter.** Candidates for the screen are open/in_progress
   **bugs** outside the subtree that were created after the epic's FIRST `open →
   in_progress` transition (fallback: the epic's creation time when it was never claimed)
   OR are linked — any relation, either direction — to any subtree member. At most 32 are
   evaluated per close (linked first, then newest); any remainder is recorded as an
   unevaluated-overflow count, never silently dropped.
3. **LLM relevance screen + verifier adjudication.** Each candidate gets one single-turn
   trivial-class call (`epic_bug_screen` prompt → `epic_bug_screen_verdict`): forced choice
   **A** (defect in something this epic changed/built or behavior its AC claims) / **B**
   (same subsystem, pre-existing or adjacent) / **C** (unrelated) plus a one-line citation.
   A-verdicts are forwarded to the completion verifier as a compact block (≤8 rows of
   title + citation + id) inside the fenced context; the verifier retrieves detail via
   `show_ticket` and **blocks only a defect-in-deliverable with NO recorded disposition** —
   a disposition satisfies via (a) a `supersedes`/`duplicates` link (either direction) to
   the subtree or a named successor, or (b) a REASONED pre-existence/deferral/supersession
   assertion in the bug's description or comments (a bare "out of scope" does not qualify).
   Prose-only supersession is deliberately handled by (b): agents routinely record
   supersession in comments without linking, and blocking those would teach agents to stop
   linking.

The screen **degrades open**: any failure (model down, malformed output — normalized to the
non-surfacing `C` — or a store read error) logs and skips; only the deterministic floor is a
hard tier, and `--force` remains the operator escape hatch. The full per-bug tally +
overflow count lands on the completion sidecar (`epic_bug_screen_v1`) for audit and live
false-negative calibration.

### Which commit the completion gate verifies — `--ref`

When `verify.require_completion_verification_for_close = true`, the completion-verification close
gate verifies an **immutable snapshot of the committed tree at your worktree HEAD** by default,
and — on PASS — signs a `verified-at-sha` attestation bound to that commit.

`--ref` pins the **code** only. The verifier reads the ticket itself from a *separately* pinned
copy of the ticket store, taken from the live store when the run starts — see
[repo-snapshot-gates.md](repo-snapshot-gates.md) §"The TICKET store is pinned separately from the
code". That is why a finding reporting ticket evidence as missing means "not visible in the
snapshot I read", not "does not exist": if you recorded the evidence after the run began, or the
write has not committed, re-verify rather than recording it again.

#### Experimental lazy ticket view and atomic close bundle

`verify.completion_pinned_ticket_view = true` replaces the full ticket-tree copy for eligible
completion runs with a demand reader at one immutable `tickets_oid`. Eligibility is fixed before
capture: the run must be attested, non-epic, and use `sync.push = "always"`. Epics and the
`async`/`off` push policies stay on the materialized path. The verdict and durable sidecar record
the choice in `ticket_read_mode`; disabling the key is the back-out.

The code and ticket pins remain separate:

```text
--ref ──> code_oid ──> immutable code tools
tracker HEAD ──> tickets_oid ──> lazy ticket tools + deterministic ticket reads
                                └─> read receipt ──> descendant validation before close
```

The receipt binds exact demanded ticket states, positive and negative reference resolution,
field-only deterministic reads, direct children, transitive descendants, inbound and outbound
links, and consulted relation reachability. It also carries lazy-view and reducer schema versions,
so a process cannot replay old predicates under changed reduction semantics. A later ticket request
loads from the original ticket OID and adds to that receipt.
Before mutation, rebar requires current tracker history to descend from `tickets_oid` and repeats
each recorded predicate. Unrelated ticket events can pass; relevant drift and any replay failure
raise an exit-10 concurrency conflict and require a new completion run.

The close caller captures that OID before its completion-specific checkbox, operator-attestation,
descendant, file-impact, attached-commit, and commit-reference checks. It passes the same view
object through every bounded verifier auto-resume. Criterion banks and cross-run verdict-cache
entries are stamped with `tickets_oid`, so an attempt cannot reuse evidence from another tracker
revision. Parent-bounded listing and ticket display are modeled; any other in-process ticket read
fails closed as unsupported instead of silently consulting the live store. Resolver support is
materialized only for the demanded reference, including every competitor needed to preserve
ambiguous alias/short-id behavior, so read order cannot change reduction results.

For a fresh certifiable PASS, close publication is one Git commit containing exactly a PASS
`COMPLETION_VERDICT`, `STATUS in_progress -> closed`, and completion `SIGNATURE`, all bound to the
same `(run_id, code_oid, tickets_oid, receipt_digest)`. Certificate preparation stays outside the
write lock. Final tracker/status checks, HLC ordering, authorship, staging, and the single commit
are inside it, but the commit is built in a cheap object-sharing sparse candidate repository whose
`HEAD` and index are not the shared tracker's. A failure or relevant remote rejection discards that
candidate and leaves no close for a later generic tracker push to leak. Retry of the same basis is
idempotent when an equivalent signed bundle already exists.

Lazy-view metrics live in the completion result's `metrics` object. The `atomic_close`
command-result object reports `delivery`, `commit_oid`, `receipt_digest`, and transaction/delivery
metrics:

| Object | Metric | Boundary |
|---|---|---|
| completion `metrics` | `tickets_oid_capture_ms` | Capture the immutable ticket-store commit |
| close phase metrics | `verifier_ticket_view_setup_ms` | Select/capture the lazy view or attach the materialized fallback |
| completion `metrics` | `ticket_object_list_ms` / `ticket_object_read_ms` / `ticket_object_reads` | Lazy Git tree and object access |
| completion `metrics` | `ticket_reduction_ms` | Uncached reduction of demanded tickets |
| `atomic_close` | `atomic_close_receipt_validation_ms` | All pre-publication receipt replays |
| `atomic_close` | `atomic_close_delivery_receipt_validation_ms` | Receipt replay during local/remote delivery reconciliation |
| `atomic_close` | `atomic_close_prepare_ms` | Sidecar and completion-certificate preparation |
| `atomic_close` | `atomic_close_candidate_prepare_ms` | Object-sharing sparse candidate setup outside the lock |
| `atomic_close` | `atomic_close_lock_wait_ms` / `atomic_close_lock_hold_ms` | Local store-lock queue and critical section |
| `atomic_close` | `atomic_close_commit_ms` / `atomic_close_events` | One three-event private candidate commit |
| `atomic_close` | `atomic_close_push_ms` / `atomic_close_push_attempts` / `atomic_close_merges` | Post-lock delivery and bounded reconciliation |

After a successful push acknowledgement, rebar fetches the branch through a run-unique private ref
and proves that the fetched tip still contains the candidate commit before reporting publication.
An immediate remote rewrite therefore fails closed rather than being mislabeled as a durable close.

The private candidate commit is the all-or-none bundle; publication remains an ordinary Git ref
update rather than a distributed transaction. Candidate imports and fetched remote tips use
UUID-private refs. The first rollout revalidates a fetched remote OID before merging an unrelated
non-fast-forward delta and never performs a force update. A relevant independent-clone change
raises an exit-10 conflict and the candidate is deleted without moving shared `HEAD`. If a push
acknowledgement is lost after acceptance, a reachability fetch reports
`pushed_after_ambiguous_ack` and does not publish another bundle. If the following local
fetch/merge fails, or a concurrent local commit leaves shared `HEAD` ahead of the fetched remote,
`pushed_local_pending` means the remote close is durable and only local delivery remains.

The tracker lock fixes final receipt/status checks and HLC order; the later Git ref update is the
visibility boundary. It is intentionally not a lease over independent writers. A later ticket
event is ordered after the bundle and may make the certificate stale through validity-on-read, but
cannot make the earlier three-event commit partial. See
[concurrency.md](concurrency.md#receipt-aware-completion-delivery-experimental) for the mechanics.

**"No verdict obtainable" is a fault, not a verdict.** If the verifier returns a failure naming no
criterion — a truncated or garbled structured turn — the gate no longer invents an `(unspecified)`
criterion for it. It reports the run as a verifier fault and exits **11** (transient — retry). The
close is still refused, but the right response is to re-run the close, not to hunt for a
requirement that was never evaluated.

`rebar transition <id> closed --ref <commit>` (library: `transition(..., ref=<commit>)`) targets a
**specific commit** instead of HEAD: the gate verifies, and signs against, that ref's tree. The
pre-sign drift guard resolves the **same** ref for its fresh-SHA read, so a fixed commit — whose
tree is immutable — is a stable no-op rather than being spuriously treated as drift. The close
therefore lands **signed** even though HEAD is elsewhere. Absent `--ref`, behavior is unchanged
(verify at HEAD, drift-check against `head_sha`).

**Stacked-epic recipe.** When landing a stack where each story is its own commit, close each story
against **its own commit** — `rebar transition <story-id> closed --ref <story-sha>` (or check that
commit out) — while your worktree stays at the epic tip. Each story's scope acceptance-criteria are
then evaluated against just that story's tree, not the cumulative tip, and each close still signs a
certified per-story completion attestation.

### When the acceptance criteria no longer match reality

Sometimes the close gate blocks because the ticket's criteria have gone out of date — later
work moved a file they name, or the fix itself made them describe something that no longer
exists. **Edit the ticket so its criteria are accurate, re-run `rebar review-plan <id>` to
re-pass the plan gate, then close against the corrected criteria.**

Do not reach for `--force`, and do not point `--ref` at an older commit where the stale
criteria still happened to hold. Both can produce a close, but both leave **inaccurate state**
in the ticket system: the ticket goes on asserting something untrue of the codebase, and the
ticket store is the shared, durable record every other agent reads. A close that passed only
because it was measured against an old tree is a close whose criteria still lie.

(`--ref` is for the stacked-epic case above — evaluating a story against *its own* commit,
where the criteria are correct and the tree is simply not HEAD. That is a different problem
from criteria that are wrong.)

## Fail-open behavior

* **Unsupported stack / missing tool / parse error / timeout** in any DET check →
  the check `abstain`s (records a reason) and is treated as PASS. The recorded
  abstain set *is* the coverage.
* **LLM unavailable** (missing `[agents]` extra / no API key) → `review_plan`
  degrades to a **DET-only** review (the floor still blocks on P1/P5/P8/P10/P11; advisory
  LLM findings are simply absent). The error is recorded in coverage.
* **A broken individual check** abstains rather than aborting the floor.
* **Pass-2 verify failed but Pass-1 ran** (e.g. the agentic verifier exhausted its step
  budget on a finding-rich ticket — bug `59bc`): the Pass-1 findings are **preserved**
  (un-verified → INDETERMINATE) rather than discarded, and `coverage.verify_failed` is
  set (distinct from `llm_unavailable`). The verdict fails **open** (PASS) unless a
  preserved finding sits on a blocking-enabled criterion — then it can't rule out a real
  block, so it is INDETERMINATE (fail-closed). The agentic verifier's step budget also
  scales with the finding count (`step_budget_per_item`), so the failure is rare; the
  per-step request usage is recorded on `coverage.metrics.verify_requests` for headroom
  observability.
* The **claim gate** itself fails *closed* when enabled and the signing subsystem
  is unavailable (a missing key blocks the claim, consistent with the close gate);
  `--force` is the escape.

## Gate liveness and supervisor stall budgets

`review-plan`, `verify-completion`, and completion-gated closes routinely span
multiple model/tool requests, so their total wall clock can exceed a common 600s
supervisor "no output" budget. rebar emits lightweight keepalive log lines between
LLM/tool/workflow calls at the default WARNING level to make multi-call progress
visible, but a single very long request can still be silent until that request returns.
The LLM `timeout` default (`DEFAULT_TIMEOUT_S = 600`) is per request, not a total gate
runtime, and close can issue many requests; raise the supervising harness's no-output
budget for gate commands rather than treating 600s as the whole-operation ceiling.

## The `REVIEW_RESULT` observability sidecar

Every review emits a `REVIEW_RESULT` event (`sidecar.py`) capturing per-finding
fingerprints + decisions + verification attributes + coverage. It is a
**reducer-ignored** sidecar: not in `KNOWN_EVENT_TYPES` (so it never enters
compiled state, deps, validate, or the hot paths, and compaction preserves it),
but in the write allow-list and in `_NON_REPLAY_KNOWN_TYPES` (so `fsck` recognises
it and does not warn). Offline replay joins on `ticket_id` + finding `id` to
reconstruct per-criterion false-positive / remediation rates — capture only, no
in-session analysis, no human-feedback requirement.

Each sidecar finding also carries a **`cohort`** (epic cite-stone-sea / WS9): the sorted set of
criterion ids that were **co-resident** in the finder call that produced it — the sorted chunk
ids for a `pass1_chunk` finding, the container criteria ids for a `pass1_container` finding, and
the singleton `["ISF"]` for the ISF path (which is never co-resident). It is the offline
calibration key for chunk-contamination analysis (R-1): how often a blocking-tier finding came
from a chunk where other criteria were co-resident rather than being reviewed in isolation
(`scripts/plan_review_contamination_rate.py`). A finding written before WS9 — or by a path that
does not stamp it — has **no `cohort` key**; offline analysis MUST treat a **missing `cohort` as
"unknown"** (skip it), never as an empty/isolated set.

## Blocking-FP-proxy demotion (dogfood alarm)

The gate's own false-positive drift is watched offline by the R7 gate-eval instrumentation
(`docs/experiments/plan-review-gate/harnesses/gate_eval_instrumentation.py`, epic 6982). It
joins each reviewed ticket's outcome (post-claim edits, reopens, force-closes) to that ticket's
persisted `REVIEW_RESULT` sidecar findings and emits, per criterion, a trailing
**`blocking_fp_proxy`** = the fraction of that criterion's blocking findings whose ticket was
subsequently force-closed or reopened (a conservative lower-bound proxy for "adjudicated
not-block-worthy"; force-close can also happen for orthogonal operator-attestation reasons, so
this under-counts rather than over-counts).

**Alarm rule.** When a criterion's trailing `blocking_fp_proxy` **exceeds 10%** (the Tricorder
trust cliff), a human reviews that criterion's recent blocks and, if they are genuinely
not-block-worthy, **demotes it from blocking to advisory** via an ordinary
`criteria_routing.json` change (`default_posture: "advisory"`). This is dogfood monitoring, not
an automatic action — the >10% figure triggers review, not an auto-demote.

**Precedent.** The demotion-on-FP-evidence precedent is **T5e**, demoted to advisory in
calibration-3 (task relishable-ammonitic-hoverfly). (T3/T10 are a *different* precedent —
criteria kept non-blocking from the start; they were never demoted, so they are not the
demotion precedent.) The companion coaching lever for advisory *latency* (as opposed to
blocking FPs) is the R6 [Advisory triage](#advisory-triage-apply-now-vs-defer) stage.

## Standing per-criterion effectiveness recorder + advisory→blocking promotion gate

R7's job above joins each ticket to the **outcome corpus** (post-claim edits / reopens /
force-closes), which is mined from the `tickets` branch's git objects — a periodic batch job. The
**standing per-criterion effectiveness recorder**
(`docs/experiments/plan-review-gate/harnesses/criterion_effectiveness.py`, epic 6982) is a
*complementary, not duplicate* dogfood job whose signal source is the sidecar **re-review history
alone** — no outcome corpus, no git-object walk — so it accumulates at **zero marginal LLM cost**.
It reads the persisted `REVIEW_RESULT` sidecars and appends one lean **firing row per
(review-round, finding)** into an append-only, **prune-immune** ledger `runs/criterion_firings.jsonl`
(prune-immune because the ledger captures firings as they happen, so it survives an operator
running the sidecar's retention prune down to `RETAIN_PER_TICKET=50` rounds/ticket). `--record --backfill` seeds it from
the whole corpus; the default `--record` is incremental (append past the ledger watermark, idempotent
by `(ticket, review_ts, round_uuid, norm_id)`); `--report` writes `runs/criterion_effectiveness.json`.

It **auto-includes every criterion id it sees** — so a newly-shipped advisory criterion (e.g.
R1/R3/R4's) is monitored with **no per-criterion wiring**. Per criterion, over a trailing window of
the most-recently-reviewed tickets, it emits (from the ledger alone):

- **`detection_proxy`** — the fraction of the criterion's blocking fix-units (criteria-free
  `fix_unit_key`) that the ticket **remediated to a PASS** (blocked in some round, then absent from a
  later PASS-verdict round). A caught-then-fixed "true-positive-ish" proxy R7 does not compute.
- **`blocking_fp_proxy`** — the fraction the gate later **de-escalated** (found again on a later
  round but no longer surfaced as blocking — decision `dropped`) without remediation: the gate's own
  re-review reversed the block, a conservative within-review FP lower bound. This keys off
  `decision == "dropped"` (which subsumes a Pass-3 threshold drop and the novelty/completion floors)
  because those floors are **inert by default** in the production corpus — every observed `dropped`
  finding carries `drop_reason` null, so a floor-only signal would be structurally dead. This is a
  **deliberately different metric from R7's same-named proxy** (which counts force-close/reopen, a
  post-hoc outcome signal): the two are complementary FP lenses with **different base rates**, so
  R7's 10% cliff does NOT transfer to this one — read this proxy as a relative/trend signal against
  its own committed baseline (`runs/criterion_effectiveness.json`), not against R7's threshold.

**Advisory→blocking promotion gate (referenced by R1/R3/R4).** A new criterion enters at **advisory**
posture (per this epic's operator direction). Promotion to blocking is gated on standing evidence
from this recorder — a criterion is a promotion candidate only once its trailing metrics show it
earns its blocks: a **high `detection_proxy`** (its blocks are acted on and reach PASS), a **low
`blocking_fp_proxy`** (the gate rarely reverses its own would-be blocks), and a **sufficient sample**
(`sample_counts.blocking_fix_units` / `resolvable_fix_units` large enough to be meaningful — a
single-digit denominator is not evidence). As with the demotion alarm, this is dogfood monitoring
that **triggers a human review**, not an automatic posture flip; the flip itself is an ordinary
`criteria_routing.json` change. Because the recorder auto-includes every criterion, R1/R3/R4 need
add nothing to be monitored — their advisory criteria accrue `detection_proxy`/`blocking_fp_proxy`
from real reviews automatically, and this section is the gate they cite for eventual promotion.

**R3 (`decomp-shape`) — a SECOND, independent gate on top of dogfood effectiveness.** The R3
decomposition-shape container criterion ships **advisory as its permanent posture**, so it never
blocks; but even as a *candidate* for a future blocking flip it is gated not only on the recorder
evidence above (high `detection_proxy` / low `blocking_fp_proxy` / sufficient sample) but ALSO on
**E6 judge order-stability clearing floor**. E6's order-shuffle experiment (ticket a880,
`runs/e6_summary.json`) FAILED — `fleiss_kappa = 0.55` (< the 0.6 floor) and `raw_agreement = 0.7`
(< the 0.8 floor) — a declared *R3 prerequisite failure*
(`harnesses/e6_judge_reliability.py`). Order-sensitivity is a *general* judge property (the E6
write-up: any new criterion, including R3's container criterion, inherits it), so while the same
plan re-judged after a section shuffle can land differently, a **blocking** container verdict from
`decomp-shape` would be untrustworthy. Advisory shipping is unaffected — a non-blocking coaching
nudge is safe under judge instability — but **blocking-promotion of `decomp-shape` is deferred
until BOTH (1) its dogfood effectiveness earns its blocks AND (2) E6 order-stability improves above
floor.** (This is why R3's stub "E2+E6 batch eval" gate was re-planned: E2/E3 batch evals are
permanently rejected on cost, and E6's order-stability result correctly defers *promotion* while
leaving R3 free to ship advisory + be dogfood-monitored now.)

## R4 — the necessity probe + the lightweight bug review tier

R4 (ticket 03a9, `clerkish-cloggy-cod`) ships two coupled ADVISORY pieces that close two gaps in
the gate.

**(a) The necessity / no-op probe (`necessity`).** A single-turn (1-TURN), advisory pass-1
criterion (facet `scope-intent`) that flags a plan which does **not demonstrate the change is
needed** — the current behavior is neither reproduced nor concretely motivated, so a mechanism is
added without establishing that the status quo is wrong. This is the gate counterpart to
**FixedBench's over-action result** (35–65% of agent changes taken without demonstrating
necessity — the prior-art T1 finding this probe closes). It ACCEPTS a well-motivated plan (a
reproduction, an Expected/Actual, a named defect/gap) and a justified no-op / docs-only /
test-only outcome. It is DISTINCT from R1's `asserted-capability` (which greps whether a named
module already provides the capability — a code-grounded surface check): `necessity` judges,
from the plan text, whether the change is motivated *at all*. It is a real pass-1 finder — wired
into the production `plan-review-finder` batch (`gates/plan-review.yaml`, `when:
include_necessity`) so it runs on leaf plan reviews — registered in `CANONICAL_LLM` and routed
advisory in `criteria_routing.json`.

**(b) The lightweight bug review tier.** Before R4 the gate short-circuited **every** bug to a
bare exempt-PASS (`workflow_ops.plan_review_precheck` → `orchestrator._exempt_verdict`,
`llm_calls:0`), so a bug got no substantive review — verified on bug 5886, whose persisted
`REVIEW_RESULT` was `{"runner":"exempt","verdict":"PASS","llm_calls":0}`. The bug tier instead
runs a **light advisory review**: the DET floor + the `necessity` probe
(`registry.BUG_TIER_CRITERIA = ("necessity",)`). The light tier is still subject to the
deterministic readiness floor: P1 (missing `## Acceptance Criteria`), P10
(verification-presence), and P4's description admission limit remain blocking and short-circuit
before any LLM call. Other DET findings are advisory, and the sole LLM criterion is advisory: after P1/P10/P4
pass, the light tier can be coached but never BLOCKED unless the bug escalates out of the
tier; equivalently, the bug-tier LLM probe never blocks a bug unless escalation sends it to the
full rubric. The
restriction is centralised in the single routing seam (`orchestrator.route_criteria` returns only
`BUG_TIER_CRITERIA` for a `bug`), so BOTH the assemble step and the batch-runner's project-criteria
fan-in honor it — an activated blocking `project.*` criterion can never be fanned into a bug review
and block it. `necessity` deliberately does **not** `suppress_types:["bug"]` (contrast F1/F4/A1/G7)
so it applies to bugs. The CLI claim-time bug exemption (`rebar._commands.gates`) is unchanged — a
bug still needs no signed attestation to be claimed; the tier only makes an explicit
`rebar review-plan <bug>` (and any gate run) produce a substantive advisory review instead of
exempt-PASS. `session_log` / `code_review` / `identity` stay fully exempt.

**(c) The blast-radius escalation out of the bug tier (ad0d B1).** The light tier is sized for a
small fix, so it is keyed on the fix staying small. A bug whose **persisted `file_impact` declares
any non-test path** leaves the tier and is reviewed by the FULL, blocking-capable rubric.
Escalation is the way a bug receives the full default blocking rubric; P1/P10/P4 remain blocking
even inside the light tier because they are deterministic admission requirements for a signed
plan. The predicate is
`orchestrator.bug_blast_radius_escalates(file_impact)`: a path counts as "test" iff it lives under
`tests/` or its basename is `conftest.py` — the same classification the Gerrit bugfix-size gate
(B2) applies to diff lines, so the plan-side and code-side ends agree. It is derived from ticket
state rather than a diff, because at review time no diff exists yet.

Both enforcement steps key on that one predicate, and an escalated bug is lifted on **both** bug
axes, not one: `workflow_ops.plan_review_precheck` skips the light-tier arm (so DET findings keep
their real blocking posture, exactly as for a non-bug), and `orchestrator.route_criteria` drops
the `BUG_TIER_CRITERIA` restriction *and* passes `ticket_type=None` into the applicability check
so the packaged `suppress_types: ["bug"]` axis that every full-suite criterion carries does not
empty the escalation out. Coverage records it as `bug_tier: False` + `bug_blast_escalated: True`.
The claim-time exemption is untouched — an escalated bug still needs no attestation to be claimed;
what changes is that an explicit `rebar review-plan <bug>` can now return a blocking verdict.

**Advisory-first + promotion.** Both pieces ship advisory (never block) and are validated by
HAND-AUTHORED bounded sanity fixtures (`src/rebar/llm/eval_specs/plan-review-necessity.eval.yaml`,
`rebar criteria eval necessity`; committed `runs/necessity_sanity.json` +
`runs/bug_tier_sanity.json`) — **not** an E2/E3 batch eval (those are permanently rejected on
cost, which is why R4's stub "retrospective eval on E2's class-4 corpus" gate was re-planned; E2's
corpus does not exist on main). Both criteria are auto-monitored by the standing per-criterion
effectiveness recorder (d8a5) with zero wiring, and their **advisory→blocking promotion is gated on
that recorder's trailing metrics** (high `detection_proxy`, low `blocking_fp_proxy`, sufficient
sample) exactly per the [promotion gate above](#standing-per-criterion-effectiveness-recorder--advisoryblocking-promotion-gate)
— a dogfood-triggered human review, not an automatic flip.

**Cadence + cost.** Run `--record` on a standing cadence (a cron / `session-log`-style invocation)
as reviews accumulate, then `--report`; both are deterministic and make **no LLM call**. The firing
ledger `runs/criterion_firings.jsonl` is a local, growing artifact (~8 MB over the current corpus,
so it is **git-ignored** — over the 500 KB large-file gate — and regenerated by `--record
--backfill`); the **committed CI-visible baseline is the computed metrics artifact**
`runs/criterion_effectiveness.json`, and the pure metric logic is CI-tested in
`tests/unit/test_criterion_effectiveness.py`. The future production seam is the emit-time hook at
`sidecar.emit` (`src/rebar/llm/plan_review/__init__.py`); the standing invocation is used today so
the review hot path stays byte-identical and best-effort-safe.

## The CI rigor signal

### Detailed derived plan-review health

Detailed readers (`rebar audit show`, the audit page, default `rebar show`, and MCP
`show_ticket`) expose the same **derived**, non-persisted `plan_review_health` payload.
It is recomputed with `compute_validity` from the certified plan-review attestation;
ordinary lists and ticket events intentionally do not gain this field. The payload includes
the canonical id and normalized role of every pinned child/prerequisite, its pinned and
current fingerprints, drift/missing state, enforcement posture, signed and required review
phases, and the effective execution floor. A disabled pin rule reports observed drift as
`advisory; enforcement disabled` without changing validity; an enabled rule is `enforced`
and can invalidate it. A missing execution floor is absent from text displays, never shown as
`0.00`.

`legacy-unpinned` identifies an older attestation without phase/pin-era metadata, while
`current-no-relationships` identifies a current attestation that deliberately recorded no
related-ticket pins. A deleted related ticket is shown as `stale-pin-missing`; repair it by
restoring/relinking the target as appropriate and running `rebar review-plan` again. Detailed
read failures use one compact shape — `{available: false,
reason: "derived plan-review health unavailable"}` — and never change a gate decision. Claim uses the
normal freshness profile; close uses its close profile (implementation code may advance), but
both retain material, phase, and enabled-pin checks.

`rebar verify-signature <ticket>` locally certifies a DSSE envelope by verifying its SSHSIG signature against the signer's Ed25519 public key. The signing environment is not itself a gate (bug `c21f-6f29-5d2d-4a5a`), so a certificate minted elsewhere certifies here when its signature verifies; the result's `trust_basis` names which key was used. A CI process that requires a named trusted environment uses `rebar verify-opcert` with the public key pinned in `.rebar/trusted_environments.yaml`. A certified current plan-review certificate records that the plan passed review. A claimed ticket without that certificate records that the review was bypassed. The completion close gate uses the corresponding distinction between a certified completion-verifier record and a close without one.

### Multiple attestation kinds + how a CI gate reads them

A ticket carries a kind-keyed `attestations` map. The `plan-review` and `completion-verifier` entries can coexist. Current entries contain DSSE envelopes that carry SSHSIG signatures over their PAE bytes, produced with the signing environment's Ed25519 key. Each certificate principal identifies that environment. `rebar show <ticket>` renders the map. New records contain no HMAC hex field. The top-level `signature` field is a compatibility mirror of the most recent attestation. ADR 0073 and ticket `352b-5097-9971-4dc1` record the mirror's history. A legacy HMAC gate record remains readable, but it returns `unknown_scheme` and cannot certify a current gated operation.

Read the map through these interfaces:

- **One kind.** `rebar verify-signature <ticket> --kind plan-review` selects plan-review. Use `--kind completion-verifier` for completion. Exit status 0 means the selected record is certified. Only a PASS produces a current operation certificate.
- **All kinds.** `rebar.signing.verify_attestations(ticket)` returns `{kind: verdict}`. `rebar show` also exposes the `attestations` map.
- **Certification and validity.** Cryptographic certification does not establish current applicability. A reopened ticket, code drift, a material edit, or stale related-ticket pins can invalidate a certified record. `plan_review.attest.compute_validity` performs these checks on read.

> A CI verifier needs the trusted environment's Ed25519 public key, not its private key or a shared HMAC secret. `.rebar/trusted_environments.yaml` pins that key and environment principal. `rebar verify-opcert` walks the merged ticket log, resolves each certificate's storage anchor, and verifies the DSSE envelope's SSHSIG signature. Without required-environment configuration, the certificate remains a local process record.

## End-to-end latency before work begins

The claim check performs local DSSE and SSHSIG verification against the signer's Ed25519 public key and recomputes freshness data. It does not require the certificate principal to identify this environment. It makes no LLM call or network request. The preceding `review-plan` operation runs a multi-pass LLM review and can take seconds or minutes depending on ticket size and review tier. A plan that needs revision may require several review rounds, and each edit invalidates the prompt cache. The review sidecar records latency and cost for later analysis.

## Asymmetric-error invariants (a design invariant — read before tuning a floor or adding a criterion)

The gate's reliability comes from each stage erring in a **deliberately opposite** direction; the
errors balance rather than compound. Documented here (R-3) so a future floor-tuner or criterion
author does not accidentally point two adjacent stages' skepticism the **same** way — which is how
real findings die (or false ones survive):

| Stage | Errs toward | Why |
|-------|-------------|-----|
| Pass-1 finder | **surface** (over-report) | recall first; a severity-free finder floods, the verifier filters |
| Pass-2 verifier | **the author** (charitable) | drops a finding whose evidence doesn't entail it under a charitable reading |
| Pass-3 decide | **drop** below 0.5 validity | arithmetic, not a second skepticism pass |
| DET floor (P1–P11) | **fail-open** | a check that cannot run abstains (recorded coverage) and is treated as PASS |
| Claim gate | **fail-closed** | a missing/stale plan-review attestation BLOCKS the claim |
| Novelty / completion floors | **KEEP** | when unsure whether a finding is novel / a criterion met, keep the finding / do not certify |

**Floor-tuning & criteria-authoring checklist:**
- Do NOT make two adjacent stages err the same way (e.g. a stricter verifier AND a higher validity
  cutoff double-counts skepticism — real findings die).
- A new blocking-eligible criterion must be in-session-closable and fail-open on what it cannot
  ground (mirror the DET floor); reserve fail-closed for the claim gate.
- A new DET check blocks ONLY when it is sound + unambiguous (P1 / P5-cycle / P8 / P10 / P11); everything else
  is advisory or coverage-only.
- Adding a Pass-2 graded sub-answer? Default it to `na` (excluded until answered) so old sidecars
  stay comparable (ADR 0032) — do not silently shift the validity denominator.

## The `removal-rationale` criterion (Chesterton's Fence — the removal-side dual of A1)

The gate has strong ADDITION-side discipline — A1 (rule-of-three / YAGNI / NIH /
anti-premature-optimization) catches an agent adding machinery it does not need. `removal-rationale`
is its **removal-side dual**: don't tear down a fence until you understand why it was built. An
autonomous agent under scope pressure is biased toward "simplifying" by deleting guardrails it does
not understand — exactly the early-trajectory defect this gate exists to catch. T4 already covers a
removal's *consequences* (consumer breakage, reversibility, destructiveness) and E5 partly covers
test sync, but none asks whether the plan *understands why the removed thing existed* — you can
knowingly tear down a fence with a rollback plan and still not know why it was built.

It is an **advisory, code-grounded, AGENT-tier** criterion (`applies_at: leaf`) with two bright-line
triggers (a disjunction — no subjective "is this incidental?" call): the plan removes/weakens an
externally-observable behavior or contract on any path (including failure/timeout/exception
semantics — "internal" means observable-behavior-preserving, not file-local); it removes a
guarding check/test; or it removes an artifact carrying an explicit intent marker (comment,
`# do not remove`, referenced bug, bug-named test). To PASS, the plan must supply a concrete
triggering scenario GROUNDED in evidence (comment / pinning test / git-blame / linked ticket) —
never invented — plus evidence the reason no longer applies. Coaching reuses **move 6
(specification-by-example)** to ask for that grounded scenario, and when E5's changed-behavior-tests
finding also fires, the Pass-4 coaching pass GROUPS the two rather than double-reporting.

**Accepted limitation (no silent cap — R-3):** a purely-latent guard whose removal changes behavior
only for inputs never exercised today AND which carries no intent marker will NOT fire — it is
indistinguishable from dead code without an external signal, and chasing it is the un-scalable nag
this criterion deliberately avoids. This limitation is recorded in the criterion's coverage, not
hidden.

## Scope (v1)

Shipped advisory-by-default with high thresholds; **threshold calibration and tier
re-validation were explicitly post-implementation** (calibration is only meaningful
against the running system — the eval suite + sidecar collect the real data to tune
later). **Two calibrations have now run**: the first (story `3d3d`) replayed the
`REVIEW_RESULT` sidecar corpus to flip seven dual-signal criteria to blocking at
`0.70`, validated by re-reviewing a 20-ticket high-finding/overlay sample
(`docs/experiments/plan-review-threshold-calibration.md`); the second (story
`usable-chattery-coelacanth`, 2026-07-08) lowered those to `0.60` and promoted
T1/T8/G1G2 (`0.70`) and E4 (`0.75`) on human adjudication showing under-blocking. The
current table lives in `src/rebar/llm/plan_review/criteria_routing.json` (pinned by
`tests/unit/test_threshold_recalibration.py`). Recalibrate on a cadence as more
sidecar data accrues — ADR 0036 mandates the replay be segmented by
`impact_model_version` (the calibration-2 thresholds predate the `plan-v2` impact
model shipped the same day, so a plan-v2-segmented replay is the standing next step).
Bug tickets are scored under the separate bug tier (`BUG_TIER_CRITERIA`), whose findings are
always advisory, so they are excluded from this blocking-threshold calibration rather than
exempt from review. See the epic for the full criteria registry and the experiment-grounded
defaults.

## Definition-of-done for a cutover/engine swap (live exercise required)

When a plan **cuts over or defaults to a new code path** (an engine/gate swap, a
default-flag flip), its definition-of-done **must include exercising that new path
end-to-end as it runs in production** — e.g. against a live model/dependency — not
only offline/mocked tests. **Green offline tests and a passing completion verdict are
necessary but NOT sufficient**: an acceptance criterion satisfiable by canned/fake
substitutes that bypass the new behavior can close green while the live path is broken
(the `super-plant-liver` root cause — B5 shipped with the live plan-review gate broken
because its AC was satisfiable offline-only). The plan-review gate enforces this at
review time: **E5** (testing) flags the *proxy-validation* anti-pattern — a
changed/defaulted risky path validated solely through a mock that never runs it live —
and **E6** (ac-text-quality) flags a cutover/defaulted path with no criterion that
exercises it end-to-end. The coaching move is **add a live/end-to-end acceptance
criterion for the path you are defaulting to** (Pass-4 moves 7 *thin vertical slice* /
9 *plan the verification*). The honest discriminator: *could this AC be marked done
without the changed risky path ever executing?* If yes, add the live DoD.

## Validating a gate change: which replay tier, and what counts as noise

**ADR 0109** covers the case ADR 0054 does not: a change to a **prompt or pipeline**
(a finder criterion prompt, a Pass-2 question, the finder system prompt/chunking) that
alters what the model would produce, rather than a threshold/impact-model change that
can be validated by ADR 0054's offline, zero-LLM replay of the persisted corpus. The
replay harness family lives under `src/rebar/llm/evals/plan_replay/`.

### Tier-selection table

| Change | Required tier(s) |
| --- | --- |
| Pass-3 code, threshold, or routing change | Tier 0, always |
| Pass-2 question or prompt change | Tier 1 (N≥40) + Tier 0 |
| Pass-1 criterion-prompt change | Tier 2 single-criterion (N=20) + downstream |
| Finder system prompt or chunking strategy change | Tier 2 full mode |

Every replay call MUST use the exact production frontier model for the pass it
replays (Bedrock only, no substitution) — each tier refuses to run otherwise. Attach
the generated report's path in the Gerrit commit message under an advisory
`plan-review-eval:` trailer (no CI enforcement, to stay portable).

### The per-tier noise band — each tier is judged against its own metric

"Indistinguishable from noise" is defined **per tier**, never shared across tiers,
because each tier scores a different metric:

| Tier | Metric | Noise floor source |
| --- | --- | --- |
| 0 | run-level verdict flip rate (PASS↔BLOCK) | identical-material flips: rerun the same stored material through the unchanged pipeline |
| 1 | per-question raw agreement + Cohen's kappa | the Tier-1 reproduction run's own agreement floor, plus Tier 0's flip-rate floor (Tier 1 always runs with Tier 0) |
| 2 | finding-set Jaccard (by `norm_id`/criterion) + candidate-vs-stored verdict flip matrix | an identical-candidate reproduction run: rerun the unchanged candidate against the same fixed sample |

A Tier-2 Pass-1 criterion-prompt change is judged only against the Tier-2-native
Jaccard/flip-matrix floor — never against Tier 1's per-question-agreement numbers,
which measure a different pass and are not comparable. See ADR 0109 for the full
decision record, the cost table observed from commissioning runs, and why sidecar
replay (not recorded-response mocking) is the required approach.
