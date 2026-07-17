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
| **persisted-review subset** (§5.2) | **8** (3 MISSED + 4 CAUGHT-BUT-IGNORED + 1 UNKNOWABLE) | **8** — all 8 case tickets present in the corpus | **AGREES** |
| **3 MISSED** asserted-capability cases | `dc58-af7b`, `db7b-c8fd`, `5886-d028` | all **present**; classes `ac-strengthened` / `ac-strengthened` / `ac-strengthened` | **AGREES** (present + post-claim-edited) |
| **4 CAUGHT-BUT-IGNORED** cases | `c8cc-68b8`, `f5df-0069`, `115b-ceea`, `8c4f-b81c` | all **present**; `substantive-unclassified` / `ac-strengthened` / `operator-attested-retag` / `operator-attested-retag` | **AGREES** (present + post-claim-edited) |
| **1 UNKNOWABLE** case | `3006-e198` | **present**; `ac-strengthened` | **AGREES** (present) — the mechanical class is recorded; §5's "unknowable" is a human verdict the corpus does not overturn. |

> Disambiguation: §5's `db7b` is the story `db7b-c8fd` ("REVIEW_RESULT reducer-ignored
> sidecar"), which shares its short id with the sidecar it discusses; the corpus pins full
> 4-quad ids to avoid ambiguity.

---

## 2. `adjudication_corpus.jsonl` — the finding SAMPLE (UNLABELED)

> **DESCOPED (2026-07-16): finding-level TP/FP LABELS are NOT shipped.** An earlier version carried
> LLM-assigned + human-adjudicated TP/FP labels. That labeling was **invalidated**: the adjudication
> tooling truncated the plan (`PLAN_CAP=6000`) and ran single-pass with **no codebase tools**, while the
> production gate reviews the *whole* plan and grounds 16 criteria *agentically* against real code — so
> ~79% of the labels were contaminated, and the human adjudication inherited the flaw (it adjudicated a
> dispute set selected by the faulty process, anchored on faulty rationales, without independent code
> grounding). A production-faithful grounded re-labeler was prototyped but rejected as duplicative of the
> gate's own persisted sidecar validity/impact and over the human-time budget. The independent
> human-adjudicated gold set is parked as idea `e59d-c078-724d-46e5` for future enrichment. What ships
> here is the reusable, **unlabeled** finding sample only.

**400 findings** sampled from the persisted `REVIEW_RESULT` sidecars: **300 surfaced + 100 dropped**
(floor ≥280 / ≥90), stratified by primary criterion, deterministic (`seed=1729`), deduped by
`(ticket_id, finding_id)`, **text-bearing** (findings whose prose the pre-4e19/e344 lean sidecar never
persisted are excluded). Regenerate: `python $H/build_adjudication_corpus.py`.

### SURFACED vs DROPPED is decided positionally, not by `decision`

`decision` alone can't classify a finding — an overflow-suppressed advisory and a surfaced advisory both
carry `decision="advisory"`. The sidecar concatenates findings in a fixed segment order and
`coverage.counts` gives each segment length:

```
findings = blocking ++ advisory_surfaced ++ advisory_overflow ++ indeterminate ++ dropped
SURFACED = findings[: blocking + advisory_surfaced]         # shown to the agent
DROPPED  = advisory_overflow segment ++ dropped segment     # sidecar-only (suppressed)
```

The **indeterminate** segment sits between overflow and dropped and is **excluded** (an abstain).
Verified store-wide: **0 of ~1900 payloads** have `sum(counts) != len(findings)`.

### Schema (one JSON object per line) — the UNLABELED sample

| field | meaning |
|---|---|
| `finding_id`, `ticket_id` | finding + its ticket |
| `criterion` / `criteria[]` | primary criterion (stratification key) / all cited criteria |
| `source` | `surfaced` \| `dropped` (positional, per above) |
| `decision` / `drop_reason` | the gate's Pass-3 decision / floor (nullable) |
| `finding`, `suggested_fix`, `location`, `severity` | the finding's substance |
| `impact`, `validity`, `priority` | **the gate's own** persisted scores (from the sidecar — NOT a label) |

No `tp_fp` / `rater_*` / `rationale` / gold label is shipped (see the descope note). The gate's own
`impact`/`validity`/`priority` are retained as the gate's self-assessment — useful downstream (E6 re-judges
these findings for self-consistency; it never needed the TP/FP labels).
