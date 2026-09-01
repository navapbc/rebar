# ADR 0109 — Plan-review replay harness: tier selection, model parity, and the per-tier noise band

**Status:** Accepted (epic `compliant-lemon-grunion` / `759e-7dc1-675c-4877`)
**Date:** 2026-09-01
**Relation:** EXTENDS ADR 0054 (the field corpus is the calibration instrument; costly
fresh LLM-based assessments are rejected as calibration instruments) and epic `6982`'s
no-live-A/B directive. Neither is superseded — this ADR answers a narrower question ADR
0054 left open: how a contributor validates a **prompt/pipeline** change (not a
threshold/impact-model change), for which ADR 0054's offline replay of the persisted
`REVIEW_RESULT` corpus does not apply because a prompt edit changes what would be
elicited from the model, not just how a fixed set of already-elicited answers is scored.

## Context

ADR 0054 established that threshold and impact-model calibration must be driven by
replaying the persisted `REVIEW_RESULT` sidecar corpus offline, with zero LLM calls,
and rejected commissioning fresh LLM assessments to answer a question the corpus
already answers. That is correct for Pass-3 (`impact_*`, `pass3_decide`): those
functions are deterministic and pure, so a threshold change is a pure recompute.

It does not extend to a change in a **finder criterion prompt**, a **Pass-2 question**,
or a **finder system prompt / chunking strategy** — those change what the model
*produces*, which the persisted corpus cannot answer by replay alone; the pipeline has
to actually be re-run with the candidate prompt against real material. Prior to this
harness, contributors editing these had no sanctioned way to validate a change short of
either (a) shipping blind and finding out from production dogfooding, or (b) a
purpose-built one-off eval commission — exactly the expensive, ad-hoc pattern ADR 0054
rejects for the cases it *does* cover. Nobody was required to use any particular
harness, so in practice changes to prompts and chunking went out unvalidated.

A harness nobody is required to use changes nothing, so this ADR also fixes *when* it
is required (the tier-selection table) and *what counts as signal vs. noise* for each
tier's own metric (the noise-band rule) — leaving either open would leave the harness
optional in practice.

## Decision

### 1. Three replay tiers, one harness family (`src/rebar/llm/evals/plan_replay/`)

- **Tier 0** replays Pass-3 (`pass3_decide`/`pass3_over_findings`) over the full
  persisted `REVIEW_RESULT` corpus. Deterministic and pure — **zero LLM calls, zero
  marginal cost** — this is exactly ADR 0054's offline recompute, packaged as a
  harness. Required for any Pass-3 code, threshold, or routing change.
- **Tier 1** replays Pass-2 (impact/validity questions) over a stratified sample
  (N≥40) against the current model, comparing candidate answers to the stored
  answers per question (`raw` agreement + Cohen's kappa). Required for any Pass-2
  question or prompt change, always run together with Tier 0 (a Pass-2 change can
  shift what reaches Pass-3).
- **Tier 2** replays Pass-1 (the finder pass) over a smaller fixed sample (N=20),
  either in **full mode** (every criterion) or **single-criterion mode**
  (`--criteria <id>`, one criterion at a time — materially cheaper for a
  single-criterion prompt edit). It compares candidate finding sets to stored
  finding sets by `norm_id`/criterion (Jaccard, gained/lost), then verifies the
  candidate findings via the same `verify_findings` seam Tier 1 uses and computes
  their verdict via `pass3_decide`/`pass3_over_findings`, compared against the
  stored verdict (a flip matrix). Required for a Pass-1 criterion-prompt change
  (single-criterion mode) or a finder system-prompt/chunking change (full mode).

### 2. Model parity is non-negotiable

Every replay call MUST use the exact production frontier model for the pass it
replays, sourced from the Bedrock provider id actually configured for that pass in
production (currently `bedrock:us.anthropic.claude-opus-4-8` for Pass-1,
`bedrock:us.anthropic.claude-sonnet-4-6` for Pass-2) — never a substitute, cheaper, or
non-Bedrock model. A run against the wrong model answers a different question than the
one the harness exists to answer (would this change help/hurt *production*), so each
harness refuses to run when the resolved model does not match. This is the same
model-parity discipline ADR 0054 implicitly assumes for the corpus it replays (the
corpus was produced by production models) made explicit for a harness that elicits
fresh answers.

### 3. Budget ledger and cap

Every live (non-deterministic-recompute) run is priced from the provider's own
token/request accounting and appended to the append-only
`docs/experiments/plan-review-gate/replay/ledger.jsonl` before the run is considered
complete — `run_id`, `tier`, `candidate`, `sample_n`, per-pass model id, USD, and
token counts. A pre-flight budget estimate is computed from stored per-criterion usage
(`coverage.usage.per_criterion`) times current price before a run is issued, refusing
runs that would exceed a proposal's declared ceiling. See "Cost table" below for
observed per-tier costs.

### 4. Sidecar replay, not recorded-response mocking (`jira-reb-529`)

The harness replays against real stored material (reconstructed tickets, sidecars) and
issues real model calls where a tier requires them — it does not mock or replay
canned/recorded model responses. A recorded-response mock only proves the harness
reproduces what was recorded, not that a prompt change would behave differently against
live material; the entire point of Tiers 1–2 is to observe how a *changed* prompt
behaves against unchanged material, which a canned response cannot do by construction.

### 5. Per-tier noise-band acceptance rule — each tier is scored on its own metric

Because Tier 0, Tier 1, and Tier 2 are scored on different metrics, "indistinguishable
from noise" is defined **per tier**, against that tier's own reproduction run, not a
single shared band:

| Tier | Metric | Noise floor source |
| --- | --- | --- |
| 0 | run-level verdict flip rate (PASS↔BLOCK) | identical-material flips: re-running the same stored material through the *unchanged* pipeline and measuring the flip rate that occurs from nondeterminism/registry drift alone |
| 1 | per-question raw agreement + Cohen's kappa | the Tier-1 reproduction run's own per-question agreement floor, always reported alongside Tier 0's flip-rate floor (Tier 1 always runs with Tier 0) |
| 2 | finding-set Jaccard (per criterion, by `norm_id`) + candidate-vs-stored verdict flip matrix | an *identical-candidate* reproduction run: rerunning the unchanged candidate against the same fixed sample and measuring the Jaccard/flip-matrix floor that occurs from model nondeterminism alone, reported alongside the committed baseline report |

A change is judged improving/regressing **only when it moves outside its own tier's
band** — a Tier-2 Pass-1 criterion-prompt change is judged against the Tier-2-native
Jaccard/flip-matrix floor, never against Tier 1's per-question-agreement numbers (they
measure different passes and are not comparable), and a Tier-0 threshold change is
judged only against Tier 0's flip-rate floor. Tier 1 is the one case with a *combined*
band (its own agreement floor plus Tier 0's, since a Pass-2 change always also runs
Tier 0).

### 6. Tier selection is required, not advisory, at review time

| Change | Required tier(s) |
| --- | --- |
| Pass-3 code, threshold, or routing change | Tier 0, always |
| Pass-2 question or prompt change | Tier 1 (N≥40) + Tier 0 |
| Pass-1 criterion-prompt change | Tier 2 single-criterion (N=20) + downstream |
| Finder system prompt or chunking strategy change | Tier 2 full mode |

The report file path attached as evidence is named in the commit message under a
`plan-review-eval:` trailer — advisory (no CI enforcement), to keep the convention
portable to projects without this harness.

## Cost table (observed, from the commissioning ledger)

| Tier / mode | Sample N | Model | Cost | Per-sample |
| --- | --- | --- | --- | --- |
| Tier 0 | full corpus (3,758 replayed rows) | n/a — deterministic recompute | $0 | $0 |
| Tier 1 | 60 | `bedrock:us.anthropic.claude-sonnet-4-6` | $6.72 | $0.112 |
| Tier 2, full mode | 20 | `bedrock:us.anthropic.claude-opus-4-8` | $6.96–$11.83 across runs | ~$0.35–$0.59 |
| Tier 2, single-criterion (1-TURN, e.g. `E2`) | 20 | `bedrock:us.anthropic.claude-opus-4-8` | $1.85 | $0.093 |
| Tier 2, single-sample diagnostic | 1 | `bedrock:us.anthropic.claude-opus-4-8` | $0.27 | $0.27 |

Tier 2 single-criterion mode's $0.093/sample sits well under the $0.25/sample ceiling
that applies to `1-TURN`/`2-STEP` criteria; an `AGENT`-tier `--criteria` run (including
the G3/G4 container split) is priced at its real production rate and reported
separately, with no fixed per-sample ceiling assumed. All commissioning spend against
this epic's tickets stayed within each ticket's own declared ceiling (see the ledger).

## Consequences

- A contributor editing a criterion prompt, a Pass-2 question, or a Pass-3 threshold
  now has one place to look (`docs/plan-review-gate.md`'s tier-selection table) to know
  which tier(s) are required and what report to attach.
- The per-tier noise band prevents a contributor from being told "this changed nothing"
  or "this regressed" using a metric that does not apply to the pass they changed.
- This ADR does not change ADR 0054's scope: Tier-0 threshold/impact-model calibration
  is still the offline, zero-LLM-cost corpus replay ADR 0054 mandates. This ADR only
  covers the cases ADR 0054 explicitly leaves open — prompt and pipeline changes that
  alter what the model would produce, which no offline replay of already-elicited
  answers can validate.
