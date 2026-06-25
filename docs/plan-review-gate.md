# The plan-review gate

The plan-review gate is the **inverse of the completion-verification close gate**:
where completion verification checks at *close* that a ticket's work was actually
done, the plan-review gate checks at *claim* (open→in_progress) that a ticket's
**plan is sound before an agent executes it**. Early plan defects compound over an
autonomous agent's trajectory, so catching them at the start is high-leverage.

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

2. **The claim gate** — when `verify.require_plan_review_for_claim` is on, `claim`
   (open→in_progress) does a **fast, local** check for a fresh, certified
   plan-review attestation and blocks if it is absent/stale. No LLM, no network —
   a pure HMAC verify + a light fingerprint recompute. `--force="<reason>"`
   bypasses it (audit-logged). It reuses rebar's existing atomic `claim` primitive,
   so two agents still cannot both claim a ticket.

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
| **2 — verify** | A *separate* verifier re-grounds each finding and emits coarse severity **attributes** + a typed **binary** sub-answer set `{yes\|no\|insufficient}`. One aggregate pass over all findings. | `passes.pass2_verify` |
| **3 — decide** | **Deterministic.** validity = graded fraction of the binary answers; impact = mean of the ordinal-mapped severity attributes; **priority = validity × impact**; decision = `block \| advisory \| dropped`. | `passes.pass3_decide` |
| **4 — coach** | A single-turn call over the *surviving advisory* findings maps each to a move from a locked registry; the coaching prose is rendered **deterministically** from the move's template (the LLM only picks the move + names a bounded noun-phrase subject — validated). | `passes.pass4_coach` |

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
`priority ≥ block_threshold` (default **0.95** ⇒ near-certain *and* high-impact);
else **advisory**. v1 ships thresholds high and posture advisory, so the LLM tiers
are almost entirely advisory during calibration — only the DET floor blocks by
default.

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

The surfaced advisory findings are capped at the top-N by priority (default **10**,
`orchestrator.DEFAULT_ADVISORY_CAP`); overflow goes to the `REVIEW_RESULT` sidecar,
not the agent. **Blocking findings are exempt** — all of them are always returned;
the cap can never weaken the block decision. (Volume is the lever that preserves an
LLM's ability to act on feedback; the cap is a tunable default, not a validated
constant.)

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
file-impact / decomposition). The claim gate verifies, in order:

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

## Configuration

| Key | Default | Effect |
|-----|---------|--------|
| `verify.require_plan_review_for_claim` | `false` | When true, claiming a work ticket requires a fresh certified plan-review attestation. **Turning it off is the rollback** — an ordinary preference, no kill-switch needed. |

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

Advisory-by-default with high thresholds; **threshold calibration and tier
re-validation are explicitly post-implementation** (calibration is only meaningful
against the running system — the eval suite + sidecar collect the real data to tune
later). Bugs are exempt (a dedicated follow-on). See the epic for the full criteria
registry and the experiment-grounded defaults.
