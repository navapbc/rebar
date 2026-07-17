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

2. **The start-work gate** — when `verify.require_plan_review_for_claim` is on,
   starting work on a ticket (**any** entry into `in_progress` — via `claim`, a plain
   `transition <id> open in_progress`, a `blocked` resume, or reactivating a `closed`
   ticket) does a **fast, local** check for a fresh, certified plan-review attestation
   and blocks if it is absent/stale. Gating *entry into `in_progress`* (keyed on the
   target status, not only the `open` edge) means no side-door —
   `open → blocked → in_progress` or `open → closed → in_progress` — can start
   un-reviewed work past the gate; a legitimately-reviewed ticket keeps a valid
   attestation, so a normal block/resume passes. All entry points run the **same**
   consolidated check (`rebar._commands.gates.plan_review_precheck`), so they cannot
   diverge. No LLM, no
   network — a pure HMAC verify + a light fingerprint recompute. Bugs and session_logs
   are exempt. `--force="<reason>"` bypasses it (audit-logged; on the `transition` path
   pass `--force` and the `--reason` text becomes the audit note). `claim` additionally
   reuses rebar's atomic claim primitive, so two agents still cannot both claim a ticket.

A review is a **process, not a dialog**: when a finding blocks (or you want to
clear advisories), revise the ticket and re-run `review-plan` to earn a fresh
signature — exactly like the completion verifier.

## The verdict model — four passes (find → verify → decide → coach)

The gate has **two layers**:

* **Layer 1 — the deterministic floor (P1–P8)** — `det_floor.py`. The *only* tier
  that blocks **by default**. Frozen, deterministic, polyglot, fail-open. The
  sound, unambiguous blockers are **P1** (missing `## Acceptance Criteria`
  checklist), **P5** (a dependency *cycle* among children), and **P8** (the ticket
  is too big to review in full even one criterion at a time → "reduce/decompose").
  P2/P3 (file/package resolution via the grounding oracle) are coverage-only;
  P4/P6/P7 (oversize / AC-quality / destructive-op sniff) are advisory.

* **Layer 2 — the advisory coaching review (the four passes)** — never blocks by
  default. Each of the 32 criteria (the F/E/G/A judgment criteria, the T1–T12
  triggered overlays, COH, and ISF) ships as a **contract-bearing prompt in the
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
| **4 — coach** | A single-turn call over the *surviving advisory* findings maps each to a move from a locked registry; the coaching prose is rendered **deterministically** from the move's template (the LLM only picks the move + names a bounded noun-phrase subject — validated). | `passes.pass4_coach` |

**Verifier model.** Pass-2 verify (and the Pass-4 coach, which share the verify cfg) run on
the decisive non-frontier `VERIFIER_DEFAULT_MODEL` (`claude-sonnet-4-6`) **unless the operator
explicitly chose a model** (`REBAR_LLM_MODEL` / `[tool.rebar.llm].model` set to a non-default).

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
(P1/P5/P8) still blocks unconditionally.

**The hard-override floor is oracle-graded for `ac_unverifiable` (plan-v3, story
`large-sleepful-needlefish`).** `impact_plan` floors a finding at 0.85 when
`dod_uncertifiable` / `undecomposed` / `divergent_implementation` is present at any
grade — but `ac_unverifiable` is graded by ORACLE KIND, a closed vocabulary enforced at
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
(ADR 0036), which this change bumps to `plan-v3`.

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
| 14 | state attestation evidence | "State the concrete attestation evidence the [operator-attested] {subject} will require (a change id / vote outcome / timestamp), recorded on the ticket." |
| 10 | foundation/enhancement split | "Deliver {subject} with existing machinery first; make the ideal version a dependent follow-on ticket." |
| 11 | propagate to children | "Propagate the revision for {subject} to the child tickets." |
| 12 | generalize the finding | "Generalize {subject} across the rest of the work." |
| 13 | realign to parent plan | "Realign {subject} to the parent's plan — the parent wins on conflict; if the parent is genuinely wrong, update the PARENT first (which forces its re-review), never silently diverge the leaf." |

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
  "activate": ["project.portability"]
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
- `Platform and venue`: macOS, Windows, Linux, BSD, CI, servers, developer workstations.
- `Project location and access`: in-checkout current working directory, explicitly located workspace, server outside the checkout, no unrestricted-local-filesystem assumption.

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
Separately, **bugs and session_logs are exempt** from the whole gate (a distinct
exemption axis, not part of container/leaf scrutiny); mechanical/test *leaves* suppress
noisy criteria. Overlays fire from
low-false-positive deterministic triggers where safe (T5a/T5d/T7/T12) and are
LLM-routed otherwise. **Only the code-grounding set (E4/G1G2/A1/G6) greps the
codebase**; everything else reasons from the plan text. The reviewed plan is
**always whole** — never truncated, never content-chunked; the rubric is the lever
that fits a context window (batch criteria → one-criterion-per-call → escalate the
model → if still too big, P8 fails it as "reduce the ticket").

## Attestation, freshness & invalidation

On a non-blocking `PASS`, `review_plan` signs a manifest via the existing signing
machinery (`rebar.signing.sign_manifest`; HMAC-SHA256 under the environment key; a
`SIGNATURE` event — see [reuse-surface.md](reuse-surface.md)). The manifest's first
line is `plan-review: PASS` (distinguishing it from a completion signature) and it
binds a **material fingerprint** (a hash of description / acceptance-criteria /
file-impact / decomposition). The start-work gate (`claim` / `transition
open→in_progress`) verifies, in order:

1. the signature is **certified** under the environment key;
2. it is a **plan-review** manifest (not a completion one);
3. it was made at the **current code HEAD** (the same freshness binding the close
   gate uses) — a code commit since the review invalidates it;
4. the bound material fingerprint matches the **current** ticket — a material edit
   (description/AC/file-impact/decomposition) invalidates it. (Tags/comments/links/
   assignee are *not* material and do not invalidate.)
5. it **post-dates the latest reopen** — reactivating a ticket (`closed → open`,
   recorded as `state["last_reopened_at"]`) invalidates an attestation signed before it.

**Validity-on-read, not write-time mutation (epic dark-acme-lumen, ADR 0009).** These
checks are computed when a gate reads an attestation, by the single
`plan_review.attest.compute_validity(attestation, ticket_state, kind)` dispatcher — the
attestation records themselves are **immutable** and are never cleared/mutated by a
transition (the old reopen-time `retire_attested_pin` is gone). An attestation can thus be
HMAC-`certified` (integrity intact) yet not **valid** for its gate (e.g. after a reopen or a
material edit). **Invariant: gates call `compute_validity` on a certified attestation — they
never trust HMAC certification alone, nor mutate a record.** This is also what lets a ticket
hold a plan-review *and* a completion-verifier attestation at once without either clobbering
the other (the kind-keyed `attestations` map; the legacy top-level `signature` is a
back-compat mirror of the most-recent one).

The attestation means **"a review process was followed, no blocking red flags, with
coverage recorded"** — *not* "perfect". The rich per-criterion verdicts live in the
sidecar; a project composes any hard CI gate by checking the signed result + its
coverage.

### What the `signature` field guarantees (read this before trusting it)

A signature (a real `SIGNATURE` event + a manifest whose first line is `plan-review:
PASS`) is emitted **only** on a genuine non-blocking `PASS` where the LLM tier actually
ran — i.e. **not** on `BLOCK`, **not** on `INDETERMINATE`, and **not** for an `exempt`
runner (`bug` / `session_log` tickets, which are exempt from the gate). On every other
outcome no manifest is signed and **no event is written to the ticket** (the ticket's
own `signature` stays null), so the start-work gate has nothing to verify and the claim
is denied.

The one subtlety that has misled callers: the `review_plan` **verdict JSON** always
carries a `signature` *object* — its shape is `{signed: bool, …}`. On a signed PASS it
is `{signed: true, key_id, head_sha}`; on every other outcome it is `{signed: false,
reason: "<VERDICT>"}` (e.g. `{signed: false, reason: "BLOCK"}`, or even `{signed:
false, reason: "PASS"}` for an *exempt* runner that returned PASS but was not signed).

> **Read `signature.signed` (a boolean), never the mere presence of the `signature`
> object.** `if result["signature"]:` / `signed = bool(result.get("signature"))` is a
> bug — the object is *always* present and truthy, so that check reports a signed BLOCK.
> The trustworthy proof-of-PASS is the **certified `SIGNATURE` event** (`rebar
> verify-signature <ticket>` / the claim gate's local HMAC verify), which a `BLOCK` can
> never produce — `signature.signed == true` in the verdict JSON is only its in-band
> echo.

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
  concise log line (`plan review reused ... -- pass --force to re-run`) marks the skip.
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
`7d43`; ADR [0008](adr/0008-convergent-plan-edit-re-review.md)) makes it converge while
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

Default **off** ⇒ `claim` keeps today's behavior exactly. An unreadable config
fails this opt-in gate *off* with a warning (it never auto-enables across a repo).

## Fail-open behavior

* **Unsupported stack / missing tool / parse error / timeout** in any DET check →
  the check `abstain`s (records a reason) and is treated as PASS. The recorded
  abstain set *is* the coverage.
* **LLM unavailable** (missing `[agents]` extra / no API key) → `review_plan`
  degrades to a **DET-only** review (the floor still blocks on P1/P5/P8; advisory
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

## The CI rigor signal

`rebar verify-signature <ticket>` certifies the attestation; a CI process treats a
**certified plan-review signature at the current HEAD** as the "this plan was
reviewed" predicate, and a **claimed-without-signature** (or force-claimed) ticket
as the durable signal that the review was skipped — exactly parallel to the
completion close gate's signed-verdict / closed-without-signature pair.

### Multiple attestation kinds + how a CI gate reads them

A ticket carries a **kind-keyed `attestations` map** (epic dark-acme-lumen): independent
attestations of different **kinds** — `plan-review` (signed out-of-band by `review-plan`,
verified at claim) and `completion-verifier` (signed by the close transition), with room for
future kinds — coexist instead of
clobbering one slot. `rebar show <ticket>` renders the map (the raw HMAC hex is stripped from
every kind); the legacy top-level `signature` is a back-compat mirror of the most-recent
attestation (its removal is the deferred follow-up — see ADR 0009 / ticket
`352b-5097-9971-4dc1`).

To read them:
- **Per kind (the CI-gate form):** `rebar verify-signature <ticket> --kind plan-review`
  (or `--kind completion-verifier`) — exit 0 iff that kind is certified. Since **only a PASS
  is ever signed**, `certified` already implies the gate passed.
- **All kinds at once:** the library `rebar.signing.verify_attestations(ticket)` →
  `{kind: verdict}`; or the `attestations` field of `rebar show`.
- **"Certified" ≠ "valid":** a record can be HMAC-`certified` yet stale (reopened / code
  drifted / materially edited). A gate that cares about current applicability — including a
  CI gate — must also confirm it (status, freshness), which is what
  `plan_review.attest.compute_validity` does on the read path.

> **CI deployment constraint (known follow-up).** The signature is an HMAC under the
> environment's signing key, so a CI runner that *verifies* attestations needs that same
> `REBAR_SIGNING_KEY` (a shared secret). This is the symmetric-key limitation of the current
> scheme; moving to asymmetric verification (a CI verifier needs only a public key) — and
> associating attestations with the *code* under review, not just the ticket — is deliberately
> **out of scope** here and tracked as future work. This epic builds the per-ticket primitives
> and does **not** add a dedicated CI `gate-check` command.

## End-to-end time-to-first-work (honest)

The **claim** check is fast (a local HMAC verify; the ~50 ms target is a structural
property — it makes no LLM/network call, proven by a test). But the **honest**
end-to-end time to start work includes the out-of-band `review-plan` run: the LLM
four-pass review takes seconds to minutes depending on ticket size + tier, and the
edit→re-review convergence loop (~2–3 rounds for a plan that needs revision) busts
the prompt cache each round, so the real cost-to-signature ≈ per-run cost ×
revisions. Per-run latency/cost is captured on the sidecar for passive refinement —
no upfront wall-clock benchmark is claimed.

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
| DET floor (P1–P9) | **fail-open** | a check that cannot run abstains (recorded coverage) and is treated as PASS |
| Claim gate | **fail-closed** | a missing/stale plan-review attestation BLOCKS the claim |
| Novelty / completion floors | **KEEP** | when unsure whether a finding is novel / a criterion met, keep the finding / do not certify |

**Floor-tuning & criteria-authoring checklist:**
- Do NOT make two adjacent stages err the same way (e.g. a stricter verifier AND a higher validity
  cutoff double-counts skepticism — real findings die).
- A new blocking-eligible criterion must be in-session-closable and fail-open on what it cannot
  ground (mirror the DET floor); reserve fail-closed for the claim gate.
- A new DET check blocks ONLY when it is sound + unambiguous (P1 / P5-cycle / P8); everything else
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
model shipped the same day, so a plan-v2-segmented replay is the standing next step). Bugs are exempt (a dedicated follow-on). See the epic for the full criteria
registry and the experiment-grounded defaults.

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
