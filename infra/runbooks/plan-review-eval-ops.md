# Runbook — plan-review eval ops (replay tiers · heal loop · breakers)

Scope: operating the plan-review evaluation harness — running replay tiers, budgeting them,
and running the mined-fixture heal loop. The decisions behind these procedures are
[ADR 0109](../../docs/adr/0109-plan-review-replay-harness.md) (replay tiers and their
comparison bands) and
[ADR 0113](../../docs/adr/0113-fixture-mining-and-admission-protocol.md) (fixture mining,
admission, and the breaker).

**Which tier a given change requires is not decided here.** That table is single-sourced in
[`docs/plan-review-gate.md`](../../docs/plan-review-gate.md) §"Validating a gate change:
which replay tier, and what counts as noise". Read it first; this runbook covers invocation
and budget once you know the tier.

## Replay tiers: invocation and budget

```sh
python -m rebar.llm.evals.plan_replay tier0    # Pass-3 over the corpus
python -m rebar.llm.evals.plan_replay tier1    # Pass-2 over a corpus sample
python -m rebar.llm.evals.plan_replay tier2    # Pass-1 over a fixed sample
```

`--candidate` selects the prompt candidate to replay (registry default: `current`).
Tier 2 takes `--criteria <criterion-id>` for single-criterion mode:

```sh
python -m rebar.llm.evals.plan_replay tier2 --candidate current --criteria E2
```

The replay reads the tracker store roots from `REBAR_ROOT` (the checkout's own
`.tickets-tracker`) by default; pass `--store name=path` (repeatable) to replay a different
store set.

Tier selection is **required, not advisory**, at review time. Attach the report file path
as evidence by naming it in the commit message under a `plan-review-eval:` trailer. That
trailer is advisory — no CI enforcement — deliberately, so the convention stays portable to
projects that do not run this harness.

### Cost table (observed, from the commissioning ledger)

| Tier / mode | Sample N | Model | Cost | Per-sample |
| --- | --- | --- | --- | --- |
| Tier 0 | full corpus (3,758 replayed rows) | n/a — deterministic recompute | $0 | $0 |
| Tier 1 | 60 | `bedrock:us.anthropic.claude-sonnet-4-6` | $6.72 | $0.112 |
| Tier 2, full mode | 20 | `bedrock:us.anthropic.claude-opus-4-8` | $6.96–$11.83 across runs | ~$0.35–$0.59 |
| Tier 2, single-criterion (1-TURN, e.g. `E2`) | 20 | `bedrock:us.anthropic.claude-opus-4-8` | $1.85 | $0.093 |
| Tier 2, single-sample diagnostic | 1 | `bedrock:us.anthropic.claude-opus-4-8` | $0.27 | $0.27 |

Tier 2 single-criterion mode's $0.093/sample sits well under the $0.25/sample ceiling that
applies to `1-TURN`/`2-STEP` criteria. An `AGENT`-tier `--criteria` run (including the
G3/G4 container split) is priced at its real production rate and reported separately, with
no fixed per-sample ceiling assumed. Keep each run inside its ticket's own declared ceiling
and record the spend in the ledger.

**A tier is judged only against its own band.** A Tier-2 Pass-1 criterion-prompt change is
judged against the Tier-2-native Jaccard/flip-matrix floor, never against Tier 1's
per-question-agreement numbers — they measure different passes and are not comparable.
Tier 1 is the one case with a combined band, because a Pass-2 change always also runs
Tier 0.

## Heal loop and the unreliable-criterion breaker

```sh
rebar criteria heal --dry-run    # preview selection, no spend
rebar criteria heal
```

The loop is **gap-only**: it mines only criteria the gate routes but that still lack an
eval spec. A criterion already covered by a spec is out of scope. The loop advances its
due-stamp only after the configured interval, so it is safe to invoke on a schedule.

Mined-fixture evaluation is **change-triggered**, not time-triggered: it runs on
rubric-change commits via the `eval-changed-rubrics` job in
[`.github/workflows/prompt-eval.yml`](../../.github/workflows/prompt-eval.yml), selecting
criteria whose rubric changed since the base ref, and is deliberately excluded from the
weekly live sweep. The execution surface is portable — selection and the dry-run preview
run in-process with no CI provider required — so the rule is not bound to any one CI
system.

Activity is recorded in the ledger at `.rebar/fixture_heal_ledger.jsonl`.

### When a breaker fires

A criterion the loop cannot mine reliably is quarantined rather than retried forever.
A breaker fires when either:

- a per-criterion failure counter reaches its threshold (default **3**; reproduction
  failures and emitter `skipped-unbalanced` results accrue the same counter), or
- a single runner declares the criterion un-minable —
  `container-material-unrecoverable` for container criteria, or `not-inline-admissible`
  for the inline-unadmissible class.

Either way the loop files exactly one open ticket titled
`unreliable-criterion: <criterion-id>`. Filing is idempotent: a re-run while that ticket is
open files no duplicate. The criterion is excluded from future selection until **a human
resolves it**, so a criterion that cannot be mined stops burning budget instead of failing
silently every run.

To resolve one:

1. Read the ledger entries for that criterion to see which failure mode accrued —
   non-reproducing, unbalanced, invalid-spec, or an outright un-minable declaration.
2. Fix the underlying cause (usually the criterion's rubric or its material), or decide the
   criterion is genuinely not inline-admissible and record that on the ticket.
3. Close the `unreliable-criterion:` ticket. The next scheduled run re-selects the
   criterion.

A fixture that disagrees with its predicted direction is **drift**: it is withheld,
recorded with its direction (`predicted`/`observed`) and reason (`non-reproducing`,
`unbalanced`, `invalid-spec`), and never silently admitted. A criterion that loses one side
of its fire/pass balance to drift is unbalanced and emits no spec.
