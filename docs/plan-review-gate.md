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
*and* high-impact); else **advisory**. After the first dogfood-data calibration (story
`3d3d`; see `docs/experiments/plan-review-threshold-calibration.md`), **seven criteria
that the verifier rarely refutes and that empirically drive plan revisions** — **G6,
COH, T5e, E2, G5, F1, T4** — are `default_posture: blocking` at `block_threshold: 0.70`
(a precision-first cut: only a verifier-confirmed, high-impact finding from one of these
blocks). Every other LLM criterion stays advisory (`0.95`), including the demonstrably
false-positive-prone set (G1G2/T6/T5b/E5/E6/F4) and the confident-but-routinely-ignored
set (T3/T10/T8). The DET floor (P1/P5/P8) still blocks unconditionally.

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
| 9 | plan the verification | "Plan how {subject} will be verified before implementing it." |
| 11 | propagate to children | "Propagate the revision for {subject} to the child tickets." |
| 12 | generalize the finding | "Generalize {subject} across the rest of the work." |

**Project-extensible:** a project adds or overrides moves by id via
`.rebar/plan_review_moves.json` (`{move_id: {name, template}}`; the template must
contain a single `{subject}` placeholder). The **C1 subject validator**
(`passes._validate_subject`) rejects code/imperatives/overlong subjects so the move
can only ever name what to investigate, never hand over a solution.

### The advisory cap

The surfaced advisory findings are capped at the top-N by priority (default **20**,
`orchestrator.DEFAULT_ADVISORY_CAP`); the overflow goes to the `REVIEW_RESULT` sidecar,
not the agent, and the **overflow count** is reported on the verdict
(`coverage.counts.advisory_overflow`, shown as `overflow=N` in the CLI summary) so a
capped list never reads as a complete count. **Blocking findings are exempt** — all of
them are always returned; the cap can never weaken the block decision. (Volume is the
lever that preserves an LLM's ability to act on feedback; the cap is a tunable default,
not a validated constant.)

## Proportionate scrutiny & routing

Criteria carry an `applies_at` descriptor (`registry.applies`): leaf-implementation
criteria don't run at epic/story altitude, container criteria (G3/G4) run only when
there are children, and type rules apply (**bugs and session_logs are exempt** from
the whole gate; mechanical/test tasks suppress noisy criteria). Overlays fire from
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

**Remediation mode is gated, not the default.** The floor applies only when ALL hold:
config `remediation_mode` on; the plan changed; the **code is unchanged** since the baseline
(detected by `verified_at_sha` equality against the prior signed manifest — reusing the
signed snapshot ref, no new diff machinery); the registry is unchanged; a prior sidecar with
finding text exists; and the last review of any kind is within the freshness window
(default 60 min, measured from the last review and **reset on each review**, so the loop
persists across a series of edits and lapses to a normal full review only after the agent
goes idle). Any precondition failing → a **byte-identical full review**. A separate
**evidence-gate** flag `novelty_drop_active` (default off) keeps the floor inert until the
`discriminates_novelty` eval has proven the novelty signal discriminates novel from
carryover (`rebar prompt eval plan-review-novelty`).

A narrowed verdict records `narrowed: true` + `floored_criteria` + `floored_finding_ids` on
its `coverage`, and the dropped novel findings are written to the `REVIEW_RESULT` sidecar
(joinable by `norm_id`) — so within-session suppression is always **observable**. This is
the *complement* of the code-drift `drift_refresh` path (ADR 0002): drift-refresh is plan
**unchanged** + code drifted; remediation is plan **changed** + code unchanged. Setting
`remediation_mode` (or `novelty_drop_active`) off restores byte-identical full-review
behavior — the total back-out.

## Configuration

| Key | Default | Effect |
|-----|---------|--------|
| `verify.require_plan_review_for_claim` | `false` | When true, starting work on a work ticket (`claim`, or `transition open→in_progress`) requires a fresh certified plan-review attestation. **Turning it off is the rollback** — an ordinary preference, no kill-switch needed. |
| `verify.remediation_mode` | `false` | Enable the rising-floor remediation re-review (above). Off/absent → byte-identical full review (the back-out). |
| `verify.remediation_window_minutes` | `60` | Freshness window: a re-review is eligible only if the last review of any kind was within this many minutes (measured from it, reset on each review). |
| `verify.novelty_drop_threshold` | `0.7` | `T_novel`: a finding is droppable only if its novelty ≥ this. |
| `verify.novelty_priority_floor` | `0.4` | The rising floor: drop a novel finding only if its priority < this (a scalar ≈ the corpus p40 impact percentile; `scripts/plan_review_impact_distribution.py` prints the inputs). |
| `verify.novelty_drop_active` | `false` | **Evidence gate.** The floor stays inert until flipped true — done manually only after the `discriminates_novelty` eval clears its bar. |

Enable it in `.rebar/config.conf` (dotted legacy form) or a `[verify]` table in
`rebar.toml` / `pyproject.toml`:

```ini
# .rebar/config.conf
verify.require_plan_review_for_claim = true
```

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

## The CI rigor signal

`rebar verify-signature <ticket>` certifies the attestation; a CI process treats a
**certified plan-review signature at the current HEAD** as the "this plan was
reviewed" predicate, and a **claimed-without-signature** (or force-claimed) ticket
as the durable signal that the review was skipped — exactly parallel to the
completion close gate's signed-verdict / closed-without-signature pair.

## End-to-end time-to-first-work (honest)

The **claim** check is fast (a local HMAC verify; the ~50 ms target is a structural
property — it makes no LLM/network call, proven by a test). But the **honest**
end-to-end time to start work includes the out-of-band `review-plan` run: the LLM
four-pass review takes seconds to minutes depending on ticket size + tier, and the
edit→re-review convergence loop (~2–3 rounds for a plan that needs revision) busts
the prompt cache each round, so the real cost-to-signature ≈ per-run cost ×
revisions. Per-run latency/cost is captured on the sidecar for passive refinement —
no upfront wall-clock benchmark is claimed.

## Scope (v1)

Shipped advisory-by-default with high thresholds; **threshold calibration and tier
re-validation were explicitly post-implementation** (calibration is only meaningful
against the running system — the eval suite + sidecar collect the real data to tune
later). The **first calibration has now run** (story `3d3d`): the `REVIEW_RESULT`
sidecar corpus was replayed to flip the seven dual-signal criteria above to blocking at
`0.70`, validated by re-reviewing a 20-ticket high-finding/overlay sample
(`docs/experiments/plan-review-threshold-calibration.md`). Recalibrate on a cadence as
more sidecar data accrues. Bugs are exempt (a dedicated follow-on). See the epic for the full criteria
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
