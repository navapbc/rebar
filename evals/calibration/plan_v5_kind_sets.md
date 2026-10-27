# plan-v5 kind sets: `undecomposed` and `dod_uncertifiable`

Story `fixable-angular-caribou`. Records the evidence that converting the last two ordinal
hard-override axes to closed kind sets does not lose blocks on genuine-gap findings.

The re-score is **zero-LLM** (ADR 0054): it replays the persisted `REVIEW_RESULT` sidecars through
the shipped compose functions. Reproduce with:

```sh
python docs/calibration/plan_v5_rescore.py [--residual=advisory|floor] [--root=<checkout>]
```

`--root` points at a checkout holding `.tickets-tracker/` (a fresh worktree has no store, and
`/reports/` is gitignored, so the script lives here beside its write-up rather than under
`reports/`).

## The kind sets

Both mirror the shipped precedents — `ORACLE_GRADE01` (plan-v3, `ac_unverifiable`) and
`DIVERGENCE_GRADE01` (plan-v4, `divergent_implementation`): one advisory kind contributing 0.55,
strictly below the lowest blocking `block_threshold` (0.60) and never flooring, plus genuine-gap
kinds that trigger the 0.85 hard-override floor.

| axis | kind | consequence |
|---|---|---|
| `undecomposed` | `bundles_separable_slices` | advisory, 0.55, never floors |
| | `missing_required_child` | floors |
| | `no_executable_breakdown` | floors |
| `dod_uncertifiable` | `underspecified_certification` | advisory, 0.55, never floors |
| | `uncertifiable_outcome` | floors |
| | `certification_cannot_prove` | floors |

The designs come from reading the recorded gradings, not from theory. All 30 `undecomposed` bodies
were read: 23 say "this plan bundles N independently-releasable outcomes" — a right-sizing
observation on a plan that is executable as written — yet every one floored to 0.85, and 4 blocked
on that floor. That class is what `bundles_separable_slices` demotes. `dod_uncertifiable`'s 1,413
gradings were sampled stratified by grade; they mix three semantically distinct defects (no oracle
at all; a stated oracle that is broken or vacuously satisfiable; an oracle whose exact command is
not spelled out) under one grade-blind floor.

## Reading the old corpus

Persisted `severity_attributes` are read as RAW dicts, never through the new `Literal` model — the
closed sets exclude `low|medium|high`, so parsing historical rows through the new model would
reject them. Old rows keep their ordinal labels on disk under their own `impact_model_version`
cohort tag; only new emits stamp `plan-v5`.

The **baseline is pinned, not borrowed**. `legacy_impact_plan()` in the script reproduces the
plan-v4 formula explicitly instead of calling the shipped `impact_plan`. This matters: once the
conversion lands, the shipped function scores the corpus's ordinal grades as 0.0, which collapses
the baseline and makes the comparison report `lost=0` regardless of the change's real effect. That
false-clean result was observed during development and is exactly what the pinned baseline
prevents.

## Grade-to-kind mapping rules

Each recorded ordinal grading maps to its nearest kind by ordered regex over the finding prose plus
its evidence lines, first match wins (literal patterns in the script). The rules are deliberately
**gap-biased**: a body describing a genuine gap maps to a flooring kind, and only a body whose
complaint is specificity or framing maps to the advisory kind.

The rules were revised twice during the audit below. Bodies such as "the plan asserts idempotency,
but the only write path is documented 'no idempotency'" were initially matched as *vague* when they
are in fact **broken oracles** — the plan's stated basis for calling the work done is contradicted.
Those now map to `certification_cannot_prove`.

Mapping historical prose to kinds is inexact by nature. The result below is therefore reported as a
**bound**, not a point estimate.

## Result

Snapshot over 22,665 plan findings carrying `verification.severity_attributes`. (The C11 plan
quotes 22,503; the corpus grows as reviews are recorded, so re-running will not reproduce these
counts exactly — the shape of the result is the claim, not the digits.)

549 `dod_uncertifiable` rows match no rule. Rather than silently defaulting them, the script scores
them both ways:

| residual scored as | old at/above bar | new | lost | gained |
|---|---|---|---|---|
| `advisory` (pessimistic) | 2,897 | 2,885 | 47 | 35 |
| `floor` (optimistic) | 2,897 | 2,946 | 15 | 64 |

Kind distribution (advisory residual): `undecomposed` 21 / 3 / 6 across
bundles / missing_required_child / no_executable_breakdown; `dod_uncertifiable` 634 / 232 / 521
across underspecified / uncertifiable_outcome / certification_cannot_prove.

Blocks are also **gained** (35–64). Those are findings whose recorded grade falls outside the old
ordinal vocabulary, which `_SEV01` maps to 0.0 — so a real defect scored nothing and could not
block. The closed kind set gives every member a defined consequence.

## Audit of the losses (the R1 bar)

Per the operator's R1 instruction, a lost block is **unjustified** iff the finding body describes a
genuine gap (a committed outcome with no or broken oracle, or missing required decomposition) yet
re-maps to an advisory kind. All 15 losses surviving the optimistic bound were read individually.
Each is one of:

- an `undecomposed` bundling finding (3 of 15) — "the plan bundles two independently-releasable
  outcomes", exactly the class the operator's 2026-08-10 decision designates advisory; or
- a `dod_uncertifiable` finding whose complaint is "the plan does not specify / never names / does
  not enumerate / is procedurally rather than outcome framed" (12 of 15) — the specificity class
  `underspecified_certification` exists to coach rather than auto-block.

**No lost block is a committed outcome without an oracle, and none is a broken or vacuous oracle.**
Zero unjustified losses: the R1 escalation trigger is not met.

Print the current loss list with:

```sh
python docs/calibration/plan_v5_rescore.py --residual=floor --root=<checkout>
```
