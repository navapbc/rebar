# ADR 0024: Completion-aware container plan-review (the Pass-3 completion floor)

- **Status:** Accepted
- **Context:** Epic *Completion-aware plan-review for container tickets* (`66ac-fd03-6d4d-4249`);
  stories 457a (the `delivered-now` predicate), 94fd (the Pass-2 completion sub-call), 6533 (this
  ADR + the Pass-3 completion floor + config), 77cf (the calibration gold-set). Relates to the
  plan-review epic `5fd2-a7c2-0aec-48fa`, ADR 0008 (the novelty rising floor), ADR 0002 (code-drift
  invalidation / drift-refresh), ADR 0009 (attestation validity-on-read), and ADR 0016 (container/
  leaf proportionate scrutiny).

> **Numbering note.** The originating ticket named this ADR *0020*; that number was taken by the
> two-vote-CI-gate ADR (epic 1fa8) that landed first, so it is filed as **0024** (next free).

## Context

A container ticket (an epic, or a story with children) is reviewed **repeatedly** over its life —
once up front, then again each time it is re-decomposed, re-scoped, or its plan-review attestation
goes stale. But by the time of a re-fire, some of its children are **already delivered** (closed
with a valid completion-verifier attestation, or superseded by a live in-epic sibling). A full
re-review re-reads the whole plan, including the settled acceptance text of that delivered work, and
**re-litigates it**: it raises fresh, low-stakes findings about the *wording* of an AC that a closed,
verified child already satisfied — "step 3's phrasing is ambiguous," "this section could be sized
smaller." Those findings are throw-away: the plan text they critique describes work that is **done**,
so there is nothing left to act on. They produce false BLOCKs and stale coaching, and — exactly like
the un-converged remediation loop ADR 0008 solves — they never go green.

This is **distinct** from both prior Pass-3 floors and must not be conflated with them:

- **ADR 0008 (novelty floor)** suppresses *novel, low-priority* findings across a **plan-edit**
  remediation loop (plan changed, code unchanged). Its axis is *novelty vs the prior review*.
- **ADR 0002 (drift-refresh)** refreshes an attestation when only the **reviewed code** drifted
  (plan unchanged). Its axis is *material freshness*.
- **This floor (completion)** suppresses findings that only re-litigate **already-delivered,
  settled plan text** on a container. Its axis is *what the finding is about* — delivered work vs
  live work, and plan-semantics vs delivered-functionality.

We explicitly do **not** touch code-drift invalidation (ADR 0002): a delivered child's code changing
should still invalidate, and that signal is orthogonal and correct.

## Decision

Add a **third deterministic Pass-3 floor** — the *completion floor* — that drops a finding **iff it
is entirely about delivered, settled plan text and is low-stakes**, computed from three closed-enum
sub-answers plus the existing priority score, and gated exactly like the novelty floor.

### The three-field completion sub-answer schema (Pass-2, story 94fd)

A **separate** single-turn Pass-2 sub-call (`plan_review_completion` contract + prompt) classifies
each surfaced finding against the **delivered-children manifest** (each already-delivered child + its
own `## Acceptance Criteria`). It emits three atomic sub-answers per finding, each a **closed
vocabulary** coerced per-finding to a fail-safe default (so one bad value never fails the batch):

| Field         | Closed values                                             | Fail-safe default              |
|---------------|-----------------------------------------------------------|--------------------------------|
| `attribution` | a delivered child ticket-id \| `"none"`                   | `"none"` (about no closed child) |
| `containment` | `"limited-to-closed"` \| `"spans-open-or-system"` \| `"n-a"` | `"spans-open-or-system"`    |
| `layer`       | `"plan-semantics"` \| `"delivered-functionality"` \| `"n-a"` | `"delivered-functionality"` |

`attribution` answers *which* delivered child the finding is about (a G3/G4 container finding that
already carries a structural `_container_child` has this set deterministically in code — the model is
never asked to re-derive it). `containment` answers whether the finding is **limited to** that closed
work or **spans** open/system work (the third question that distinguishes "settled" from "still
live"). `layer` answers whether the finding is about the **plan document itself** (scope/clarity/
sizing — throw-away for delivered work) or the **delivered functionality** (mechanism/contract — a
real signal even on closed work).

### The `delivered-now` predicate (story 457a)

A child is **delivered-now** iff **either**:

- **(A) attested-closed** — `status == "closed"` AND its `completion-verifier` attestation is **valid
  on read** (ADR 0009 `compute_validity`, keyed on that child's own `last_reopened_at` + material
  fingerprint). A **force-closed** child (closed without a signed verdict) is therefore **not**
  delivered-now — every finding on it is kept. **or**
- **(B) superseded-by-live-sibling** — a sibling `A` under the **same parent** links `A -supersedes->
  child`, and `A` is itself live (open/in_progress, or itself attested-delivered). This avoids
  hard-blocking a container review on a **stale closed** ticket that a live sibling has replaced
  (a real, observed failure — not hypothetical). Non-recursive; the supersede link is never
  hierarchy-promoted.

Fail-closed on any read error (treat as *not* delivered — keep the finding).

### The Pass-3 completion-floor drop rule (story 6533)

Deterministic, pure — **no LLM in the decision path** (mirrors `rising_floor_drop`). A finding is
**dropped iff ALL** hold:

1. `attribution` is a child id that is **provably delivered-now** — i.e. present in the manifest's
   delivered-id set. This is stronger than "not `none`": a *structural* `_container_child`
   attribution can name a **force-closed** (unverified) child, and a model attribution can name a
   non-manifest id — both must be **kept** ("delivery is proven, not assumed", invariant 3);
2. `containment == "limited-to-closed"`;
3. `layer == "plan-semantics"`;
4. `priority` (`validity × impact`) `< completion_priority_floor`;
5. **none** of the finding's criteria is in the always-preserve set.

Every other combination **keeps** the finding. Because each fail-safe sub-answer value
(`attribution="none"`, `containment` anything but limited-to-closed, `layer` anything but
plan-semantics) is a non-drop value, an unsure/degraded classification **always fails toward KEEP**.

### Config knobs (`VerifyConfig`)

- `completion_floor_active: bool = False` — the **evidence gate**. Inert until an operator flips it
  on after the calibration gold-set (story 77cf) clears its must-never-suppress bar. Default off ⇒
  the floor never drops a finding ⇒ the verdict is **byte-identical** to today's (the total back-out,
  exactly like `novelty_drop_active` was — though `novelty_drop_active` has since been retired
  in the config-prune epic and its floor is now always-on).
- `completion_priority_floor: float = 0.4` — the low-stakes bar (the corpus "below major" band; same
  default as `novelty_priority_floor`).
- `completion_preserve_criteria: tuple[str,…] = ("T5c", "T10")` — the always-preserve set, referencing
  **registered** criterion ids: the security overlay (`T5c`) + the endpoint/interface contract
  (`T10`). A finding on a preserved criterion is **never** dropped regardless of the other axes — so a
  delivered child's "endpoint has no auth" or "contract omits a field consumers rely on" is always
  surfaced. Adding privacy/compliance ids is a **config change, not code**.

### The sidecar `drop_reason` field

The per-finding slim payload in the `plan_review_result_v1` sidecar gains `drop_reason ∈ {null,
"completion", "novelty"}`: `null` for surfaced/normal findings, `"completion"` for completion-floor
drops, `"novelty"` for novelty-floor drops. Each floor stamps its own value on the dropped finding;
`_slim` reads it directly. This is the **join key** that disambiguates the two floors offline so a
calibration join never conflates them (G6). Completion drops additionally record namespaced coverage
keys (`completion_floored_criteria` / `completion_floored_finding_ids`) that never collide with the
novelty floor's `floored_*` keys.

## Invariants (the design's load-bearing properties)

1. **Container-only + evidence-gated.** The floor runs only when the ticket has children AND
   `completion_floor_active` is on. A leaf has nothing delivered to settle; off ⇒ byte-identical.
2. **Fail toward KEEP, everywhere.** Ambiguous sub-answers, an empty manifest, a degraded sub-call,
   a read error in `delivered-now`, or an unclassified finding index all resolve to **no drop**.
3. **Delivery is proven, not assumed.** `delivered-now` reuses the ADR 0009 completion-verifier
   validity-on-read — a force-closed or reopened child is not delivered, so its findings are kept.
4. **Delivered-functionality is never thrown away.** The `layer` axis keeps mechanism/contract
   findings even on closed work; only *plan-semantics* (scope/clarity/sizing) findings — moot once
   the work ships — are dropped. The preserve set is a second, criterion-level veto over that.
5. **Deterministic in Pass-3.** The drop is pure arithmetic + closed-enum comparison; no LLM holistic
   severity anywhere in the decision. Recomputed every run — never persisted.

## Alternatives rejected

- **Hard-block / skip delivered children entirely.** Rejected: it would miss a real
  delivered-functionality or security regression in closed work, and it hard-blocks on stale closed
  tickets — the exact failure the supersede branch (B) exists to avoid.
- **Priority alone (no `layer` axis).** Rejected (explicit reviewer direction): priority cannot
  distinguish a throw-away plan-wording nit from a low-priced-but-real delivered-contract gap. The
  plan-semantics vs delivered-functionality distinction must be its **own** axis.
- **Two questions (attribution + layer), no containment.** Rejected: without "is this finding
  *limited to* the closed child," a finding that spans a closed child **and** an open sibling would be
  wrongly dropped. Containment is the third question that keeps spanning findings live.
- **Structural attribution only (no Pass-2 association question).** Rejected: a non-G3/G4
  plan-semantics finding has no structural child link, so there would be no way to tell whether it is
  a valid signal about live work or throw-away text about delivered work.

## Consequences

- A re-fired container review **stops re-litigating delivered, settled plan text** while still
  surfacing everything about live children, delivered functionality, security, and contracts.
- **Back-out is trivial and total:** `completion_floor_active=false` (the default) restores
  byte-identical behavior. The novelty floor (ADR 0008) and drift-refresh (ADR 0002) are untouched
  and orthogonal — the three Pass-3 floors compose along independent axes (novelty / material
  freshness / delivered-completion).
- Suppression is **observable**: narrowed verdicts record the namespaced coverage keys and every
  dropped finding lives in the sidecar with `drop_reason="completion"` — joinable offline for the
  calibration measurement (story 77cf).
