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

## Standing per-criterion effectiveness recorder (epic 6982, extends R7)

`harnesses/criterion_effectiveness.py` is a **complementary** standing dogfood recorder whose signal
source is the sidecar **re-review history alone** — no outcome corpus, no git-object walk — so it
accumulates at **zero marginal LLM cost** and computes a *detection* proxy R7 does not, plus a
*within-review de-escalation* blocking-FP proxy distinct from R7's force-close one.

```
python docs/experiments/plan-review-gate/harnesses/criterion_effectiveness.py --record --backfill   # seed runs/criterion_firings.jsonl from all sidecars
python docs/experiments/plan-review-gate/harnesses/criterion_effectiveness.py --record              # incremental append (past the ledger watermark; idempotent)
python docs/experiments/plan-review-gate/harnesses/criterion_effectiveness.py --report --no-refresh  # writes runs/criterion_effectiveness.json (reads the committed ledger; never auto-records)
```

`--record` reads each ticket's `REVIEW_RESULT` sidecars (mirroring `all_review_results`'s
file-enumeration + v1/v2 schema guard, but reading the review `ts_ns` + `round_uuid` from the
sidecar **filename**, since `all_review_results` returns only the payload body) and appends one lean
firing row per (review-round, finding) into the append-only, prune-immune ledger
`runs/criterion_firings.jsonl`. **Ledger row schema** (short keys, v1): `t`=ticket_id, `ts`=review_ts
(ns int), `r`=round_uuid, `v`=verdict, `c`=criteria[], `n`=norm_id, `u`=fix_unit_key, `d`=decision,
`s`=severity, `p`=priority, `x`=drop_reason (`indeterminate` abstains are not recorded). It is a
single-writer standing job (like `mine_outcome_corpus.py`); run it on a cron / `session-log` cadence.
The ledger is a local, growing artifact (~8 MB over the current corpus) and is **git-ignored** (over
the 500 KB large-file gate) — regenerate it with `--record --backfill`; only the small computed
metrics artifact `runs/criterion_effectiveness.json` is committed as the CI-visible baseline.

**Metrics** (`runs/criterion_effectiveness.json`, per criterion, over a trailing `--window` of the
N most-recently-reviewed tickets, default 400 — **auto-including every criterion id in the ledger**,
so R1/R3/R4's new advisory criteria are monitored with no per-criterion wiring): `detection_proxy` =
fraction of C's blocking fix-units the ticket remediated to a PASS (blocked, then absent from a later
PASS round); `blocking_fp_proxy` = fraction the gate later de-escalated (found again but `dropped`,
not surfaced as blocking) without remediation; `sample_counts` = every numerator + denominator
(self-verifying). The pure `firings_from_review` / `compute_effectiveness` are CI-tested in
`tests/unit/test_criterion_effectiveness.py`; the advisory→blocking **promotion-gate** protocol (and
why this metric's base rate differs from R7's 10% cliff) lives in `docs/plan-review-gate.md`.
