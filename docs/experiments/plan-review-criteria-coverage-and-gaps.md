# Plan-Review Gate — Criteria Coverage, Research Comparison, Gap Analysis & Additions (Round 5)

Driven by Joe's questions: do our criteria cover the standard quality dimensions; how do they compare to 2025–2026
research on LLM-plan failure modes; what gaps do senior reviewers find; how should we coach "consider alternatives"
without negative priming; what does DSO's implementation-plan *decider* add; and which of our criteria should have
caught a broad critical review's findings. Backed by five xhigh subagents (Google Principal SWE, Principal-SWE
gap-audit, a cited 2025–2026 research scan, a broad critical review of our own plan, and a DSO decider extraction)
plus direct analysis. Result: **4 new criteria (G6, T10, T11, T12)** + **8 roll-in extensions**, all in
`plan-review-gate/criteria/criteria_v6.json` and guarded by `check_registry_coverage.py`.

## Q1 — Do we cover maintainability / decay / reliability / perf / security / a11y / feasibility / fitness / correctness / accuracy-vs-code?

| dimension | covered? | by |
|---|---|---|
| maintainability | YES | T5e (coupling/changeability/ADR) + A1 |
| architectural decay | PARTIAL | plan-level "does this add decay" = P4 oversize + A1 + T5e. *Longitudinal* decay of existing code (churn/clone/hotspots) is a codebase-health/retro concern, mostly out of plan-review scope |
| reliability | YES | T5b (+ now observability + dependency-failure degraded-mode) |
| performance | YES | T5a (+ now cost/economics) |
| security | YES | T5c (+ now least-privilege + secret-lifecycle) |
| accessibility | YES | T5d |
| feasibility | YES | T3 + E4 |
| fitness for purpose | YES | F4 + E3 + E6, now sharpened by **G6** |
| correctness | **was a GAP → fixed** | plan-level proxies (E6/E5/E3) plus the new **G6** (mechanism correctness — the defect a well-formed plan can still have) |
| accuracy vs ground truth of code | YES (our strongest) | AGENT tier: E4 (assertion probe), G1G2 (symbol/file existence), A1 (NIH) |

8/10 were already well-covered; **correctness-of-approach was the real gap** (now G6), and **infra** was under-covered
(now T10).

## Q2 — 2025–2026 research comparison (15-mode failure taxonomy)

A cited scan (~30 sources) produced a 15-mode taxonomy of LLM-plan failures and mapped each to our registry. The
research consensus: the **most damaging modes for autonomous agents are early, silent, and compounding** — #1
unverified codebase assumptions, #2 premature-commitment to a plausible-but-wrong approach, #5 ambiguity-driven
silent guessing (a wrong premise propagates with no human to catch it; *Long-Horizon Task Mirage* arXiv:2604.11978,
*Where LLM Agents Fail* arXiv:2509.25370).

- **Strong coverage (direct criteria):** #1 hallucinated assumptions (P2/P3/E4/G1G2), #5 ambiguity (E2), #6
  under-decomposition (G5/P4/P5), #7 missing edge-paths (E5/T6/T5b), #9 untestable AC (F1/E6), #10 NIH (A1),
  #11 blast-radius (G1G2/T4), #14 no-empirical-probe (T2).
- **Clean misses (2):** #3 weighing alternatives / trade-off rationale (no criterion) → **G6**. #15 trusting unsound
  self-verification → handled by *architecture* (external AGENT-grounding + anti-fabrication), defensibly not a
  registry item.
- **Partials tightened (3):** #2 general premature-commitment (only T1/T2 caught novel cases) → **G6**; #8
  change-detector / self-authored-oracle tests → **rolled into E5**; #12 rollback-as-remedy (T4 only checked
  *acknowledgement*) → **T4 sharpened + new T11/T12**.

Most strategically important: #2's general case — the highest-compounding gap — is exactly what **G6** closes.
Full taxonomy + citations: subagent research output (session log).

## Q3–Q5 — Senior-reviewer gaps, and the additions decision (distinct vs roll-in; valuable + generic)

A Google PSE produced ~24 independent criteria and a Principal SWE adversarially audited ours. Convergent verdict,
evaluated for distinctness/value/genericness:

### ADD (distinct, valuable, generic) — now in the registry
- **G6 — Approach soundness, anti-patterns & alternative-selection** [AGENT, complexity-gated]. *The* top gap (both
  PSEs ranked #1; research #2/#3). Checks (1) mechanism correctness (e.g. a check-then-act idempotency that's a
  TOCTOU race — passes every other criterion today), (2) fitness-for-purpose, (3) approach-selection vs reviewer-
  generated alternatives, (4) presence of a positive rationale. Distinct from E3 (scope-fidelity) and E6
  (end-state). Subsumes correctness + alternatives + fitness; folds the 7 DSO anti-patterns into A1.
- **T10 — Infrastructure / IaC** [overlay, deterministic trigger] (Joe-requested). State/locking, least-privilege
  IAM, idempotency/drift, blast-radius + destroy-safety, secrets, cost/sizing, observability. Distinct from T9
  (app-level shared state) and T5c (OWASP-app). Generic (cloud-agnostic), triggered (free when N/A).
- **T11 — Data-migration / backfill safety** [overlay]. Online/expand-contract, batching, resumability, dual-write
  window, rollback. Distinct from T4 (breakage-acknowledgement ≠ migration-execution safety).
- **T12 — Rollout / rollback / reversibility** [overlay]. Flag/canary/staged + tested rollback + deploy ordering.
  Distinct from T4/T5b; PASS-N/A for libraries/CLIs.

### ROLL-IN (a sub-check of an existing criterion — not distinct enough for a new id)
- **A1** ← the full 7 DSO anti-patterns (golden-hammer, cargo-cult, resume-driven, premature-optimization, + NIH /
  premature-abstraction / config-proliferation it already had).
- **T9** ← concurrency-safety / idempotency (atomicity, lock/CAS, TOCTOU) — distinct *question* from lifecycle
  completeness, so called out explicitly.
- **T5a** → performance **& cost**. **T5b** ← observability + dependency-failure degraded-mode. **T5c** ←
  least-privilege + secret-lifecycle (broadened trigger). **E5** ← change-detector / self-authored-oracle / tautology
  test anti-pattern. **T4** ← require a rollback/back-out plan as the *remedy*. **P3** (DET) ← license + CVE/advisory +
  maintenance-health (syft already runs).
- **G5** ← consume P4's oversize signal (mis-tier fix) + a vertical-slice / evidence-gated-MVP **sequencing** check.

### Redundancy / mis-tier fixes (Principal SWE)
- E1 vs COH boundary clarified (E1 = criterion↔deliverable mapping + terminology + dups; COH = cross-*section*
  contradictions only). E2/E6 de-dup the shared "implicit AC" signal. G5 was mis-tiered (1-TURN guessing file/layer
  counts) → consume the AGENT edit-set. Confirm the router never LLM-invokes T5d absent a UI.

## Q6 — Infrastructure overlay: added as **T10** (above).

## Q7 — DSO implementation-plan decider (the missed agent) → G6

DSO's `approach-decision-maker` (the decider) + `approach-proposer` are exactly what we'd missed. Adopted into G6:
its **5 decision dimensions** (codebase-alignment [Grep-verified], blast-radius, testability, simplicity [anchored on
complexity gates], robustness), its **7 anti-patterns** (→ A1), and the proposer's **≥3-distinct-proposals across 4
structural axes** (data-layer / control-flow / dependency-graph / interface-boundary). Critically, DSO **validates our
anti-priming design** (Q8): its task-decomposer receives only the *chosen* approach ("do not re-architect or
substitute") and rejected alternatives are logged in-session, **never persisted to a ticket the implementer reads**.

## Q8 — Coaching "consider alternatives" WITHOUT negative priming

**Concern:** a persisted "Alternatives Considered (rejected)" section primes the implementer to incorporate rejected
behavior. **Solution (now G6):** separate the *process* (were alternatives weighed?) from the *implementer-facing
plan* (chosen approach + positive rationale only). Three mechanisms:
1. The plan contains only the chosen approach + a **positive** "why this fits" rationale — never a rejected list
   (positive priming reinforces the chosen path).
2. The **reviewer** adversarially generates 1–2 structurally-distinct alternatives, checks the chosen approach is
   defensibly at-least-as-good, then **discards** them. A clearly-better missed alternative becomes out-of-band
   coaching to the *planner* (who revises the single chosen approach) — the implementer still sees one approach.
3. Evidence of consideration (N candidates weighed, chosen on dims D) is recorded in the **REVIEW_RESULT sidecar /
   attestation** (out-of-hot-path; implementer never reads it) as a durable rigor signal.

This validates serious consideration with **zero** rejected options in the implementer's artifact — and matches DSO's
battle-tested architecture.

## Q9 — Broad critical review → which criterion should have caught it, & why missed

A skeptical xhigh review of our *own* plan found valid issues; mapping them to our criteria is strongly
self-validating (the two CRITICAL ones map to a criterion we **dropped** and the one we're **adding**):

| critical finding | should be caught by | why missed |
|---|---|---|
| #1 blocking on `claim` contradicts the "coach, never block" posture | **COH** + E3 | COH was a *dropped* criterion (only restored in the reconciliation), never run on the epic |
| #2 fail-open + advisory + `--force` ⇒ signature certifies a plan with checks skipped/advice ignored (signal ≠ "rigor") | **G6** + E6 | G6 didn't exist; E6 checked AC→proving-command, not "does the signal mean what it claims" |
| #3 defaults grounded in self-consistency (Jaccard), not ground truth; efficacy deferred to F1 | T2 + F1 | T2 didn't scrutinize whether the validation measured the *right* thing |
| #4 50ms claim is sleight-of-hand (effective latency = minutes); caching breaks under edit-then-re-review | T5a (+cost) | T5a checked the claim-latency target, not end-to-end time-to-work nor cache-busting on re-review |
| #5 enormous front-loaded scope; 9 parallel stories don't de-risk; no thin-slice MVP | G5 (sequencing facet) | G5 checked decomposition-*existence*, not de-risking *sequence* |
| #6 LLM-tier review is non-reproducible; code-drift (F9) is central not follow-on | G6/E6 + E4/T9 | no criterion checks signature-claim vs process-guarantee |

**Design-level findings flagged for Joe (decisions, not just criteria gaps):** #1 (blocking-on-`claim` vs coaching)
and #2 (what the signature actually certifies under fail-open + advisory + `--force`) are the deepest. The reviewer's
cleanest resolution: **advisory-everywhere on `claim`, put any hard gate in CI (branch/PR), and make the attestation
a richer-than-boolean rigor vector** (coverage, open-MAJOR count, `forced`) with one canonical in-repo CI predicate.
The brainstorm deliberately chose `claim` as the enforcement point — so this is a posture decision to revisit, not a
silent bug. Captured as an open question on the epic.

## Q10 — Is the boolean/checklist approach incorporated?

YES, deliberately (v3 §1 adopted binary-checklist decomposition over 1–5 scalar scoring — CheckEval / TICKing /
AbsenceBench: binary checks raise agreement, cut variance, make omission-detection tractable). The criteria are
authored as binary sub-checks `(a)/(b)/(c)`. Two nuances: (1) the structured `checklist[]` field isn't yet populated
in the JSON — checks live in `scenario` prose; lifting them to an array is mechanical registry work (registry +
layer2 child). (2) Genuinely-qualitative criteria (E3 narrative blind-restate, BROAD open-ended pass) correctly stay
graded/narrative — boolean isn't forced where it would lose signal. That is exactly "boolean where viable without
losing signal."

## Net change

Registry grows from 31 → 35 *named* criteria concepts; `criteria_v6.json` holds all 31 LLM descriptors (the DET
P1–P7 are code; BROAD is the open pass). Additions are disciplined: 4 genuinely-distinct criteria, 8 roll-ins, 0
redundant new ids. Overlays are triggered, so the larger set costs nothing on tickets where they don't apply.
