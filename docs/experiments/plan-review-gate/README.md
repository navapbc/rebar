# Plan-review gate — experiment & probe artifacts

> **These are exploratory experiment / probe artifacts, NOT automated tests and NOT production code.**
> Nothing here is imported by `rebar`, run in CI, or collected by pytest (pytest collects only `tests/`; the
> module-size gate scans only `src/rebar`). The scripts were run **manually** to ground the plan-review gate
> design in data. The production implementation will live under `src/rebar/` per epic `5fd2-a7c2-0aec-48fa`.
> Treat outputs as grounded starting defaults, not final, and don't wire any of this into the build.

The written-up findings (the actual conclusions) live in the report `.md` files one level up
(`docs/experiments/plan-review-*.md` and `criteria-registry-reconciliation.md`). This directory holds the
reproduction material behind them.

## Layout

| dir | what | note |
|-----|------|------|
| `harnesses/` | the Python runners, analysis scripts, and the registry guard | run by hand against a venv with `anthropic`; many read inputs from a scratch dir, so they reproduce the *method*, not necessarily byte-identical outputs |
| `runs/` | raw run outputs (`*.jsonl`) + intermediate data (`dso_sample.json`, `reconcile.json`, …) | the captured experiment data the reports cite |
| `criteria/` | the criterion descriptor sets (`criteria_v*.json`) + DSO-grounded specs | `criteria_v5.json` is the current, complete set (all 22 single-turn/overlay descriptors) |

## The one reusable guard

`harnesses/check_registry_coverage.py` is **not** a test (it isn't under `tests/` and CI doesn't run it) — it is a
manual completeness check that encodes the canonical v4 §5 criteria registry and fails loudly if a criteria set
omits a criterion. It exists because the criteria originally got dropped silently for want of exactly this check.
Run it by hand when editing the criteria set:

```
python3 docs/experiments/plan-review-gate/harnesses/check_registry_coverage.py \
        docs/experiments/plan-review-gate/criteria/criteria_v5.json
```

## Gate-eval instrumentation (R7, epic 6982)

`harnesses/gate_eval_instrumentation.py` is the standing dogfood job that turns E1's one-shot
outcome corpus into re-runnable per-criterion FP-proxy metrics.

```
python docs/experiments/plan-review-gate/harnesses/gate_eval_instrumentation.py --verify-repro   # 8-case reproduction (>=6/8)
python docs/experiments/plan-review-gate/harnesses/gate_eval_instrumentation.py --emit            # writes runs/gate_eval_metrics.json
```

It refreshes `runs/outcome_corpus.jsonl` by invoking `mine_outcome_corpus.py` as a **subprocess**
(that entry `sys.exit()`s on a floor-check failure, so it must not be imported), then reads each
ticket's persisted `REVIEW_RESULT` sidecars via `rebar.llm.plan_review.sidecar.all_review_results`.
`--no-refresh` reads the committed corpus (offline / CI).

**Outcome classifier (deterministic cascade).** For each ticket with a post-claim signal (an
adverse edit, an `operator-attested-retag`, or `>=2` review rounds; else `N/A`), first match wins:
`operator-attested-retag -> CAUGHT-BUT-IGNORED`; `rounds>=2 -> CAUGHT-BUT-IGNORED`;
`has_strong_finding -> UNKNOWABLE`; else `MISSED`. `has_strong_finding` = ∃ a finding (over the
ticket's deduplicated round union) with `decision ∈ {block,advisory}` AND `priority >= 0.5`
(= `RECALL_MIN_PRIORITY`) AND `severity ∈ {critical,major}`.

**The load-bearing finding: the separating signal is NOT in the sidecar findings.** A classifier
over the **sidecar-findings-alone** reaches only **4/8** on the frozen §5.2 cases — the CAUGHT
cases (c8cc, f5df) have finding profiles at or below the MISSED cases, because
"caught-but-ignored" is a fact about the author's post-claim *edit*, not about what the gate saw.
The MISSED↔CAUGHT split is carried by the outcome-corpus fields (`post_claim_edit_class`,
`review_round_count`); the sidecar contributes only the MISSED↔UNKNOWABLE tiebreak. The core
cascade reaches **7/8** (only c8cc, a lone `substantive-unclassified` case, misclassifies — a
rule to reach 8/8 rests on that single case and is deliberately excluded as overfitting).

**Metrics** (`runs/gate_eval_metrics.json`, per criterion, over the trailing `--window`,
default 200; findings deduplicated per ticket, joined to the outcome row by `ticket_id`):
`blocking_fp_proxy` = fraction of C's blocking findings whose ticket was force-closed or reopened;
`advisory_application_rate` = fraction of C's advisory findings whose ticket later shows an adverse
post-claim edit; `sample_counts` = the denominators. The pure `classify` / `compute_metrics` are
CI-tested in `tests/unit/test_gate_eval_classifier.py`. The alarm/demotion playbook (trailing
`blocking_fp_proxy > 10% -> demote to advisory`) lives in `docs/plan-review-gate.md`.
