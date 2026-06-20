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
