# Plan-Review Gate — Validation & Tuning Experiment Plan (with a seeded-defect pilot)

What to run with the revised criteria set (`criteria_v6.json`: 31 descriptors incl. new G6/T10/T11/T12 + 8 roll-ins)
to raise confidence and tune **before finalizing**. The recurring honest gap across every round — and the
brainstorm's own stated primary metric — is that we've validated reviewer **self-consistency (Jaccard)**, never
**ground truth**. These experiments target that, in priority order.

## E1 (top priority) — Seeded-defect recall & false-accept (the gold set; the brainstorm's primary metric)

**Why:** consistency can't tell a reliable false-positive from a real finding; only labeled cases can. The brainstorm
named **FNR-on-seeded-bad-plans** the primary metric and it has never been measured. **What:** for each criterion,
construct a KNOWN-BAD plan containing exactly the defect it should catch + a matched KNOWN-GOOD plan; run the
criterion and measure **recall** (bad→FAIL), **false-accept / rubber-stamp** (bad→PASS — the load-bearing LLM-judge
risk), and **false-fire** (good→FAIL). Scale to ~3–5 cases × every criterion, human-adjudicated. Promotes follow-on
**F10 (gold set)** into the pre-finalize loop. **Reported per criterion → tells you which criteria to trust/retune.**

### Pilot run (8 criteria × bad/good × 2 repeats = 32 runs, Sonnet, single-turn)
| criterion | BAD (want FAIL) | GOOD (want PASS) | recall | clean |
|---|---|---|---|---|
| G6 approach-soundness | TOCTOU check-then-act idempotency | atomic compare-and-swap | **FAIL ✓** | AMBIGUOUS |
| T10 infra/IaC | AdministratorAccess + plaintext key + local state | scoped IAM + SSM + remote state + prevent_destroy | **FAIL ✓** | AMBIGUOUS |
| T11 migration | single-shot NOT NULL + 50M-row backfill | expand-contract + batched + resumable + rollback | **FAIL ✓** | AMBIGUOUS |
| T12 rollout | replace algo, deploy 100% | feature-flag + canary + rollback | **FAIL ✓** | **PASS ✓** |
| E5 change-detector | snapshot-of-current-output + source-grep test | RED failure-path + boundary test | **FAIL ✓** | PASS/AMBIG |
| COH coherence | "full coverage" vs "no tests needed" vs reversed deps | consistent | **FAIL ✓** | AMBIGUOUS |
| T9 concurrency | `count = count + 1` shared, parallel | atomic counter | **FAIL ✓** | AMBIGUOUS |
| A1 anti-patterns | generic plugin framework for one handler | add the one handler, extract on Rule-of-Three | **FAIL ✓** | **PASS ✓** |

**Recall = 100% (8/8), zero rubber-stamping** — the new criteria and roll-ins catch the defect they're designed for.
The tuning finding is on the clean side: the **codebase-grounded criteria hedge to AMBIGUOUS** (non-blocking) on a
*terse* good plan when run *without tools* — the same "needs the live code to confirm" behavior that flipped to PASS
with the agentic tier in round 4. **Two tuning levers:** (a) run G6/T10/T11/T9/COH grounded-against-the-codebase
(agentic) where the GOOD case resolves to PASS; (b) the single-turn PASS threshold for a well-specified-but-
unverifiable case should be more decisive. AMBIGUOUS-rate on clean plans is the precision metric to track. Raw:
`plan-review-gate/runs/seedpilot.jsonl`; harness `plan-review-gate/harnesses/seedpilot.py`.

→ **Next for E1:** expand to 3–5 adjudicated cases per criterion (all 31), run the codebase-grounded ones with real
tools, and report a per-criterion recall / false-accept / false-fire table — the confidence scorecard to finalize on.

## E2 — Exercise the never-run criteria for baseline behavior

G6, T10, T11, T12, T1, T3, T4, T5d, G3, G4, COH have descriptors but **no coverage data**. Run them on representative
real tickets (the DSO sample + the epic's 9 children + a few infra/migration/deploy tickets) for fire-rate, FP, cost,
and consistency — the round-3/4 protocol applied to the new criteria. Especially **G6** (AGENT, ~$0.12) and the new
overlays. Confirms they don't over-fire on ordinary tickets and earn their cost.

## E3 — Overlay-trigger precision for the new overlays (extend round-4 Stream C)

Build a small set of infra / data-migration / deployed-behavior tickets + matched non-applicable ones; check the
**deterministic** triggers for T10 (IaC keywords) / T11 / T12 vs an LLM router — precision (fire on the right
tickets) and recall (don't miss). T10's keyword trigger should be high-precision; T11/T12 likely LLM-routed.

## E4 — Generalization on a NON-DSO corpus (the overfit critique)

The `applies_at` level/type routing was tuned on the DSO sample (calibrating against the test set). Run the suite +
routing on a **different ticket population** (rebar's own tickets, or synthetic plans in non-epic→story→task styles —
e.g. leaf detail directly in stories, untagged test-tasks) and check the routing + criteria still behave. This is the
held-out validation the round-4 retune lacked.

## E5 — Weak/local-model ceiling

Run a **Haiku** reviewer across chunk sizes and on the agentic tier to locate its reliable-N ceiling and whether it
loses the structural-gap signal — grounds the "small models chunk finer" default and tells clients what a cheap
reviewer can and can't do.

## E6 — G6 alternative-generation & anti-priming validation

Specifically test G6's reviewer-generated-alternatives mechanism: (a) it flags a clearly-better missed alternative;
(b) it PASSes a defensible approach without manufacturing alternatives; (c) **the finding text never leaks rejected
options that would reach the implementer** (the anti-priming property). Seed approach-choice scenarios with a known
better/worse alternative.

## E7 — Structured-checklist lift validation (the Q10 follow-up)

After lifting the prose sub-checks into the descriptor `checklist[]` arrays, A/B whether structured-checklist scoring
changes recall/consistency vs prose on our own criteria — validates the binary-checklist claim on our set, not just
in the cited literature.

## Priority & sequencing

E1 (recall/false-accept) is the single highest-leverage — it's the only experiment that measures whether criteria
catch what they should, and it doubles as the per-criterion tuning scorecard. Then E2 (baseline the never-run set),
E6 (G6 mechanism), E3 (new-overlay triggers), E4 (generalization — the strongest answer to "is this DSO-overfit"),
E5 (weak model), E7 (checklist lift). E1+E2+E6 are the **must-haves before finalizing**; E4 is the must-have before
claiming the defaults generalize beyond DSO.
