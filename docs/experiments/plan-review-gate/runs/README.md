# Plan-review ground-truth corpora (E1 — story `doctrinal-untruthful-vaquita` / `e95e`)

Frozen ground-truth for epic **pastoral-aquatic-viper** (task-decomposition R1–R7). This
directory holds two committed corpora and the scripts that regenerate them:

| Artifact | What it is |
|---|---|
| `outcome_corpus.jsonl` | one row per **reviewed** ticket — the per-ticket outcome signals E2/E3/E6/R7 join on |
| `adjudication_corpus.jsonl` | a stratified sample of plan-review **findings**, double-labeled TP/FP by two independent LLM raters |

**Provenance.** The §5 figures re-verified below are published in
`docs/research/task-decomposition-sota-2026.md` (recovered verbatim from commit
`143d5074e`; landed by this change as the re-verification baseline). All ticket data is
recovered from the **`tickets` orphan branch** of this repo (git objects), not from any
`~/.claude/jobs/*` scratch artifact.

## Regeneration (exact commands)

```sh
# from the repo root, with the local venv active
H=docs/experiments/plan-review-gate/harnesses
R=docs/experiments/plan-review-gate/runs

python $H/mine_outcome_corpus.py                 # -> $R/outcome_corpus.jsonl  (atomic)
python $H/mine_outcome_corpus.py --verify-s5     # print the §5 re-derivation table
python $H/build_adjudication_corpus.py           # -> $R/adjudication_corpus.jsonl (unlabeled)
python $H/adjudicate.py                          # fill rater_a (all) + rater_b (subset) — LLM
python $H/kappa.py $R/adjudication_corpus.jsonl  # inter-rater reliability + validity gate
```

`adjudicate.py` needs `ANTHROPIC_API_KEY` and the `[agents]` extra; the other three are
offline. All writes are atomic (`.tmp` + `os.replace`); the sampler and subset selector
use fixed seeds, so a re-run reproduces the same corpus.

---

## 1. `outcome_corpus.jsonl` — the outcome corpus

**Population:** the **628 reviewed tickets** (any ticket carrying ≥1 `REVIEW_RESULT`
plan-review sidecar — the §5.1 population, grown from 527 since the report was authored).

**Why a git-object walk, not an on-disk scan.** The on-disk `.tickets-tracker/<id>/`
directory is *compacted*: for older tickets, `CREATE` + `STATUS` events are folded into a
`SNAPSHOT` and their event files are **deleted with no `.retired` copy on disk**. Measured
here: **140 of 631 reviewed tickets (22%) have a SNAPSHOT but zero on-disk STATUS files**
— an on-disk scan would silently emit `reopen_count=0` / `post_claim_edit_class="none"` for
~1 in 5 rows. The miner therefore recovers events from git objects
(`git rev-list --objects --all` + `git cat-file --batch`), which returns the folded events
verbatim (e.g. the fully-compacted case ticket `dc58-af7b` yields its 1 CREATE + 2 STATUS +
4 EDIT). A pre-flight floor (≥500 reviewed tickets recovered) halts on a gc'd/pruned repo
rather than shipping a truncated corpus.

### Schema (one JSON object per line)

| field | type | meaning |
|---|---|---|
| `ticket_id` | str | 4-quad id |
| `ticket_type` | str | `epic` / `story` / `task` / `bug` (from the CREATE event) |
| `level` | str | same as `ticket_type` (hierarchy level) |
| `post_claim_edit_class` | str | one of the closed §5.2 vocabulary below |
| `reopen_count` | int | # `STATUS` events with `current_status=="closed" and status=="open"` |
| `force_close` | bool | any `COMMENT` whose body starts `"FORCE_CLOSE:"` |
| `completion_verifier_fail_count` | int | # `COMPLETION_VERDICT` with `schema=="completion_verifier_fail_v1"` (on-disk retention cap 10/ticket) |
| `review_round_count` | int | # distinct `REVIEW_RESULT` uuids in history (git-object count — the TRUE count, may exceed the on-disk retention cap of 50) |
| `had_persisted_review` | bool | `review_round_count > 0` (always true for this population) |

### `post_claim_edit_class` — closed vocabulary (§5.2 taxonomy)

Classified by **diffing consecutive description states** across the first claim→close
cycle (`[first STATUS→in_progress, first STATUS→closed]`); edits in any post-reopen window
are excluded (`reopen_count` is recorded separately). A pure checkbox check-off
(`- [ ]`→`- [x]`) is normalized away so it does **not** read as a plan change. Precedence
(highest wins across the window's deltas):

`plan-authored-post-claim` › `operator-attested-retag` › `ac-strengthened` ›
`substantive-unclassified` › `cosmetic` › `none`.

- **`plan-authored-post-claim`** — an `## Acceptance Criteria` block first appears in a
  post-claim edit (the ticket was claimed with no plan).
- **`operator-attested-retag`** — an `[operator-attested]` tag is newly added post-claim.
- **`ac-strengthened`** — the AC block's content changed (items added/reworded), beyond
  checkbox state.
- **`substantive-unclassified`** — a large non-AC prose change. The semantic §5.2 classes
  (`premise-invalidated` / `scope-reduction` / `approach-change`) are **not fabricated by
  the miner** — they land here for the adjudication pass to split by hand.
- **`cosmetic`** — a small non-AC change.
- **`none`** — no substantive post-claim description edit (or no observable first claim).

### §5 re-verification table (derived vs published — per figure)

Run `mine_outcome_corpus.py --verify-s5` to reproduce.

| §5 figure | published | derived (this corpus) | verdict |
|---|---|---|---|
| reviewed-ticket population (§5.1) | **527** | **628** | **CORRECTED-TO 628** — the store grew since report commit `143d5074e`; every reviewed ticket is a new `REVIEW_RESULT`, so the population only rises. |
| post-claim-edit rate (§5.2) | **16/505 = 3.2%** | **42/503 = 8.3%** (reviewed *work* tickets) | **CORRECTED-TO 8.3%** — §5's denominator is *all* claimed work tickets (mostly unreviewed); the frozen corpus is the *reviewed* subset (503 work tickets, near §5's 505). The 2.6× higher rate is a **methodology difference**: the miner counts *any* AC-content change (checkbox check-offs excluded) mechanically, whereas §5 hand-classified a stricter set. Spot-checked: every sampled `ac-strengthened` is a genuine substantive AC rewrite, not noise. |
| substantive share | **15/16** | **51/52** (reviewed pop.) | **CORRECTED-TO 51/52** — same population/method difference; 1 of 52 edits is `cosmetic`, the rest substantive. |
| class distribution | (per §5.2) | `none` 576, `ac-strengthened` 42, `operator-attested-retag` 7, `substantive-unclassified` 2, `cosmetic` 1 | recorded (mechanical; semantic classes fold into `substantive-unclassified`) |
| **3 MISSED** asserted-capability cases | `dc58-af7b`, `db7b-c8fd`, `5886-d028` | all **present**; classes `ac-strengthened` / `ac-strengthened` / `ac-strengthened` | **AGREES** (present + post-claim-edited) |
| **4 CAUGHT-BUT-IGNORED** cases | `c8cc-68b8`, `f5df-0069`, `115b-ceea`, `8c4f-b81c` | all **present**; `substantive-unclassified` / `ac-strengthened` / `operator-attested-retag` / `operator-attested-retag` | **AGREES** (present + post-claim-edited) |
| **1 UNKNOWABLE** case | `3006-e198` | **present**; `ac-strengthened` | **AGREES** (present) — the mechanical class is recorded; §5's "unknowable" is a human verdict the corpus does not overturn. |

> Disambiguation: §5's `db7b` is the story `db7b-c8fd` ("REVIEW_RESULT reducer-ignored
> sidecar"), which shares its short id with the sidecar it discusses; the corpus pins full
> 4-quad ids to avoid ambiguity.

---

## 2. `adjudication_corpus.jsonl` — the finding-adjudication corpus

**400 findings** sampled from the persisted `REVIEW_RESULT` sidecars: **300 surfaced +
100 dropped** (floor ≥280 / ≥90), stratified by primary criterion (38 surfaced criteria,
36 dropped covered), deterministic (`seed=1729`), deduped by `(ticket_id, finding_id)`.

### SURFACED vs DROPPED is decided **positionally**, not by `decision`

`decision` alone can't classify a finding — an overflow-suppressed advisory and a surfaced
advisory both carry `decision="advisory"`. The sidecar (`sidecar.py:472-478`) concatenates
findings in a fixed segment order and `coverage.counts` gives each segment length:

```
findings = blocking ++ advisory_surfaced ++ advisory_overflow ++ indeterminate ++ dropped
SURFACED = findings[: blocking + advisory_surfaced]         # shown to the agent
DROPPED  = advisory_overflow segment ++ dropped segment     # sidecar-only (suppressed)
```

The **indeterminate** segment sits *between* overflow and dropped and is **excluded**
(an abstain — neither a surface nor a suppressed defect). Verified across the whole store:
**0 of ~1900 payloads** have `sum(counts) != len(findings)` (a mismatch would be skipped).

### Schema (one JSON object per line)

This corpus deliberately **diverges** from `code_review_adjudication.jsonl`'s
`{block-worthy, not-block-worthy, ambiguous}` vocabulary — it uses **TP/FP**.

| field | meaning |
|---|---|
| `finding_id`, `ticket_id` | finding + its ticket |
| `criterion` | primary criterion (`criteria[0]`) — the stratification key |
| `criteria[]` | all criteria the finding cites |
| `source` | `surfaced` \| `dropped` (positional, per above) |
| `decision` | `block` \| `advisory` (surfaced) \| `dropped` |
| `drop_reason` | Pass-3 floor that dropped it (nullable) |
| `finding`, `suggested_fix`, `location`, `severity`, `norm_id` | the finding's substance (what the rater judges) |
| `impact`, `validity`, `priority` | the gate's own scores |
| `tp_fp` | Rater A's label `TP` \| `FP` \| `ambiguous` (the corpus's working label) |
| `rater_a`, `rater_b` | each rater's label (`rater_b` nullable — subset only) |
| `rationale` | Rater A's one-line justification |

### TP/FP definitions (lens inverts by `source`)

- **surfaced** — `TP` = the criterion genuinely applies (a correct surface); `FP` =
  spurious (surfaced but not a real defect).
- **dropped** — `TP` = correctly not surfaced (no real defect, a justified drop); `FP` =
  a real defect the gate suppressed (an escaped defect).
- `ambiguous` rows are excluded pairwise from the kappa computation.

### Double-labeling + inter-rater reliability

Two **independent** LLM raters, different models, Rater B blind to Rater A:

- **Rater A (primary)** — `claude-opus-4-8`, rubric `adjudicate_rubric_a.md`, labels 399/400
  (one call returned empty → that row stays unlabeled, see label provenance below).
- **Rater B (independent)** — `claude-sonnet-5`, an independently-worded rubric
  `adjudicate_rubric_b.md`, re-labels a stratified-by-criterion subset (target 64 ≥ the
  50-finding floor; **102 actually double-labeled**) **blind** to Rater A.

**Cohen's kappa** (`kappa.py`) on the binary `{TP, FP}` labels of the double-labeled subset
(ambiguous excluded pairwise). Reproduce with
`python harnesses/kappa.py runs/adjudication_corpus.jsonl`:

<!-- KAPPA-RESULT -->

| stratum | double-labeled | usable binary pairs | raw agreement | Cohen's κ | Gwet's AC1 |
|---|---|---|---|---|---|
| **overall** | 102 | 94 | **0.766** | **0.472** | **0.581** |
| surfaced | 69 | 64 | 0.797 | 0.563 | 0.621 |
| dropped | 33 | 30 | 0.700 | 0.229 | 0.520 |

Confusion (overall): `A=TP,B=TP` 52 · `A=TP,B=FP` 13 · `A=FP,B=TP` 9 · `A=FP,B=FP` 20.
Raters: **A = `claude-opus-4-8`** (rubric `adjudicate_rubric_a.md`), **B = `claude-sonnet-5`**
(rubric `adjudicate_rubric_b.md`, blind to A).

### Reading the kappa: the kappa paradox, not low reliability

Raw agreement is **77%** but Cohen's κ is only **0.47** — the classic *kappa paradox*: the
label distribution is skewed (both raters call most findings TP), which inflates the
chance-agreement term κ subtracts, so κ collapses even though observed agreement is high.
**Gwet's AC1 (0.58)**, robust to prevalence skew, is the more faithful summary. The dropped
stratum shows the paradox most sharply (70% raw agreement, κ = 0.23 on only 30 pairs). κ also
**fell as n grew** (~0.74 → 0.47 over the labeling run) — the signature of small-sample luck,
not of a degrading rubric. Editing rubrics to chase κ ≥ 0.7 would be training-on-the-test-set
(a tuned κ is a worthless trust signal), so it was deliberately **not** done.

A second, **mechanical** depressor of κ: the adjudication prompt caps plan text at
`PLAN_CAP = 6000` chars (`adjudicate.py`), so on long plans (several disputed tickets have
15k–21k-char plans) a rater judged from a *truncated* view and abstained/erred, inflating
disagreement with context-starvation noise. This is a **labeling-harness limitation, not a
plan-review-gate defect** — the gate itself reviewed the full plans (these findings came from
it); only this corpus's *adjudication* prompt was capped. Human adjudication (below) read the
**full** plan per disputed finding, correcting for it.

### Validity basis: human adjudication is authoritative; κ is reported, not gated

The original design used a hard **κ ≥ 0.7 ship/discard gate**. Per operator direction
(recorded on story `e95e`, 2026-07-15) that was **replaced** by two things:

1. **Report κ honestly** as the LLM-reproducibility signal (the table above — stratified,
   plus Gwet's AC1 and the confusion matrix): disclosure, *not* a pass/fail gate.
   `kappa.py --strict` preserves the old hard gate for anyone who wants it, but the shipped
   corpus does not use it.
2. **Human adjudication of the contested findings is the authoritative ground truth.** The
   A/B disagreements were clustered into **30 distinct disputes** (22 hard TP↔FP + 8 soft,
   one-rater-abstained; near-duplicate findings within a source+criterion collapse to one
   representative, verdict propagating to every member) by `cluster_disputes.py`, then
   **adjudicated by the operator** and folded to the final gold label by
   `apply_adjudications.py`.

### Human adjudication (the gold labels)

- **Adjudicator:** the operator (a human), 2026-07-16, over `runs/disputes.jsonl` +
  `runs/dispute_worksheet.md`, each disputed finding shown with both raters' calls and a
  **full-plan** context summary.
- **Coverage:** all **30** disputes ruled (**16 → TP, 14 → FP**). Three near-duplicate
  archetypes were semantically grouped so one ruling covered both members (H013+H015,
  H003+H004, H011+H012); four clear nit/spurious/content-free disputes were **inferred from
  the operator's own prior rulings and explicitly confirmed** (H005, H009, H016, S003); the
  rest were ruled individually. Each cluster records `human_label` + `human_label_basis`.

### Final corpus label provenance (`final_label` / `label_source`)

Every row carries `final_label ∈ {TP, FP, ambiguous, null}` and a `label_source`
(reproduce with `python harnesses/apply_adjudications.py`):

| `label_source` | rows | TP / FP / amb | meaning |
|---|---|---|---|
| `single-rater-A` | 297 | 177 / 100 / 20 | outside the double-labeled subset — Rater A's label |
| `agreed` | 72 | 52 / 20 / 0 | both raters agreed |
| `human-adjudicated` | 30 | 16 / 14 / 0 | a contested dispute — the operator's verdict |
| `unlabeled` | 1 | — | Rater A's call failed (empty); no gold label |

**379 gold definite labels** (`final_label ∈ {TP, FP}`: **245 TP / 134 FP**), 20 ambiguous, 1
unlabeled. By source: **surfaced 300** → 177 TP / 110 FP / 12 ambiguous / 1 unlabeled;
**dropped 100** → 68 TP / 24 FP / 8 ambiguous. Downstream E/R stories join on `final_label`
and should treat `ambiguous`/`null` rows as excluded from gold. A blank rater cell (`""`, a
failed/empty rater call) is normalized to "not labeled" by `apply_adjudications.py` — so a row
Rater A labeled but Rater B never reached counts as `single-rater-A`, not as a disagreement.
