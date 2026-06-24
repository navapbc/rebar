# Plan-review standing eval suite

The standing per-criterion eval set for the plan-review gate (epic `5fd2`, child
`7284`), wired into the shipped Inspect-AI eval seam (`rebar.llm.eval`). Each spec is a
`<prompt-id>.eval.yaml` resolved by `load_eval_spec` (a project override at
`.rebar/evals/<id>.eval.yaml` wins over the packaged copy).

| Spec | Prompt | What it measures | Owner ACs |
|------|--------|------------------|-----------|
| `plan-review-finder.eval.yaml` | `plan-review-finder` | Per-criterion **recall** (bad→finding), **false-accept** (bad→pass), **false-fire** (good→finding), seeded from the real observed-FP taxonomy. | `7284` |
| `plan-review-verifier.eval.yaml` | `plan-review-verifier` | Pass-2 **discrimination**: planted `{true, false}` finding pairs — the false ones must get LOWER graded validity; plus the **sycophancy** (false-negative) axis. | `acc1`, `7284` |
| `plan-review-isf-finder.eval.yaml` | `plan-review-isf-finder` | ISF **recall** (silent drop/narrow/contradict) + no false-fire on a **justified descope**. | `681b`, `7284` |

## Run

```sh
rebar prompt eval plan-review-finder      # validate the spec offline (grader discipline, gate, coverage)
```

The **offline** validation (spec structure, scorer discipline, `at_least(k)` gate,
coverage threshold) runs with no model and is gated by
`tests/unit/test_plan_review_evals.py`. The **live** model run (the actual
recall/false-fire scoring across epochs) needs the `nava-rebar[eval]` extra + model
credentials and runs in the eval CI.

## Corpuses

Cases are drawn from all three corpuses the epic names — **DSO** (read-only),
**snap-oakhart-manual** (Rails/Ruby), and **rebar's own tickets** (Python) — tagged by
`corpus:` on each dataset case. The false-fires are seeded from
`docs/experiments/plan-review-gate/eval/observed-false-positives.md` (FP1–FP7 + the
11-mode taxonomy + the sycophancy axis).

## Gold subset + human adjudication

Each spec carries a `gold_set` — the frozen, human-adjudicated subset the LLM-judge is
aligned to via Cohen's kappa (judge adoption gates on kappa ≥ threshold). **The labels
shipped here are a best-effort SEED pending human adjudication**: the bulk dataset is
auto/seed-labeled for breadth, but the GATING gold subset (the cases that block a prompt
change) is owned by a human reviewer (Who-Validates-the-Validators). Calibrating the
block thresholds + judge from real dogfood data (the `REVIEW_RESULT` sidecar) is the
ongoing post-implementation work this suite enables — add new observed misfires as
labeled cases and re-measure recall on the sycophancy axis after each prompt change.
