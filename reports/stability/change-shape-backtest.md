# Change shape vs rework — 2026-09-01 (all work, 2026-07-01 .. 2026-09-02)

Ticket `floaty-imperfect-pomeranian` (`0880-0afb-7fe3-48c9`), read (a) of three, under epic
`wide-wimpy-insect` (Track I — reduce defect introduction).

Measures whether change shape actually drives the defect-rework loop **in this repository**,
before any size or decomposition rule is allowed into the deterministic floor. The field's
practice is measurement-first — Prow's `size/*` labels are non-blocking — so the decision is
taken on this data, in either direction.

## Headline

- **Surface breadth predicts rework. Diff volume does not.** Across 2,194 merged Gerrit
  changes, the probability of collecting at least one `Verified-1` before merge rises
  monotonically with files touched (22.7% → 63.5%), and the effect **survives a control for
  line count** while the reverse control fails.
- **Commit fragmentation is a null result here** and should be dropped as a variable: 1,617
  of 1,795 tickets (90.1%) are single-commit, because Gerrit's amend-and-repush model records
  rework as *patchsets*, not as extra trunk commits.
- Effect sizes are moderate (every ρ ≤ 0.42; the worst stratum still passes CI first time 38%
  of the time), which supports an **advisory** finding on file surface, **not** a blocking
  gate. See ADR 0110 for the recorded decision.

## Step 0 — overlap with ADR 0109: none

Method: read `docs/adr/0109-plan-review-replay-harness.md` and `src/rebar/llm/evals/plan_replay/`.

ADR 0109 measures the plan-review pipeline's **answer stability** under a prompt or pipeline
edit — Tier 0 verdict flip rate over the persisted `REVIEW_RESULT` corpus, Tier 1 per-question
agreement and Cohen's kappa, Tier 2 finding-set Jaccard by `norm_id`. Its unit of analysis is a
ticket's plan text and the model's findings about it. It never reads a git diff, a file count, a
Gerrit patchset, or a `caused_by` link:

```sh
grep -rlE "count_non_test_diff_lines|files_touched|non_test|patchset|rework" \
  src/rebar/llm/evals/plan_replay/     # no matches
```

The instruments are complementary: 0109 asks *did the reviewer's judgement change*; this read
asks *does change shape predict downstream rework*. A rule entering the floor would be validated
**through** Tier 0, but 0109 supplies no evidence that such a rule should exist.

## Method

Corpus: `git log --no-merges -p -U0 origin/main`, one pass, 2,860 commits (2026-06-08 ..
2026-09-01). `non_test_lines` comes from **importing** `count_non_test_diff_lines` and
`is_test_path` from `rebar.llm.code_review.bugfix_size_gate` — the shipped predicate — so the
measurement cannot drift from the gate that ships. Nothing is re-derived. Ticket refs via
`rebar._commands.verify_commit.extract_ticket_refs`.

Window **2026-07-01 .. 2026-09-01**, because `rebar-ticket` trailer coverage on `origin/main` is
60/650 in June but 876/907 in July and 1283/1283 in August. June is pre-enforcement; its 590
trailerless commits cannot be joined to tickets and would bias the ticket-level corpus.

Rework signal: Gerrit `Verified-1` votes before merge, plus patchset count. 2,208 merged changes
fetched (numbers 21..2502) and joined to main commits by revision SHA — of 2,203 merged in the
window, **2,194 matched exactly, 0 needed a subject fallback**, 9 unmatched (0.4%).

Why not the cheaper signals: `rebar metrics`' `attempts_per_ticket` is saturated (1,224 of 1,235
tickets sit at 1; `first_pass_rate` 0.9911; `revert_recovery` 0), and commits-per-ticket is
structurally weak for the reason in the headline. Store `caused_by` fan-in is usable but sparse
(137 positives) and biased by blame visibility. Gerrit is the only exact, per-change, complete
signal, so it carries the argument; the store signals corroborate below.

Reproduce the offline half — the distributions, the ticket-level correlations and the threshold
split — from the repository itself:

```sh
python scripts/backtest_bugfix_size.py --all-work \
  --rev-range origin/main --since 2026-07-01 --until 2026-09-02 \
  --out reports/stability/change-shape-backtest.json
```

(`--until` is exclusive; ~12 s; no network.) The Gerrit-joined figures — every `Verified-1` rate in
this report — need authenticated Gerrit access and are deliberately **not** in that mode, so the
shipped script stays offline and CI-provider-independent.

**A correction made while wiring this up.** An earlier draft put the ticket-level corpus at 1,793.
That came from a resolution universe admitting a ticket only if it had a gate event or a
19-character store directory, which silently and *inconsistently* dropped Jira-bridged tickets,
whose directories are named like `jira-reb-596`: `jira-reb-1163` was counted (it happened to carry a
`COMPLETION_VERDICT`) while `jira-reb-596` and `jira-reb-687` were not, though all three have commits
in the window. The shipped mode enumerates every ticket directory, giving **1,795**. No correlation,
gate-round n, or `caused_by` rate changes — the affected cells are the corpus size, two histogram
buckets and the threshold split, stated at their corrected values here.

## Distributions

Commit level, window, n=2,207:

```
non_test_lines    mean=184.8  p50=73   p75=218  p90=446  p95=676  p99=1673  max=7228
total_diff_lines  mean=371.7  p50=213  p75=457  p90=829  p95=1125 p99=2191  max=18416
files_touched     mean=6.0    p50=4    p75=7    p90=12   p95=16   p99=38    max=318
```

Ticket level, n=1,795: non-test LOC `mean=214.1 p50=94 p90=487`; files union `mean=6.8 p50=4
p90=13`; commits `mean=1.2 p95=2 p99=4`.

Commits per ticket, n=1,795: `{1: 1617, 2: 121, 3: 34, 4: 19, 6: 1, 7: 2, 25: 1}`.

For contrast, Gerrit patchsets over the 08-25..08-30 subset (n=213) are far from degenerate:
`{1: 25, 2: 95, 3: 49, 4: 26, 5: 6, 6: 2, 7: 5, 8: 2, 11: 2, 13: 1}` — only 11.7% of changes
landed on a single patchset. The rework is real; it simply is not visible in trunk commits.

Note on the shipped threshold: `BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES = 150` sits at roughly p65
of **all** work, not the p88 it occupies inside the hand-adjudicated bug-fix corpus (13/113).
Applied to all work it would flag 689 of 1,795 tickets (38.4%).

## The measured relationship (n = 2,194)

| non-test LOC | n | ≥1 `Verified-1` |
|---|---|---|
| 0–50 | 943 | 26.6% |
| 51–150 | 520 | 41.2% |
| 151–400 | 475 | 48.2% |
| 401–1000 | 205 | 52.7% |
| >1000 | 51 | 56.9% |

| files touched | n | ≥1 `Verified-1` |
|---|---|---|
| 1–2 | 761 | 22.7% |
| 3–5 | 739 | 40.2% |
| 6–10 | 435 | 46.0% |
| 11–20 | 189 | 63.5% |
| >20 | 70 | 58.6% |

Rank correlations over the same 2,194: `patchsets~LOC +0.236`, `patchsets~files +0.294`,
`V-1 count~LOC +0.242`, `V-1 count~files +0.299`. Restricted to 08-25..08-30 (n=213) they
strengthen: `patchsets~LOC +0.395`, `patchsets~files +0.416`.

### The decisive control: files carries the signal, lines do not

| holding non-test LOC at 151–400 | n | `Verified-1` rate |
|---|---|---|
| ≤5 files | 209 | 41.6% |
| 6–10 files | 168 | 48.2% |
| >10 files | 98 | **62.2%** |

| holding files at 3–5 | n | `Verified-1` rate |
|---|---|---|
| 0–50 LOC | 275 | 34.5% |
| 51–150 LOC | 240 | 45.8% |
| >150 LOC | 224 | **41.1%** |

Within a fixed line band, widening the file surface moves the rate monotonically 41.6 → 62.2%.
Within a fixed file band, adding lines does **not** — it is non-monotonic (34.5 → 45.8 → 41.1).
LOC's apparent effect in the first table is therefore largely LOC acting as a proxy for surface.

## Ticket-level corroboration

Spearman, ticket granularity, same window:

| rework signal | n | vs non-test LOC | vs files | vs commits |
|---|---|---|---|---|
| close-gate rounds | 1,373 | +0.062 (p=0.021) | +0.115 (p=1.9e-05) | +0.258 (p=4.6e-23) |
| plan-review rounds | 1,169 | +0.349 (p=5e-37) | +0.370 (p=4.3e-42) | +0.127 (p=1.3e-05) |
| `caused_by` fan-in | 1,795 | +0.175 (p=5.5e-14) | +0.190 (p=2.7e-16) | +0.084 (p=3.6e-04) |

Files is the stronger correlate on every signal. Commits' one non-trivial correlation
(close-gate rounds, +0.258) is almost certainly reverse causation: failing the close gate
produces another commit.

Binary defect-introduction label (137 positives / 1,656 negatives): tickets later named as some
bug's `caused_by` have median 213 non-test LOC and 7 files, against 89 and 4 for clean ones.

At the shipped 150-line floor, ticket level, n=1,795:

| group | n | mean close-gate rounds | mean plan-review rounds | `caused_by` rate |
|---|---|---|---|---|
| >150 non-test LOC | 689 | 1.39 | 4.34 | 12.9% |
| ≤150 | 1,106 | 1.27 | 2.62 | 4.3% |

## Verdict

**A file-surface rule is supported; a new all-work line-count floor is not; fragmentation is dropped.**

1. **Files-touched — supported.** The effect survives the control, the base rate nearly triples
   across the range (22.7 → 63.5%), n = 2,194, and it is the stronger correlate on every rework
   signal measured. This is the one variable worth putting in front of an author.
2. **A line-count floor — not supported as an independent rule.** Its effect dissolves under the
   files control. This does **not** invalidate the shipped bug-fix 150-line gate, which has a
   different scope, corpus and purpose; it means a *new* all-work line rule would measure the
   wrong thing.
3. **Commit fragmentation — dropped.** 90.1% of tickets (1,617 of 1,795) are single-commit; the variance is not
   there.
4. **Advisory, not blocking.** Every ρ ≤ 0.42, and even the worst stratum passes first time 38%
   of the time. A blocking rule on this evidence carries roughly a 38% false-positive rate.

## Confounds, stated

Nothing here is causal. (i) Both shape and rework are downstream of intrinsic task difficulty.
(ii) `caused_by` is a post-hoc attribution and larger changes are more visible to blame.
(iii) Plan-review rounds are measured on the plan while LOC is measured on the commit produced
after it, so the arrow could run either way. (iv) `Verified-1` includes infrastructure faults
(read (c) quantifies a real fraction), which adds noise but should not correlate with size, so it
biases the measured effect **downward**. (v) Only `status:merged` changes are counted, so a change
that failed CI and was abandoned never appears — the rates are a **lower bound** on rework.
