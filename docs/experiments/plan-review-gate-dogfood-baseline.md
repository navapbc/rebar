# Plan-Review Gate — Dogfood Baseline (running the gate on its own epic)

Ran the **full criteria spectrum** on the revised epic `5fd2-a7c2-0aec-48fa` using the **default protocol** from
the grounded design, to see what feedback looks like, refine the epic, and judge whether the criteria need tuning.

## Protocol used (the grounded defaults)

- **DET tier (deterministic floor):** rebar's own `check_ac` (pass, 13 criteria lines) + `clarity_check`
  (score 7 / pass). Stands in for P1 readiness-shape / P4 oversize.
- **Single-turn tier (Opus 4.8, cached, facet-packed chunks of 6** — epic → `size_factor 0.5 × base 12 = 6`**):**
  Chunk A `[F1,E2,E6,F4,E3,G5]` (ac-text-quality + scope-intent), Chunk B `[E1,E5,T5a,T5b,T5c,T5e]`
  (coherence + testing + overlays), ×2 repeats, + a bounded broad open-ended pass.
- **Agent tier (Sonnet 4.6 + real Grep/Read/Glob over the rebar repo, one agent each):** E4, G1G2, A1.

## What the gate found (first pass on the revised epic)

| criterion | verdict | finding (abridged) |
|---|---|---|
| **E4** (agentic) | **FAIL** | `"solidlsp (already in repo)"` + ast-grep/syft/scc/lizard claimed present, but a glob over `src/rebar/**` finds **none** — false existence claim. *Only the agentic tier could catch this.* |
| **G1G2** (agentic) | FAIL | same — named Layer-1 tooling artifacts absent from the codebase. |
| **G5** | FAIL (major) | epic-scale body joining ~8 independent goals with no children → should decompose. |
| **E5** | FAIL (major) | no AC names testing for the behavioral surface (fail-open, invalidation, override, cap-shedding). |
| **E6** | FAIL (major) | ACs describe end-states but name no proving command/check. |
| **F1** | FAIL (major) | SC "coaching findings that *measurably help* an agent revise" = post-sprint telemetry, not in-session observable. |
| **BROAD** | FAIL (major) | no named **invocation surface** — what command runs the review, who triggers it, where. |
| **T5a** perf | FAIL (major) | synchronous `claim` path has no latency target; agentic O(n) LLM-calls cost unstated. |
| **T5c** security | FAIL (minor) | signing key-management / forgery-replay model not stated. |
| F4, E1, E2, E3, A1, T5b, T5e | PASS | correctly silent (A1 anti-slop verified via tools → genuine PASS). |

## Refinements applied (and convergence check)

Addressed the real findings in the epic, then **re-ran the failing criteria**:

| criterion | after revision |
|---|---|
| F1 | **PASS** — reworded SC to a structural in-session observable (REVIEW_RESULT schema); remediation-uplift → offline F1 |
| E5 | **PASS** — added a tests+verification AC (per-tool fail-open, invalidation, override, shedding, sidecar) |
| T5a | **PASS** — added a claim-path latency target (signature-verify only, p95 < ~50ms) |
| T5c | **PASS** — stated integrity model is inherited from close-gate signing (no new key custody) |
| E6 | FAIL **major→minor** — one residual (the "external-CI-checkable" SC still names no proving step) |
| G1G2 | FAIL **→ AMBIGUOUS** — corrected the false "already in repo" claim; remaining artifacts legitimately don't exist yet |
| E4 | still FAIL — flags real-but-unverified *reuse* claims (rebar.llm catalog, close-gate signing); fail-closed by design |
| G5 | still FAIL (major) — **correct and intentionally left open**: decomposing into the implementation wave is the explicit next action |

The named invocation surface (BROAD) and false tooling claim (E4/G1G2) were also fixed in the epic text. **The
revise loop converges:** targeted plan edits resolved 4/6 single-turn FAILs to PASS and improved the rest.

## Tuning assessment — do the criteria need recalibration?

**No major recalibration needed.** The default protocol produced **high-signal, low-FP** feedback:
- Every FAIL was a real, actionable plan defect; every PASS was correctly silent. No spurious findings.
- Findings were addressable — well-formed (criterion + location + severity + suggested fix), and fixing the plan
  text resolved them.
- The agentic tier paid for itself: it caught a factual error (absent tooling) no single-turn check could, at
  ~$0.06/criterion / ~15 tool calls / ~50–100s — consistent with the cost model.

Two **minor** calibration seams, both already covered by existing follow-ons (no new scope):
1. **E4 should distinguish "contradicted" from "unverified-pending-implementation."** On a design epic that
   legitimately defers file-impact, E4's fail-closed posture flags real-but-not-yet-built reuse claims as gaps —
   useful as coaching, but it should separate *false* (block-worthy) from *unverified* (advisory nudge). → maps to
   follow-on **F3** (confidence/severity floor for blocking).
2. **G5 fires on an intentionally-undecomposed epic.** Correct in substance, but for an epic whose *next step* is
   decomposition, the gate should either accept a "decomposition deferred" acknowledgment or gate G5 on a
   ready-for-execution signal. → maps to proportionate-scrutiny **routing** (run container-decomposition criteria
   only when the ticket claims execution-readiness).

Net: the grounded defaults (severity-ranked advisory findings, anti-FP disciplines, exec-tier split, facet
chunking) are well-calibrated as a baseline. The gate found real defects in its own design epic, the fixes stuck,
and the only tuning needs are refinements already on the F-register — not a redesign.

Raw run: `plan-review-gate/runs/gate_run.jsonl`; protocol harness `plan-review-gate/harnesses/run_gate.py`.
